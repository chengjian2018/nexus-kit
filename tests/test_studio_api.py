"""Studio API / store / agent tests (ui/studio) — offline.

- store: 托管目录装载器（插件先于 pattern、坏文件降级跳过、重放幂等）
- agent: 围栏解析 / 插件块解析 / pattern 文本校验
- api: pattern 管理面（publish/fork/delete/source 徽标）+ generate SSE
  （打桩 claude runner 流式产出 step/delta/「yaml + python 插件」）+ apply
  + assist + catalog

托管目录全部指向 tmp_path（monkeypatch store.PLUGINS_DIR / PATTERNS_DIR），
不污染仓库 host/config/。generate 打桩 run_claude_code、assist 打桩
provider，零真实子进程 / LLM。
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.studio.agent as studio_agent
import ui.studio.api as studio_api
import ui.studio.store as studio_store
from nexus.registry.patterns import registry as pattern_registry
from nexus.registry.plugins import registry as plugin_registry

# 一个最小合法 agent pattern（引用生成插件 gen_demo_router 的形态）
GOOD_PATTERN_YAML = """\
code: gen_demo
name: 生成演示
description: studio 测试用
pattern_type: agent
entry_node_code: start
nodes:
- code: start
  name: 开始
  description: 入口
  task_description: 理解用户来意
  sub_nodes: []
  plugins:
    loop: gen_demo_router
  config:
    base_prompt: 你是演示助手
"""

GOOD_PLUGIN_PY = """\
# studio-plugin: file=gen_demo_router.py
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.turn_result import TurnResult
from nexus.registry.plugins import registry as plugin_registry


class GenDemoRouter(NodeExecutor):
    async def execute(self, ec):
        return TurnResult(content="hi")


plugin_registry.register("executor", "gen_demo_router", GenDemoRouter)
"""


@pytest.fixture()
def hosted_dirs(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    patterns_dir = tmp_path / "patterns"
    plugins_dir.mkdir()
    patterns_dir.mkdir()
    monkeypatch.setattr(studio_store, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(studio_store, "PATTERNS_DIR", patterns_dir)
    return plugins_dir, patterns_dir


@pytest.fixture()
def client(hosted_dirs):
    app = FastAPI()
    app.include_router(studio_api.router)
    return TestClient(app)


def _ok_data(resp):
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0" and body["status"] is True
    return body["data"]


def _cleanup_generated():
    pattern_registry.deregister("gen_demo")
    plugin_registry.deregister("executor", "gen_demo_router")


@pytest.fixture(autouse=True)
def _isolate_registry():
    _cleanup_generated()
    yield
    _cleanup_generated()


# ---------------------------------------------------------------------------
# store：托管目录装载器
# ---------------------------------------------------------------------------

def test_store_load_order_plugins_before_patterns(hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
    studio_store.write_pattern_file("gen_demo", GOOD_PATTERN_YAML)

    report = studio_store.load_console_artifacts()

    assert report["plugins"]["loaded"] == ["gen_demo_router.py"]
    assert report["patterns"]["loaded"] == ["gen_demo"]
    # 插件已注册且 pattern 引用它校验通过（装载顺序正确性即在此断言）
    assert plugin_registry.has("executor", "gen_demo_router")
    assert pattern_registry.get("gen_demo") is not None
    assert studio_store.console_pattern_codes() == {"gen_demo"}


def test_store_reload_replay_is_idempotent(hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
    studio_store.write_pattern_file("gen_demo", GOOD_PATTERN_YAML)
    studio_store.load_console_artifacts()
    # 模拟全量 reload 冲掉注册：重放必须能恢复（重复 exec 走替换窗口）
    pattern_registry.deregister("gen_demo")
    report = studio_store.load_console_artifacts()
    assert report["patterns"]["loaded"] == ["gen_demo"]
    assert pattern_registry.get("gen_demo") is not None


def test_store_bad_files_degrade_without_blocking(hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    (plugins_dir / "broken_plugin.py").write_text("raise RuntimeError('bad')\n",
                                                  encoding="utf-8")
    (patterns_dir / "broken_pattern.yml").write_text("code: [unclosed\n",
                                                     encoding="utf-8")
    report = studio_store.load_console_artifacts()
    assert set(report["plugins"]["failed"]) == {"broken_plugin.py"}
    assert set(report["patterns"]["failed"]) == {"broken_pattern"}
    assert studio_store.pattern_load_error("broken_pattern")


def test_store_stem_whitelist(hosted_dirs):
    with pytest.raises(ValueError):
        studio_store.check_stem("../evil")
    with pytest.raises(ValueError):
        studio_store.check_stem("UPPER")
    with pytest.raises(ValueError):
        studio_store.check_stem("")


# ---------------------------------------------------------------------------
# agent：围栏解析 / 校验
# ---------------------------------------------------------------------------

def test_parse_generation_output_fenced_and_bare():
    raw = (
        "设计说明。\n"
        "```yaml\ncode: a\n```\n"
        "```python\n# studio-plugin: file=gen_x.py\n"
        "plugin_registry.register('executor', 'gen_x', X)\n```\n"
    )
    parsed = studio_agent.parse_generation_output(raw)
    assert parsed["yaml"] == "code: a"
    assert len(parsed["plugins"]) == 1
    assert parsed["plugins"][0]["filename"] == "gen_x.py"
    assert parsed["plugins"][0]["declarations"] == [("executor", "gen_x")]

    # 无语言标签的启发式
    bare = "说明\n```\ncode: b\nnodes: []\n```\n"
    parsed2 = studio_agent.parse_generation_output(bare)
    assert parsed2["yaml"] == "code: b\nnodes: []"


def test_parse_generation_output_inline_fence_marks_in_yaml():
    # 自动编排线上故障回归：base_prompt 抄进了提示词原文，其中行中的
    # ``` 记号不得提前闭合外层 yaml 围栏（否则 pattern 被截成半份，
    # 后半节点定义丢失 → 悬空边），插件围栏也不得被挤成空围栏
    raw = (
        "设计说明。\n"
        "```yaml\n"
        "code: inline_demo\n"
        "name: 行中围栏记号演示\n"
        "description: 提示词内嵌围栏记号\n"
        "pattern_type: agent\n"
        "entry_node_code: gen\n"
        "nodes:\n"
        "- code: gen\n"
        "  name: 生成\n"
        "  description: 生成节点\n"
        "  sub_nodes: [check]\n"
        "  config:\n"
        "    base_prompt: |\n"
        "      输出格式：恰好一个 ```yaml 围栏，0 到 N 个 ```python 围栏\n"
        "- code: check\n"
        "  name: 校验\n"
        "  description: 校验节点\n"
        "  sub_nodes: []\n"
        "```\n"
        "```python\n# studio-plugin: file=gen_inline.py\n"
        "plugin_registry.register('executor', 'gen_inline', X)\n```\n"
    )
    parsed = studio_agent.parse_generation_output(raw)
    assert "code: check" in parsed["yaml"]
    pattern, errors, _ = studio_agent.validate_pattern_text(parsed["yaml"])
    assert pattern is not None and errors == []
    assert set(pattern.node_map) == {"gen", "check"}
    assert len(parsed["plugins"]) == 1
    assert parsed["plugins"][0]["filename"] == "gen_inline.py"


def test_parse_generation_output_yaml_with_inner_standalone_fence():
    # 更深的嵌套：base_prompt 里抄进了完整的独立成行 ```yaml 范例围栏——
    # 最短候选会把 pattern 截在范例围栏处（构造失败），级联须放宽到
    # 外层真实闭合，拿到含全部节点的完整 YAML
    raw = (
        "```yaml\n"
        "code: cascade_demo\n"
        "name: 级联演示\n"
        "description: 内嵌完整围栏\n"
        "pattern_type: agent\n"
        "entry_node_code: a\n"
        "nodes:\n"
        "- code: a\n"
        "  name: A\n"
        "  description: 甲\n"
        "  sub_nodes: [b]\n"
        "  config:\n"
        "    base_prompt: |\n"
        "      范例：\n"
        "      ```yaml\n"
        "      code: sample\n"
        "      ```\n"
        "      按上述范例输出\n"
        "- code: b\n"
        "  name: B\n"
        "  description: 乙\n"
        "  sub_nodes: []\n"
        "```\n"
    )
    parsed = studio_agent.parse_generation_output(raw)
    assert "code: b" in parsed["yaml"]
    pattern, errors, _ = studio_agent.validate_pattern_text(parsed["yaml"])
    assert pattern is not None and errors == []
    assert set(pattern.node_map) == {"a", "b"}


def test_parse_assist_json_fenced_and_bare():
    text = "说明\n```json\n{\"base_prompt\": \"p\"}\n```\n"
    assert studio_agent.parse_assist_json(text) == {"base_prompt": "p"}
    assert studio_agent.parse_assist_json('{"a": 1}') == {"a": 1}
    assert studio_agent.parse_assist_json("不是 json") is None


def test_parse_plugin_block_filename_fallback_to_code():
    block = ("from nexus.engine.execution import NodeExecutor\n"
             "plugin_registry.register(\"executor\", \"gen_fallback\", X)\n")
    parsed = studio_agent.parse_plugin_block(block)
    assert parsed["stem"] == "gen_fallback"
    assert parsed["filename"] == "gen_fallback.py"


def test_validate_pattern_text_requires_plugin_registered(hosted_dirs):
    # 插件未注册 → 校验报「未注册」；构造本身成功（工具面宽松默认下
    # 返回 (pattern, errors, warnings) 三元组）
    pattern, errors, warnings = studio_agent.validate_pattern_text(
        GOOD_PATTERN_YAML)
    assert pattern is not None and errors
    assert any("gen_demo_router" in e for e in errors)
    assert warnings == []  # GOOD_PATTERN_YAML 不声明 use_tools
    try:
        path = studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
        studio_store.import_plugin_module(path)
        pattern2, errors2, _ = studio_agent.validate_pattern_text(
            GOOD_PATTERN_YAML)
        assert pattern2 is not None and errors2 == []
    finally:
        plugin_registry.deregister("executor", "gen_demo_router")


# ---------------------------------------------------------------------------
# api：pattern 管理面
# ---------------------------------------------------------------------------

def test_publish_validate_delete_lifecycle(client, hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
    studio_store.import_plugin_module(plugins_dir / "gen_demo_router.py")

    # validate：引用已注册插件 → 通过
    data = _ok_data(client.post("/api/v1/studio/patterns/validate",
                                json={"yaml": GOOD_PATTERN_YAML}))
    assert data["errors"] == [] and data["constructed"] is True
    assert data["mermaid"].lstrip().startswith("flowchart")

    # publish：落盘 + 注册 + source=console
    data = _ok_data(client.post("/api/v1/studio/patterns/publish",
                                json={"yaml": GOOD_PATTERN_YAML}))
    assert data["meta"]["source"] == "console"
    assert (patterns_dir / "gen_demo.yml").is_file()
    listed = _ok_data(client.get("/api/v1/studio/patterns"))["patterns"]
    demo = next(p for p in listed if p["code"] == "gen_demo")
    assert demo["source"] == "console"
    builtin = next(p for p in listed if p["code"] == "xianyu_agent")
    assert builtin["source"] == "code"

    # detail
    detail = _ok_data(client.get("/api/v1/studio/patterns/gen_demo"))
    assert detail["meta"]["source"] == "console"
    assert "code: gen_demo" in detail["yaml"]

    # delete：仅 console 可删
    data = _ok_data(client.delete("/api/v1/studio/patterns/gen_demo"))
    assert pattern_registry.get("gen_demo") is None
    assert not (patterns_dir / "gen_demo.yml").exists()
    resp = client.delete("/api/v1/studio/patterns/xianyu_agent")
    assert resp.status_code == 404


def test_publish_rejects_invalid_yaml(client):
    bad = "code: [unclosed\n"
    resp = client.post("/api/v1/studio/patterns/publish", json={"yaml": bad})
    assert resp.status_code == 400
    assert "解析失败" in resp.json()["message"]


def test_fork_code_managed_pattern(client, hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    data = _ok_data(client.post("/api/v1/studio/patterns/fork",
                                json={"code": "customer_agent"}))
    assert data["meta"]["source"] == "console"
    assert (patterns_dir / "customer_agent.yml").is_file()
    listed = _ok_data(client.get("/api/v1/studio/patterns"))["patterns"]
    assert next(p for p in listed if p["code"] == "customer_agent")["source"] == "console"


def test_catalog(client):
    data = _ok_data(client.get("/api/v1/studio/catalog"))
    assert {"default_loop", "default_fsm"} <= set(data["executor"])
    assert "fsm_unified" in data["stage"]


# ---------------------------------------------------------------------------
# api：generate SSE + apply（打桩 claude runner）
# ---------------------------------------------------------------------------

def _install_stub_claude(monkeypatch, final_text):
    """把 run_claude_code 换成离线脚本：init status → step(工具调用) →
    step(工具返回) → 两段 delta（模拟 --include-partial-messages）→ final。

    返回 captured dict：fake 收到的完整提示词（可断言用户需求已组入）。"""
    captured = {}

    async def _fake_run(prompt):
        captured["prompt"] = prompt
        yield {"kind": "status", "stage": "claude", "message": "已启动 stub"}
        yield {"kind": "step", "phase": "run", "id": "tu_1", "name": "Read",
               "detail": "README.md"}
        yield {"kind": "step", "phase": "result", "id": "tu_1",
               "name": "result", "detail": "1-200 行"}
        mid = len(final_text) // 2
        for piece in (final_text[:mid], final_text[mid:]):
            if piece:
                yield {"kind": "delta", "text": piece}
        yield {"kind": "status", "stage": "claude", "message": "stub 完成"}
        yield {"kind": "final", "text": final_text}

    monkeypatch.setattr(studio_api, "run_claude_code", _fake_run)
    return captured


def _sse_events(resp):
    assert resp.status_code == 200
    events = []
    for block in resp.text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


GEN_OUTPUT = (
    "两个节点：入口路由 + 应答。\n"
    "```yaml\n" + GOOD_PATTERN_YAML + "```\n"
    "```python\n" + GOOD_PLUGIN_PY + "```\n"
)


def test_generate_sse_full_pipeline(client, hosted_dirs, monkeypatch):
    plugins_dir, patterns_dir = hosted_dirs
    captured = _install_stub_claude(monkeypatch, GEN_OUTPUT)
    resp = client.post("/api/v1/studio/generate", json={
        "background": "测试背景", "features": "功能A\n功能B",
    })
    events = _sse_events(resp)
    kinds = [e["kind"] for e in events]
    assert "delta" in kinds and "step" in kinds and "result" in kinds
    assert "error" not in kinds
    deltas = "".join(e["text"] for e in events if e["kind"] == "delta")
    assert deltas == GEN_OUTPUT
    # 提示词确实喂给了 claude runner（系统契约 + 用户需求都在）
    assert "pattern" in captured["prompt"]
    assert "测试背景" in captured["prompt"] and "功能A" in captured["prompt"]

    result = next(e["result"] for e in events if e["kind"] == "result")
    assert result["ok"] is True
    assert result["meta"]["code"] == "gen_demo"
    assert result["explanation"].startswith("两个节点")
    assert len(result["plugins"]) == 1
    assert result["plugins"][0]["imported"] is True
    # 预览导入已注册插件但未落盘（apply 才落盘）
    assert plugin_registry.has("executor", "gen_demo_router")
    assert not (plugins_dir / "gen_demo_router.py").exists()

    # apply：插件 + pattern 一次性落盘注册
    plugin_code = next(
        b for b in GEN_OUTPUT.split("```")
        if "plugin_registry.register" in b
    ).split("\n", 1)[1]
    data = _ok_data(client.post("/api/v1/studio/apply", json={
        "yaml": result["yaml"],
        "plugins": [{"filename": "gen_demo_router.py", "code": plugin_code}],
    }))
    assert data["meta"]["code"] == "gen_demo"
    assert (plugins_dir / "gen_demo_router.py").is_file()
    assert (patterns_dir / "gen_demo.yml").is_file()
    assert pattern_registry.get("gen_demo") is not None


def test_generate_sse_bad_output_flags_not_ok(client, monkeypatch):
    _install_stub_claude(monkeypatch, "模型跑偏了，没有围栏输出。")
    resp = client.post("/api/v1/studio/generate", json={
        "background": "x", "features": "y"})
    result = next(e["result"] for e in _sse_events(resp) if e["kind"] == "result")
    assert result["ok"] is False
    assert any("yaml" in e for e in result["pattern_errors"])


def test_generate_sse_claude_error_aborts_without_result(client, monkeypatch):
    """claude CLI 失败（不可用/非零退出）→ error 事件且不产 result。"""
    async def _fake_run(prompt):
        yield {"kind": "error", "message": "本地 Claude Code CLI 不可用（claude 不在 PATH 或不可执行）"}

    monkeypatch.setattr(studio_api, "run_claude_code", _fake_run)
    resp = client.post("/api/v1/studio/generate", json={
        "background": "x", "features": "y"})
    kinds = [e["kind"] for e in _sse_events(resp)]
    assert "error" in kinds and "result" not in kinds


class _FakeClaudeProc:
    """最小 subprocess 协议桩：stdin 可写可关，stdout 吐 init + success result。"""

    def __init__(self):
        class _Stdin:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        self.stdin = _Stdin()

        def _lines(lines):
            async def _gen():
                for ln in lines:
                    yield ln
            return _gen()

        self.stdout = _lines([
            b'{"type": "system", "subtype": "init", "model": "stub", '
            b'"session_id": "sess"}\n',
            b'{"type": "result", "subtype": "success", "result": "ok", '
            b'"num_turns": 1, "duration_ms": 5}\n',
        ])
        self.stderr = _lines([])
        self.returncode = 0

    async def wait(self):
        return 0

    def kill(self):
        pass


def test_run_claude_code_argv_is_read_only(monkeypatch):
    """子进程 argv 收敛写入面：不带 --dangerously-skip-permissions，
    只授只读工具白名单（Write/Edit/Bash 等未白名单工具在 -p 模式自动拒绝）。"""
    argv = {}

    async def _fake_exec(*args, **kwargs):
        argv["args"] = args
        argv["cwd"] = kwargs.get("cwd")
        return _FakeClaudeProc()

    monkeypatch.setattr(studio_api.asyncio, "create_subprocess_exec", _fake_exec)

    async def _drain():
        return [ev async for ev in studio_api.run_claude_code("hi")]

    events = asyncio.run(_drain())
    assert events[-1]["kind"] == "final" and events[-1]["text"] == "ok"

    args = argv["args"]
    assert argv["cwd"] == str(studio_api._PROJECT_ROOT)
    assert "--dangerously-skip-permissions" not in args
    i = args.index("--allowedTools")
    allowed = args[i + 1:i + 1 + len(studio_api._CLAUDE_ALLOWED_TOOLS)]
    assert list(allowed) == list(studio_api._CLAUDE_ALLOWED_TOOLS)
    assert not ({"Write", "Edit", "Bash", "NotebookEdit", "WebFetch"}
                & set(allowed))


def test_generate_allows_unregistered_new_tools(client, hosted_dirs, monkeypatch):
    """新生成模版声明未注册的新工具：工具面宽松校验——不阻塞生成/应用，
    软警告（pattern_warnings）透出；托管目录重放（重启语义）同样宽松。"""
    plugins_dir, patterns_dir = hosted_dirs
    yaml_new_tool = GOOD_PATTERN_YAML.replace(
        "    loop: gen_demo_router\n  config:",
        "    loop: gen_demo_router\n  use_tools:\n  - gen_brand_new_tool\n  config:")
    assert "gen_brand_new_tool" in yaml_new_tool
    output = ("```yaml\n" + yaml_new_tool + "```\n"
              + "```python\n" + GOOD_PLUGIN_PY + "```\n")
    _install_stub_claude(monkeypatch, output)

    resp = client.post("/api/v1/studio/generate", json={
        "background": "b", "features": "f"})
    result = next(e["result"] for e in _sse_events(resp) if e["kind"] == "result")
    assert result["ok"] is True
    assert any("gen_brand_new_tool" in w for w in result["pattern_warnings"])

    # dry-run 校验端点同样宽松：errors 空、warnings 带新工具
    data = _ok_data(client.post("/api/v1/studio/patterns/validate",
                                json={"yaml": result["yaml"]}))
    assert data["errors"] == []
    assert any("gen_brand_new_tool" in w for w in data["warnings"])

    # 应用落盘注册成功
    data = _ok_data(client.post("/api/v1/studio/apply", json={
        "yaml": result["yaml"],
        "plugins": [{"filename": "gen_demo_router.py", "code": GOOD_PLUGIN_PY}],
    }))
    assert data["meta"]["code"] == "gen_demo"
    assert (patterns_dir / "gen_demo.yml").is_file()
    assert any("gen_brand_new_tool" in w for w in data["warnings"])

    # 托管目录重放（重启装载）不因未注册工具失败
    _cleanup_generated()
    report = studio_store.load_console_artifacts()
    assert report["patterns"]["loaded"] == ["gen_demo"]


def test_apply_rejects_plugin_before_any_write(client, hosted_dirs, monkeypatch):
    plugins_dir, patterns_dir = hosted_dirs
    bad_plugin = "# 语法错误\nthis is not python(\n"
    resp = client.post("/api/v1/studio/apply", json={
        "yaml": GOOD_PATTERN_YAML,
        "plugins": [{"filename": "gen_demo_router.py", "code": bad_plugin}],
    })
    assert resp.status_code == 400
    # 验证失败 → 任何托管文件都不该出现
    assert not (plugins_dir / "gen_demo_router.py").exists()
    assert not (patterns_dir / "gen_demo.yml").exists()


# ---------------------------------------------------------------------------
# api：assist（非流式小助手）
# ---------------------------------------------------------------------------

class StubChatProvider:
    def __init__(self, content):
        self._content = content

    async def achat_completion(self, messages, model=None, temperature=0.7,
                               max_tokens=2048, **kw):
        return {"content": self._content}


def test_assist_node_prompt_parses_json(client, monkeypatch):
    content = '```json\n{"base_prompt": "你是预约助手", "answer_examples": ["好的"]}\n```'
    monkeypatch.setattr(studio_api, "build_provider",
                        lambda cfg: StubChatProvider(content))
    monkeypatch.setattr(studio_api, "get_llm_config",
                        lambda override=None: {"code": "stub", "model": "m"})
    data = _ok_data(client.post("/api/v1/studio/assist", json={
        "mode": "node_prompt",
        "payload": {"node_code": "ask", "hints": "亲切"}}))
    assert data["json"]["base_prompt"] == "你是预约助手"
    assert data["json"]["answer_examples"] == ["好的"]


def test_assist_plugin_generate_parses_plugin(client, monkeypatch):
    monkeypatch.setattr(studio_api, "build_provider",
                        lambda cfg: StubChatProvider("```python\n" + GOOD_PLUGIN_PY + "```"))
    monkeypatch.setattr(studio_api, "get_llm_config",
                        lambda override=None: {"code": "stub", "model": "m"})
    data = _ok_data(client.post("/api/v1/studio/assist", json={
        "mode": "plugin_generate",
        "payload": {"node_code": "route", "hints": "按意图路由"}}))
    assert data["plugin"]["stem"] == "gen_demo_router"
    assert {"kind": "executor", "code": "gen_demo_router"} in data["plugin"]["declarations"]


def test_assist_unknown_mode_400(client):
    resp = client.post("/api/v1/studio/assist", json={"mode": "nope"})
    assert resp.status_code == 400
