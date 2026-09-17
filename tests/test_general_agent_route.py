"""Offline tests for general_agent (the single-node general-purpose agent).

Verifies the full chain of "one default_loop node + all-builtin capability
surface + two mounted builder skills" —

1. Graph structure: single-node AGENT graph + explicit default_loop binding
   + validate_pattern (skills resolve against the scan root)
2. L0 metadata injection: the system prompt carries an "可用技能" block with
   both builder skills; knowledge tools auto-appended alongside the
   execution-side tools
3. One full turn: load the app-builder manual → read a skill reference →
   write the app scaffold → bash pytest → close out; the manual content
   enters model context; the scaffold really lands on disk
4. Unauthorized interception: a load_skill request for an unmounted skill →
   error backfill (listing authorized skills) → the model self-corrects
5. Zero new registrations: no executor/stage/tool registered by this app —
   route.py's import surface is prompts + nexus models only
"""

import json
import logging
from unittest.mock import patch

import pytest

from async_utils import arun

logging.basicConfig(level=logging.WARNING)

# Host load order is "tools discovered first, patterns loaded after"
# (validate_tools checks at registration time)
from nexus.registry.tools import discover_builtin_tools

discover_builtin_tools()

import nexus.engine.loop as loop_mod
from nexus.skills import invalidate_skills_cache


# ============================================================================
# Fixtures / helpers
# ============================================================================

BUILDER_SKILL = "nexus-app-builder-skill"
TEMPLATE_SKILL = "nexus-app-template-skill"


@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.general_agent.route" in imported, (
        f"route 未被自动发现,已发现: {imported}")
    return registry.get("general_agent")


@pytest.fixture()
def skill_root(tmp_path, monkeypatch, pattern):
    """Minimal builder-shaped skill pair (manual + one reference each);
    pattern.config.skills_dir is pinned to the tmp scan root."""
    root = tmp_path / "skills"
    for name, display, desc, ref in (
        (BUILDER_SKILL, "nexus-app-builder",
         "把业务需求转成 nexus-kit 应用（Pattern 图 + 能力插件，落 apps/）",
         "architecture.md"),
        (TEMPLATE_SKILL, "nexus-app-template-builder",
         "把需求或既有应用转成 app-templates/ 知识库模板条目（声明态蓝图）",
         "template-card.md"),
    ):
        skill_dir = root / name
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {display}\n"
            f"description: {desc}\n"
            "---\n"
            f"# {display}（测试桩）\n\n"
            "四阶段：侦察 → 图类型决策 → 计划 → 实现与验证。\n"
            "验证必须跑 pytest，测试全绿才算完成。\n",
            encoding="utf-8")
        (skill_dir / "references" / ref).write_text(
            f"# {ref}（测试桩参考）\n单节点 AGENT 图是最小应用形态。\n",
            encoding="utf-8")
    monkeypatch.setitem(pattern.config, "skills_dir", str(root))
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
# Scripted provider (round script: load → read reference → write → pytest →
# close-out)
# ============================================================================

_ROUTE_PY = (
    "from nexus.model.pattern import Pattern\n"
    "pattern = Pattern(code='demo_booking', name='demo', description='stub')\n"
)


class BuilderScriptedProvider:
    """Fixed round script; records each request's messages and tools for
    assertions. first_call can inject an unauthorized call (tries an
    unmounted skill first; after the error backfill it self-corrects)."""

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
                _tc("load_skill", {"name": BUILDER_SKILL}, "c1")],
                "finish_reason": "tool_calls"}
        if step == 2:
            return {"content": None, "tool_calls": [
                _tc("read_skill_file", {
                    "name": BUILDER_SKILL,
                    "rel_path": "references/architecture.md"}, "c2")],
                "finish_reason": "tool_calls"}
        if step == 3:
            return {"content": None, "tool_calls": [
                _tc("write_text", {
                    "path": str(self.workspace / "apps" / "demo_booking"
                                / "route.py"),
                    "content": _ROUTE_PY}, "c3")],
                "finish_reason": "tool_calls"}
        if step == 4:
            return {"content": None, "tool_calls": [
                _tc("bash", {
                    "command": "pytest tests/test_demo_booking_route.py",
                    "workdir": str(self.workspace)}, "c4")],
                "finish_reason": "tool_calls"}
        return {"content": "应用已创建：apps/demo_booking/route.py（含 "
                           "tests/），pytest 全绿。",
                "tool_calls": [], "finish_reason": "stop"}


def make_bash_stub(calls):
    """bash receipt stub: pytest → exit 0; non-bash tools go through the
    real registry (skill loading / file writes land in the tmp workspace)."""
    real = loop_mod._execute_tool

    async def fake(name, args):
        if name != "bash":
            return await real(name, args)
        calls.append((str(args.get("command") or ""), args.get("workdir")))
        return json.dumps({"exit_code": 0, "stdout": "3 passed",
                           "stderr": "", "timed_out": False},
                          ensure_ascii=False)
    return fake


def run_turn(pattern, provider, bash_stub,
             query="帮我做一个预约应用：收集姓名、时间，确认后登记"):
    sessions = {}
    launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider), \
            patch.object(loop_mod, "_execute_tool", bash_stub):
        reply = chat(sessions, "s1", query)
    return sessions["s1"], reply


# ============================================================================
# 1. Graph structure and validation
# ============================================================================

def test_pattern_structure_and_validation(pattern, skill_root):
    assert pattern.code == "general_agent"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "assistant"
    assert [n.code for n in pattern.nodes] == ["assistant"]
    node = pattern.node_map["assistant"]
    assert node.is_end is True
    assert node.sub_nodes == []
    # two-layer skill declaration + three-layer execution-side declaration
    assert pattern.allow_skills == [BUILDER_SKILL, TEMPLATE_SKILL]
    assert node.use_skills == [BUILDER_SKILL, TEMPLATE_SKILL]
    assert pattern.allow_toolset == ["shell", "filesystem"]
    assert node.use_tools == [
        "bash", "run_python",
        "read_text", "write_text", "edit_file",
        "list_dir", "search_files", "find_files",
    ]
    assert "load_skill" not in node.use_tools  # knowledge tool auto-granted
    assert node.plugins["loop"] == "default_loop"  # zero custom executors

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)  # skills resolve against the fixture root


def test_zero_new_registrations(pattern):
    """The app registers nothing but its pattern: source-level guarantee —
    no plugin/tool registry import anywhere in the app dir, and the only
    registration call is the pattern one in route.py."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "apps" / "general_agent"
    py_sources = {p.name: p.read_text(encoding="utf-8")
                  for p in app_dir.glob("*.py")}
    assert set(py_sources) == {"__init__.py", "route.py", "prompts.py"}
    for name, src in py_sources.items():
        assert "nexus.registry.plugins" not in src, name
        assert "nexus.registry.tools" not in src, name
    # the only registration is the pattern (route.py bottom)
    assert py_sources["route.py"].count("registry.register(") == 1
    # runtime side: the node binds only the builtin default_loop
    node = pattern.node_map["assistant"]
    assert node.plugins == {"loop": "default_loop"}
    assert pattern.plugins.get("loop") is None  # pattern layer not used


# ============================================================================
# 1b. App config overlay: loop budget + llm tier (apps/general_agent/config.yaml)
# ============================================================================

def test_app_config_overrides(pattern, monkeypatch):
    """config.yaml 生效：pattern 级 loop.max_tool_rounds=50，llm 换
    zai/glm-5.3（引擎每轮经 get_llm_config 分层合并，测试同路径断言）。

    conftest 默认把 NEXUS_APPS_DIR 指到临时目录隔离 app 配置；按 conftest
    头注的既有惯例 delenv + 失效缓存后读真实仓库的 apps/ 配置。"""
    import nexus.settings as settings_mod
    from nexus.settings import get_llm_config, get_loop_limits

    monkeypatch.delenv("NEXUS_APPS_DIR", raising=False)
    settings_mod.invalidate_config_cache()
    try:
        assert get_loop_limits(
            "general_agent", "assistant")["max_tool_rounds"] == 50
        llm = get_llm_config("general_agent", "assistant")
        assert llm["code"] == "zai"
        assert llm["model"] == "glm-5.3"
    finally:
        settings_mod.invalidate_config_cache()


# ============================================================================
# 2. One full-turn run
# ============================================================================

def test_full_turn_builder_skill_driven(pattern, skill_root, tmp_path):
    ws = tmp_path / "ws"
    provider = BuilderScriptedProvider(ws, skill_root)
    calls = []
    session, reply = run_turn(pattern, provider, make_bash_stub(calls))

    assert provider.rounds == 5
    sys0, tools0 = provider.requests[0]
    system0 = next(m["content"] for m in sys0 if m["role"] == "system")
    # base_prompt discipline is present (dsh-style tool rules)
    assert "read_text" in system0 and "edit_file" in system0
    assert "exit_code" in system0  # bash result-check discipline
    # L0 metadata injection: both builder skills visible (the trigger)
    assert "可用技能" in system0
    assert BUILDER_SKILL in system0 and TEMPLATE_SKILL in system0
    assert "把业务需求转成 nexus-kit 应用" in system0
    # knowledge tools auto-appended, coexisting with execution-side tools
    assert "load_skill" in tools0 and "read_skill_file" in tools0
    for name in ("bash", "run_python", "read_text", "write_text",
                 "edit_file", "list_dir", "search_files", "find_files"):
        assert name in tools0, name

    # manual content enters model context (round-2 request carries the
    # load_skill receipt with the manual body)
    sys1, _ = provider.requests[1]
    joined1 = json.dumps(sys1, ensure_ascii=False)
    assert "load_skill" in joined1
    assert "nexus-app-builder（测试桩）" in joined1

    # skill reference read lands in context too (round 3)
    sys2, _ = provider.requests[2]
    assert "测试桩参考" in json.dumps(sys2, ensure_ascii=False)

    # the scaffold really lands on disk; pytest ran in the workspace
    assert "Pattern(code='demo_booking'" in (
        ws / "apps" / "demo_booking" / "route.py").read_text(encoding="utf-8")
    assert calls and "pytest" in calls[0][0]

    # the reply is the model's closing copy mentioning the outputs
    assert "应用已创建" in reply

    # default_loop semantics: tool trace in history; single-node graph ends
    assert "tool" in [m.role for m in session.cxt.history]
    assert session.cxt.graph_state == {}
    assert session.cxt.current_node_code == "assistant"


# ============================================================================
# 3. Unauthorized interception and self-correction
# ============================================================================

def test_unmounted_skill_rejected_then_self_corrects(
        pattern, skill_root, tmp_path):
    ws = tmp_path / "ws"
    provider = BuilderScriptedProvider(
        ws, skill_root,
        first_call=("load_skill", {"name": "archify"}, "c0"))
    calls = []
    session, reply = run_turn(pattern, provider, make_bash_stub(calls))

    # the error receipt is visible in the round-2 request (listing the two
    # authorized skills), then the builder manual loads in self-correction
    sys1, _ = provider.requests[1]
    joined1 = json.dumps(sys1, ensure_ascii=False)
    assert "未授权给本节点" in joined1
    assert BUILDER_SKILL in joined1 and TEMPLATE_SKILL in joined1
    assert provider.rounds == 6
    assert "应用已创建" in reply
    # the unauthorized call produced no manual content in the first request
    assert "测试桩" not in json.dumps(
        provider.requests[0][0], ensure_ascii=False)
