"""R1/R3/R4 injection refresh: per-turn resolution by current position +
override priority (``get_llm_config(pattern_code,
node_code, override)``, the patch anchor stays "nexus.engine.chat.get_llm_config").

- R1: turn-level refresh (empty node_code before the position is known)
- R3: the FSM pipeline refreshes per node after entry resolution
- R4: the AGENT graph runtime refreshes per node at EVERY graph step
"""

from unittest.mock import patch

import atoms.executors  # noqa: F401 -- default executors registered
from async_utils import arun
from nexus.engine.chat import chat_turn
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.session import Session
from nexus.engine.turn_result import TurnResult
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.plugins import registry as plugin_registry


def _fsm_pattern():
    return Pattern(code="pf", name="t", description="t", pattern_type="fsm",
                   nodes=[BaseNode(code="f1", name="节点一"),
                          BaseNode(code="f2", name="节点二")])


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _record_calls(calls):
    import nexus.engine.chat as chat_mod
    real = chat_mod.get_llm_config

    def spy(pattern_code="", node_code="", override=None, config_path=""):
        calls.append(dict(pattern_code=pattern_code, node_code=node_code,
                          override=override))
        return real(pattern_code=pattern_code, node_code=node_code,
                    override=override, config_path=config_path)
    return spy


def _run_chat(sessions, sid, query, calls):
    with patch("nexus.engine.chat.get_llm_config",
               side_effect=_record_calls(calls)):
        from nexus.engine.chat import chat as chat_fn
        return arun(chat_fn(query=query, session_id=sid,
                            all_sessions=sessions))


def test_r1_passes_position_and_override():
    """R1: pattern_code + override all passed through, and metadata
    pattern_code written (node_code empty on the first turn — the position
    is not resolved yet)."""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        _run_chat(sessions, "s1", "你好", calls := [])
    assert calls, "R1 应调用 get_llm_config"
    first = calls[0]
    assert first["pattern_code"] == "pf"
    assert first["override"] == {"code": "x", "model": "m"}
    assert sessions["s1"].cxt.metadata["pattern_code"] == "pf"
    assert first["node_code"] in ("", "f1")


def test_r3_refresh_after_node_resolution():
    """R3: the FSM pipeline refreshes by pattern+node after entry-node
    resolution."""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        _run_chat(sessions, "s1", "你好", calls := [])
    r3 = [c for c in calls if c["pattern_code"] == "pf"
          and c["node_code"] == "f1"]
    assert r3, f"R3 应按 pattern=pf node=f1 解析，实际调用: {calls}"


def test_r4_graph_runtime_refreshes_per_node_step():
    """R4: the AGENT graph runtime refreshes the LLM config at EVERY node
    step (a node-level app overlay takes effect mid-run)."""

    class _RouteExecutor(NodeExecutor):
        async def execute(self, ec: ExecutionContext) -> TurnResult:
            return TurnResult(next="a2") if ec.node.code == "a1" \
                else TurnResult(content="done")

    plugin_registry.register("executor", "lr_route", _RouteExecutor)
    pattern = Pattern(code="pa", name="t", description="t", nodes=[
        BaseNode(code="a1", name="A", sub_nodes=["a2"],
                 plugins={"loop": "lr_route"}),
        BaseNode(code="a2", name="B", plugins={"loop": "lr_route"}),
    ])
    sessions = {}
    _launch(pattern, sessions, sid="s3")
    _run_chat(sessions, "s3", "你好", calls := [])

    refreshed = [c["node_code"] for c in calls if c["pattern_code"] == "pa"]
    # R1 (turn level) + one refresh per graph step: a1 then a2
    assert "a1" in refreshed and "a2" in refreshed, (
        f"R4 应在图每个节点步进处刷新（a1、a2），实际: {calls}")
    assert refreshed.index("a1") < refreshed.index("a2")
    assert calls[-1]["override"] == {"code": "x", "model": "m"}


def test_override_wins_and_survives_turns():
    """The override lands in cxt.llm_config and is not washed away across turns."""
    sessions = {}
    _launch(_fsm_pattern(), sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        _run_chat(sessions, "s1", "你好", [])
        _run_chat(sessions, "s1", "继续", [])
    assert sessions["s1"].cxt.llm_config["model"] == "m"
