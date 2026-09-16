"""Studio API / store / agent tests (ui/studio) — offline.

- store: hosted-directory loader (plugins before patterns, bad files
  degrade and are skipped, replay is idempotent)
- agent: fenced-block parsing / plugin-block parsing / pattern text
  validation
- api: pattern management surface (publish/fork/delete/source badge)
  + generate SSE (stubbed claude runner streaming step/delta/
  "yaml + python plugin") + apply + assist + catalog

Hosted directories all point at tmp_path (monkeypatch store.PLUGINS_DIR /
PATTERNS_DIR), so the repo's host/config/ is not polluted. generate stubs
run_claude_code and assist stubs the provider — zero real subprocesses / LLM.
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

# A minimal valid agent pattern (in the shape that references the generated
# plugin gen_demo_router)
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
# store: hosted-directory loader
# ---------------------------------------------------------------------------

def test_store_load_order_plugins_before_patterns(hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
    studio_store.write_pattern_file("gen_demo", GOOD_PATTERN_YAML)

    report = studio_store.load_console_artifacts()

    assert report["plugins"]["loaded"] == ["gen_demo_router.py"]
    assert report["patterns"]["loaded"] == ["gen_demo"]
    # plugin registered and the pattern referencing it validates (load-order
    # correctness is asserted right here)
    assert plugin_registry.has("executor", "gen_demo_router")
    assert pattern_registry.get("gen_demo") is not None
    assert studio_store.console_pattern_codes() == {"gen_demo"}


def test_store_reload_replay_is_idempotent(hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
    studio_store.write_pattern_file("gen_demo", GOOD_PATTERN_YAML)
    studio_store.load_console_artifacts()
    # simulate a full reload wiping registrations: the replay must restore
    # them (repeated exec goes through the replace window)
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
# agent: fenced-block parsing / validation
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

    # heuristic for blocks without a language tag
    bare = "说明\n```\ncode: b\nnodes: []\n```\n"
    parsed2 = studio_agent.parse_generation_output(bare)
    assert parsed2["yaml"] == "code: b\nnodes: []"


def test_parse_generation_output_inline_fence_marks_in_yaml():
    # production incident regression: base_prompt copied a verbatim prompt
    # text in which an inline ``` marker must not close the outer yaml fence
    # early (otherwise the pattern is cut in half, the latter node
    # definitions are lost -> dangling edges), and the plugin fence must not
    # be squeezed into an empty fence
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
    # deeper nesting: base_prompt copies in a complete standalone ```yaml
    # example fence on its own line — the shortest candidate would cut the
    # pattern at the example fence (construction fails), so the cascade must
    # relax up to the real outer close and get the complete YAML with all
    # nodes
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
    # plugin not registered -> validation reports "unregistered"; construction
    # itself succeeds (with the lenient tool-surface default it returns the
    # (pattern, errors, warnings) triple)
    pattern, errors, warnings = studio_agent.validate_pattern_text(
        GOOD_PATTERN_YAML)
    assert pattern is not None and errors
    assert any("gen_demo_router" in e for e in errors)
    assert warnings == []  # GOOD_PATTERN_YAML does not declare use_tools
    try:
        path = studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
        studio_store.import_plugin_module(path)
        pattern2, errors2, _ = studio_agent.validate_pattern_text(
            GOOD_PATTERN_YAML)
        assert pattern2 is not None and errors2 == []
    finally:
        plugin_registry.deregister("executor", "gen_demo_router")


# ---------------------------------------------------------------------------
# api: pattern management surface
# ---------------------------------------------------------------------------

def test_publish_validate_delete_lifecycle(client, hosted_dirs):
    plugins_dir, patterns_dir = hosted_dirs
    studio_store.write_plugin_file("gen_demo_router", GOOD_PLUGIN_PY)
    studio_store.import_plugin_module(plugins_dir / "gen_demo_router.py")

    # validate: references a registered plugin -> passes
    data = _ok_data(client.post("/api/v1/studio/patterns/validate",
                                json={"yaml": GOOD_PATTERN_YAML}))
    assert data["errors"] == [] and data["constructed"] is True
    assert data["mermaid"].lstrip().startswith("flowchart")

    # publish: persisted + registered + source=console
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

    # delete: only console-sourced patterns are deletable
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
# api: generate SSE + apply (stubbed claude runner)
# ---------------------------------------------------------------------------

def _install_stub_claude(monkeypatch, final_text):
    """Replace run_claude_code with an offline script: init status ->
    step (tool call) -> step (tool result) -> two delta segments
    (simulating --include-partial-messages) -> final.

    Returns the captured dict: the full prompt the fake received (so it can
    be asserted that the user requirement was composed in)."""
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
    # the prompt really was fed to the claude runner (system contract + user
    # requirement both present)
    assert "pattern" in captured["prompt"]
    assert "测试背景" in captured["prompt"] and "功能A" in captured["prompt"]

    result = next(e["result"] for e in events if e["kind"] == "result")
    assert result["ok"] is True
    assert result["meta"]["code"] == "gen_demo"
    assert result["explanation"].startswith("两个节点")
    assert len(result["plugins"]) == 1
    assert result["plugins"][0]["imported"] is True
    # preview imports and registers the plugin but does not persist it (only apply persists)
    assert plugin_registry.has("executor", "gen_demo_router")
    assert not (plugins_dir / "gen_demo_router.py").exists()

    # apply: plugin + pattern persisted and registered in one shot
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
    """claude CLI failure (unavailable / non-zero exit) -> an error event
    and no result."""
    async def _fake_run(prompt):
        yield {"kind": "error", "message": "本地 Claude Code CLI 不可用（claude 不在 PATH 或不可执行）"}

    monkeypatch.setattr(studio_api, "run_claude_code", _fake_run)
    resp = client.post("/api/v1/studio/generate", json={
        "background": "x", "features": "y"})
    kinds = [e["kind"] for e in _sse_events(resp)]
    assert "error" in kinds and "result" not in kinds


class _FakeClaudeProc:
    """Minimal subprocess-protocol stub: stdin writable and closeable,
    stdout emits init + a success result."""

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
    """Subprocess argv keeps a read-only write surface: no
    --dangerously-skip-permissions, only a read-only tool whitelist is
    granted (non-whitelisted tools like Write/Edit/Bash are auto-denied in
    -p mode)."""
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
    """A newly generated template declares an unregistered new tool: the
    tool surface is validated leniently — generation/apply is not blocked,
    a soft warning surfaces (pattern_warnings); hosted-directory replay
    (restart semantics) is equally lenient."""
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

    # the dry-run validate endpoint is equally lenient: errors empty,
    # warnings carry the new tool
    data = _ok_data(client.post("/api/v1/studio/patterns/validate",
                                json={"yaml": result["yaml"]}))
    assert data["errors"] == []
    assert any("gen_brand_new_tool" in w for w in data["warnings"])

    # apply persists and registers successfully
    data = _ok_data(client.post("/api/v1/studio/apply", json={
        "yaml": result["yaml"],
        "plugins": [{"filename": "gen_demo_router.py", "code": GOOD_PLUGIN_PY}],
    }))
    assert data["meta"]["code"] == "gen_demo"
    assert (patterns_dir / "gen_demo.yml").is_file()
    assert any("gen_brand_new_tool" in w for w in data["warnings"])

    # hosted-directory replay (restart load) does not fail on the unregistered tool
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
    # validation failed -> no hosted file should have appeared
    assert not (plugins_dir / "gen_demo_router.py").exists()
    assert not (patterns_dir / "gen_demo.yml").exists()


# ---------------------------------------------------------------------------
# api: assist (non-streaming helper)
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
