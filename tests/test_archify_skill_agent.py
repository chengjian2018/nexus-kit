"""Offline tests for archify_skill (the skill-manual single-node variant).

Parallel to and independent from test_archify_agent.py (the nine-node
workflow variant): this file verifies the full chain of "skill as a data
asset + a single default_loop node" —

1. Graph structure: single-node AGENT graph + explicit default_loop binding
   + validate_pattern (incl. validate_skills: requires_toolsets ⊆
   allow_toolset)
2. L0 metadata injection: the system prompt carries an "available skills"
   block (name + description)
3. Knowledge tools auto-appended: load_skill / read_skill_file appear in
   the tool list with no use_tools declaration; execution-side tools
   (bash / the file five) still go through the three-layer funnel
4. One full turn: load the manual → read the schema → write the candidate
   → bash validate → bash deliver → close out; the manual content enters
   model context (the round-2 request shows the load_skill receipt); the
   candidate really lands on disk; CLI workdir = skill directory
5. Unauthorized interception: a load_skill request for a disabled skill →
   error backfill (listing authorized skills) → the model self-corrects
   and continues
6. Independence: depends on no executor/module of the archify workflow
   variant
"""

import json
import logging
from pathlib import Path
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

@pytest.fixture(scope="module")
def pattern():
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.archify_skill_agent.route" in imported, (
        f"route 未被自动发现,已发现: {imported}")
    return registry.get("archify_skill")


@pytest.fixture()
def skill_root(tmp_path, monkeypatch, pattern):
    """Minimal archify-shaped skill directory (schemas/examples/references
    + manual); pattern.config.skills_dir is pinned to it."""
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
# Scripted provider (round script: load → read → write → validate → deliver → close-out)
# ============================================================================

_CANDIDATE = json.dumps({
    "schema_version": 2, "diagram_type": "workflow",
    "meta": {"title": "CI 发布", "quality_profile": "showcase"},
    "nodes": [{"id": "ci", "label": "CI"}],
}, ensure_ascii=False)


class SkillScriptedProvider:
    """Fixed round script; records each request's messages and tools for
    assertions. first_call can inject an unauthorized call (tries a disabled
    skill first; after the error backfill it self-corrects)."""

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
    """bash receipt stub: validate → a passing showcase receipt; deliver →
    ok; non-bash tools go through the real registry (file read/write /
    skill loading land in the tmp workspace)."""
    real = loop_mod._execute_tool  # original implementation before patching (pinned in the closure)

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
# 1. Graph structure and validation
# ============================================================================

def test_pattern_structure_and_validation(pattern, skill_root):
    assert pattern.code == "archify_skill"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "as_work"
    assert [n.code for n in pattern.nodes] == ["as_work"]
    node = pattern.node_map["as_work"]
    assert node.is_end is True
    assert node.sub_nodes == []
    # two-layer skill declaration + three-layer execution-side declaration
    assert pattern.allow_skills == ["archify"]
    assert node.use_skills == ["archify"]
    assert pattern.allow_toolset == ["shell", "filesystem"]
    assert node.use_tools == ["bash", "read_text", "write_text",
                              "edit_file", "find_files"]
    assert "load_skill" not in node.use_tools  # knowledge tool auto-granted, not in the declaration
    assert node.plugins["loop"] == "default_loop"  # zero custom executors

    from nexus.model.validation import validate_pattern
    validate_pattern(pattern)  # includes validate_skills (the stub skill
        # requires toolsets=[shell, filesystem] ⊆ allow_toolset)


def test_independent_from_workflow_version(pattern):
    """Independent from the archify workflow variant: binds no af_* executors,
    the node uses only default_loop; the two patterns coexist in the registry
    without affecting each other; the app source has zero references to
    archify_agent."""
    from nexus.registry.patterns import registry

    assert "archify" in registry.list_codes()  # the workflow variant still exists as-is
    assert pattern.node_map["as_work"].plugins["loop"] == "default_loop"
    # route.py's import surface: only prompts + nexus declaration models (no archify_agent)
    app_dir = Path(__file__).resolve().parents[1] / "apps" / \
        "archify_skill_agent"
    for path in app_dir.glob("*.py"):
        assert "apps.archify_agent" not in path.read_text(encoding="utf-8"), \
            path


# ============================================================================
# 2. One full-turn run
# ============================================================================

def test_full_turn_skill_driven(pattern, skill_root, tmp_path):
    ws = tmp_path / "ws"
    provider = SkillScriptedProvider(ws, skill_root)
    calls = []
    session, reply = run_turn(pattern, provider, make_bash_stub(calls))

    assert provider.rounds == 6
    sys0, tools0 = provider.requests[0]
    system0 = next(m["content"] for m in sys0 if m["role"] == "system")
    # L0 metadata injection (the description is the trigger)
    assert "可用技能" in system0
    assert "archify" in system0
    assert "演示用 archify 手册" in system0
    # knowledge tools auto-appended, coexisting with execution-side tools
    assert "load_skill" in tools0 and "read_skill_file" in tools0
    assert "bash" in tools0 and "write_text" in tools0

    # manual content enters model context (the round-2 request carries the load_skill receipt)
    sys1, _ = provider.requests[1]
    joined1 = json.dumps(sys1, ensure_ascii=False)
    assert "load_skill" in joined1 and "Archify（测试桩）" in joined1

    # bash execution side: validate + deliver, workdir = skill directory
    assert [c for c, _ in calls if "validate" in c]
    assert [c for c, _ in calls if "deliver" in c]
    assert {wd for _, wd in calls} == {str(skill_root)}

    # candidate really written to disk; the reply is the model's closing copy
    assert json.loads((ws / "ci-release.json").read_text(
        encoding="utf-8"))["meta"]["quality_profile"] == "showcase"
    assert "图表已交付" in reply

    # default_loop semantics: tool trace goes into session history; the single-node graph ends when done
    assert "tool" in [m.role for m in session.cxt.history]
    assert session.cxt.graph_state == {}
    assert session.cxt.current_node_code == "as_work"


# ============================================================================
# 3. Unauthorized interception and self-correction
# ============================================================================

def test_unenabled_skill_rejected_then_self_corrects(
        pattern, skill_root, tmp_path):
    ws = tmp_path / "ws"
    provider = SkillScriptedProvider(
        ws, skill_root,
        first_call=("load_skill", {"name": "not-a-skill"}, "c0"))
    calls = []
    session, reply = run_turn(pattern, provider, make_bash_stub(calls))

    # the error receipt is visible in the round-2 request (listing authorized skills), then archify is loaded in self-correction
    sys1, _ = provider.requests[1]
    joined1 = json.dumps(sys1, ensure_ascii=False)
    assert "未授权给本节点" in joined1
    assert provider.rounds == 7
    assert "图表已交付" in reply
    # the unauthorized call produced no manual content: the manual appears only after round 2 (the self-correction round)
    assert "Archify（测试桩）" not in json.dumps(
        provider.requests[0][0], ensure_ascii=False)
