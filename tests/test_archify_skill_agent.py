"""archify_skill（skill 说明书式单节点版）离线测试。

与 test_archify_agent.py（九节点 workflow 版）平行、互相独立：本文件验证
「skill 作为数据资产 + 单 default_loop 节点」的完整链路——

1. 图结构:单节点 AGENT 图 + default_loop 显式绑定 + validate_pattern
   （含 validate_skills:requires_toolsets ⊆ allow_toolset）
2. L0 元数据注入:system prompt 出现「可用技能」区块（名称+描述）
3. 知识工具自动追加:load_skill / read_skill_file 无需 use_tools 声明
   即在工具列表;执行面工具（bash/文件五件套）仍走三层收口
4. 全链路一轮:装载手册 → 读 schema → 写候选 → bash validate → bash
   deliver → 收口;手册内容进入模型上下文（第 2 轮请求可见 load_skill
   回执）;候选真实落盘;CLI workdir=技能目录
5. 越权拦截:load_skill 请求未启用技能 → 错误回填（列出已授权）→ 模型
   自纠后继续
6. 独立性:不依赖 archify workflow 版的任何执行器/模块
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

# 宿主装载顺序是"工具先发现、pattern 后装载"(validate_tools 注册期校验)
from nexus.registry.tools import discover_builtin_tools

discover_builtin_tools()

import nexus.engine.loop as loop_mod
from nexus.skills import invalidate_skills_cache


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.archify_skill_agent.route" in imported, (
        f"route 未被自动发现,已发现: {imported}")
    return registry.get("archify_skill")


@pytest.fixture()
def skill_root(tmp_path, monkeypatch, pattern):
    """最小 archify 形态的技能目录（schemas/examples/references + 手册），
    pattern.config.skills_dir 钉到它。"""
    root = tmp_path / "skills" / "archify"
    (root / "schemas").mkdir(parents=True)
    (root / "examples").mkdir(parents=True)
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: archify\n"
        "description: 演示用 archify 手册：图表创作-验证-交付纪律\n"
        "requires_toolsets: [shell, filesystem]\n"
        "metadata:\n  version: 'test'\n"
        "---\n"
        "# Archify（测试桩）\n\n"
        "1. 从需求选 diagram_type（workflow/...）。\n"
        "2. 读 schemas/<type>.schema.json 与 schemas/common.schema.json，"
        "再看一个 examples/ 示例（只取字段形态）。\n"
        "3. 产物优先：下一个动作就是写出候选 JSON。\n"
        "4. validate 后 deliver；非零退出绝不称成功。\n",
        encoding="utf-8")
    (root / "schemas" / "common.schema.json").write_text(
        json.dumps({"type": "object", "common": True}), encoding="utf-8")
    (root / "schemas" / "workflow.schema.json").write_text(
        json.dumps({"type": "object", "diagram": "workflow"}),
        encoding="utf-8")
    (root / "examples" / "workflow.example.json").write_text(
        json.dumps({"diagram_type": "workflow", "meta": {}}),
        encoding="utf-8")
    monkeypatch.setitem(pattern.config, "skills_dir", str(root.parent))
    invalidate_skills_cache()
    return root


def launch(pattern, sessions, session_id="s1"):
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


# ============================================================================
# Scripted provider（轮次脚本:load → read → write → validate → deliver → 收口）
# ============================================================================

_CANDIDATE = json.dumps({
    "schema_version": 2, "diagram_type": "workflow",
    "meta": {"title": "CI 发布", "quality_profile": "showcase"},
    "nodes": [{"id": "ci", "label": "CI"}],
}, ensure_ascii=False)


class SkillScriptedProvider:
    """固定轮次脚本;记录每次请求的 messages 与 tools 供断言。first_call
    可注入越权调用（先试未启用技能,错误回填后自纠）。"""

    def __init__(self, workspace, skill_root, first_call=None):
        self.workspace = workspace
        self.skill_root = skill_root
        self.first_call = first_call
        self.rounds = 0
        self.requests = []  # [(messages_snapshot, tool_names)]

    def _snapshot(self, messages, tools):
        self.requests.append((
            [dict(m) for m in messages],
            [t["function"]["name"] for t in (tools or [])],
        ))

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self._snapshot(messages, tools)
        self.rounds += 1
        idx = self.rounds

        def _tc(name, args, call_id):
            return {"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}

        if self.first_call and idx == 1:
            return {"content": None, "tool_calls": [_tc(*self.first_call)],
                    "finish_reason": "tool_calls"}
        offset = 1 if self.first_call else 0
        step = idx - offset
        if step == 1:
            return {"content": None, "tool_calls": [
                _tc("load_skill", {"name": "archify"}, "c1")],
                "finish_reason": "tool_calls"}
        if step == 2:
            return {"content": None, "tool_calls": [
                _tc("read_text", {"path": str(
                    self.skill_root / "schemas" / "workflow.schema.json")},
                    "c2")],
                "finish_reason": "tool_calls"}
        if step == 3:
            return {"content": None, "tool_calls": [
                _tc("write_text", {
                    "path": str(self.workspace / "ci-release.json"),
                    "content": _CANDIDATE}, "c3")],
                "finish_reason": "tool_calls"}
        if step in (4, 5):
            cmd = ("validate" if step == 4 else "deliver")
            return {"content": None, "tool_calls": [
                _tc("bash", {"command": f"node bin/archify.mjs {cmd} ...",
                             "workdir": str(self.skill_root)}, f"c{step}")],
                "finish_reason": "tool_calls"}
        return {"content": "图表已交付：data 产物见工作区，validate 与 "
                           "deliver 回执均通过。",
                "tool_calls": [], "finish_reason": "stop"}


def make_bash_stub(calls):
    """bash 出票桩:validate → showcase 通过回执;deliver → ok;非 bash 工具
    放行真实注册表（文件读写/技能装载落 tmp 工作区）。"""
    real = loop_mod._execute_tool  # patch 前的原实现（闭包钉住）

    async def fake(name, args):
        if name != "bash":
            return await real(name, args)
        cmd = str(args.get("command") or "")
        calls.append((cmd, args.get("workdir")))
        if "validate" in cmd:
            receipt = {"ok": True,
                       "checks": [{"name": f"c{i}", "ok": True}
                                  for i in range(9)],
                       "warnings": []}
        else:
            receipt = {"ok": True, "artifact": {
                "sha256": "ab" * 32, "bytes": 4096}}
        return json.dumps({"exit_code": 0, "stdout": json.dumps(
            receipt, ensure_ascii=False), "stderr": "", "timed_out": False},
            ensure_ascii=False)
    return fake


def run_turn(pattern, provider, bash_stub, query="画一个 CI 发布工作流图"):
    sessions = {}
    launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider), \
            patch.object(loop_mod, "_execute_tool", bash_stub):
        reply = chat(sessions, "s1", query)
    return sessions["s1"], reply


# ============================================================================
# 1. 图结构与校验
# ============================================================================

def test_pattern_structure_and_validation(pattern, skill_root):
    assert pattern.code == "archify_skill"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "as_work"
    assert [n.code for n in pattern.nodes] == ["as_work"]
    node = pattern.node_map["as_work"]
    assert node.is_end is True
    assert node.sub_nodes == []
    # 技能双层声明 + 执行面三层声明
    assert pattern.allow_skills == ["archify"]
    assert node.use_skills == ["archify"]
    assert pattern.allow_toolset == ["shell", "filesystem"]
    assert node.use_tools == ["bash", "read_text", "write_text",
                              "edit_file", "find_files"]
    assert "load_skill" not in node.use_tools  # 知识工具自动授予,不进声明
    assert node.plugins["loop"] == "default_loop"  # 零定制执行器

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)  # 含 validate_skills（桩技能 requires
    # toolsets=[shell, filesystem] ⊆ allow_toolset）


def test_independent_from_workflow_version(pattern):
    """与 archify workflow 版互相独立:不绑任何 af_* 执行器,节点只用
    default_loop;registry 里两 pattern 并存互不影响;app 源码零引用
    archify_agent。"""
    from nexus.registry.patterns import registry

    assert "archify" in registry.list_codes()  # workflow 版原样存在
    assert pattern.node_map["as_work"].plugins["loop"] == "default_loop"
    # route.py 的 import 面:仅 prompts + nexus 声明模型（无 archify_agent）
    app_dir = Path(__file__).resolve().parents[1] / "apps" / \
        "archify_skill_agent"
    for path in app_dir.glob("*.py"):
        assert "apps.archify_agent" not in path.read_text(encoding="utf-8"), \
            path


# ============================================================================
# 2. 全链路一轮
# ============================================================================

def test_full_turn_skill_driven(pattern, skill_root, tmp_path):
    ws = tmp_path / "ws"
    provider = SkillScriptedProvider(ws, skill_root)
    calls = []
    session, reply = run_turn(pattern, provider, make_bash_stub(calls))

    assert provider.rounds == 6
    sys0, tools0 = provider.requests[0]
    system0 = next(m["content"] for m in sys0 if m["role"] == "system")
    # L0 元数据注入（描述即触发器）
    assert "可用技能" in system0
    assert "archify" in system0
    assert "演示用 archify 手册" in system0
    # 知识工具自动追加 + 执行面工具并存
    assert "load_skill" in tools0 and "read_skill_file" in tools0
    assert "bash" in tools0 and "write_text" in tools0

    # 手册内容进入模型上下文（第 2 轮请求携带 load_skill 回执行）
    sys1, _ = provider.requests[1]
    joined1 = json.dumps(sys1, ensure_ascii=False)
    assert "load_skill" in joined1 and "Archify（测试桩）" in joined1

    # bash 执行面:validate + deliver,workdir=技能目录
    assert [c for c, _ in calls if "validate" in c]
    assert [c for c, _ in calls if "deliver" in c]
    assert {wd for _, wd in calls} == {str(skill_root)}

    # 候选真实落盘;回复为模型收口文案
    assert json.loads((ws / "ci-release.json").read_text(
        encoding="utf-8"))["meta"]["quality_profile"] == "showcase"
    assert "图表已交付" in reply

    # default_loop 语义:工具轨迹入会话历史;单节点图跑完即终
    assert "tool" in [m.role for m in session.cxt.history]
    assert session.cxt.graph_state == {}
    assert session.cxt.current_node_code == "as_work"


# ============================================================================
# 3. 越权拦截与自纠
# ============================================================================

def test_unenabled_skill_rejected_then_self_corrects(
        pattern, skill_root, tmp_path):
    ws = tmp_path / "ws"
    provider = SkillScriptedProvider(
        ws, skill_root,
        first_call=("load_skill", {"name": "not-a-skill"}, "c0"))
    calls = []
    session, reply = run_turn(pattern, provider, make_bash_stub(calls))

    # 第 2 轮请求里可见错误回执（列出已授权技能）,随后自纠装载 archify
    sys1, _ = provider.requests[1]
    joined1 = json.dumps(sys1, ensure_ascii=False)
    assert "未授权给本节点" in joined1
    assert provider.rounds == 7
    assert "图表已交付" in reply
    # 越权调用没有产出手册内容:第 2 轮之后（自纠轮）才出现手册
    assert "Archify（测试桩）" not in json.dumps(
        provider.requests[0][0], ensure_ascii=False)
