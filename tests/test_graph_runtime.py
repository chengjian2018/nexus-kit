"""AGENT graph runtime tests: routing via TurnResult.next /
max_steps budget / wait_human suspend & resume / undeclared-edge
termination / pattern_type dispatch (fsm vs agent).

Drives chat_turn with a scripted node executor (no LLM); get_llm_config is
patched at the chat namespace (the R1/R4 patch-anchor convention).
"""

import pytest
from unittest.mock import patch

import atoms.executors  # noqa: F401 -- warm executor codes
from async_utils import arun
from nexus.engine.chat import chat_turn
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.session import Session
from nexus.engine.turn_result import TurnResult
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.plugins import registry as plugin_registry

# Shared script: node_code -> callable(ec) -> TurnResult; calls record the execution trail
_CALLS = []
_SCRIPT = {}


class _ScriptExecutor(NodeExecutor):
    async def execute(self, ec: ExecutionContext) -> TurnResult:
        _CALLS.append((ec.node.code, ec.resume_input, ec.step))
        handler = _SCRIPT.get(ec.node.code)
        if handler is None:
            return TurnResult(content=f"done:{ec.node.code}")
        return handler(ec)


plugin_registry.register("executor", "grt_script", _ScriptExecutor)


def _graph_pattern(**kwargs) -> Pattern:
    defaults = dict(
        code="grt_graph",
        name="图测试",
        description="d",
        pattern_type="agent",
        nodes=[
            BaseNode(code="n1", name="N1", sub_nodes=["n2"],
                     plugins={"loop": "grt_script"}),
            BaseNode(code="n2", name="N2", sub_nodes=["n3"],
                     plugins={"loop": "grt_script"}),
            BaseNode(code="n3", name="N3",
                     plugins={"loop": "grt_script"}),
        ],
    )
    defaults.update(kwargs)
    return Pattern(**defaults)


def _session(pattern: Pattern) -> Session:
    s = Session(session_id="grt_session", pattern_code=pattern.code)
    s.pattern = pattern
    s.cxt.node_map = pattern.node_map
    return s


def _turn(session: Session, query: str):
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        return arun(chat_turn(query, session.session_id,
                              {session.session_id: session}))


@pytest.fixture(autouse=True)
def _reset():
    _CALLS.clear()
    _SCRIPT.clear()
    yield


# ---------------------------------------------------------------------------
# Routing: the next output maps onto sub_nodes
# ---------------------------------------------------------------------------

def test_linear_routing_runs_whole_graph():
    _SCRIPT["n1"] = lambda ec: TurnResult(next="n2", content="路由中")
    _SCRIPT["n2"] = lambda ec: TurnResult(next="n3")
    # n3 has no handler -> default content done:n3, and no sub_nodes -> terminate
    s = _session(_graph_pattern())
    result = _turn(s, "你好")
    assert result.text == "done:n3"  # the last non-empty content becomes the reply
    assert [c[0] for c in _CALLS] == ["n1", "n2", "n3"]
    assert s.cxt.graph_state == {}  # graph termination clears the state board


def test_no_next_with_successors_terminates():
    # n1 has successors but the executor routes nowhere -> graph terminates (warning semantics)
    s = _session(_graph_pattern())
    result = _turn(s, "你好")
    assert [c[0] for c in _CALLS] == ["n1"]
    assert result.text == "done:n1"


def test_undeclared_edge_terminates():
    _SCRIPT["n1"] = lambda ec: TurnResult(next="n3", content="n1 的回复")
    s = _session(_graph_pattern())
    result = _turn(s, "你好")
    assert [c[0] for c in _CALLS] == ["n1"]
    assert result.text == "n1 的回复"


def test_list_next_consumes_first_serially():
    _SCRIPT["n1"] = lambda ec: TurnResult(next=["n2"])  # fan-out extension slot: consumed serially
    s = _session(_graph_pattern())
    _turn(s, "你好")
    assert [c[0] for c in _CALLS][:2] == ["n1", "n2"]


def test_is_end_short_circuits():
    _SCRIPT["n1"] = lambda ec: TurnResult(next="n2")
    pattern = _graph_pattern(nodes=[
        BaseNode(code="n1", sub_nodes=["n2"], plugins={"loop": "grt_script"}),
        BaseNode(code="n2", is_end=True, plugins={"loop": "grt_script"}),
        BaseNode(code="n3", plugins={"loop": "grt_script"}),
    ])
    # n2 declaring next=n3 is still short-circuited by is_end
    _SCRIPT["n2"] = lambda ec: TurnResult(next="n3", content="结束")
    s = _session(pattern)
    result = _turn(s, "你好")
    assert [c[0] for c in _CALLS] == ["n1", "n2"]
    assert result.text == "结束"


# ---------------------------------------------------------------------------
# Budget: max_steps cycle guard
# ---------------------------------------------------------------------------

def test_max_steps_budget_on_cycle():
    # self-loop graph + always routing -> forced close after max_steps
    pattern = _graph_pattern(
        max_steps=3,
        nodes=[BaseNode(code="loop_node", sub_nodes=["loop_node"],
                        plugins={"loop": "grt_script"})])
    _SCRIPT["loop_node"] = lambda ec: TurnResult(
        next="loop_node", content=f"步进{ec.step}")
    s = _session(pattern)
    result = _turn(s, "跑")
    assert len(_CALLS) == 3  # exactly max_steps executions
    assert result.text == "步进2"  # the last non-empty content
    assert s.cxt.graph_state == {}


def test_max_steps_exhaustion_fallback_reply():
    pattern = _graph_pattern(
        max_steps=2,
        nodes=[BaseNode(code="loop_node", sub_nodes=["loop_node"],
                        plugins={"loop": "grt_script"})])
    _SCRIPT["loop_node"] = lambda ec: TurnResult(next="loop_node")
    s = _session(pattern)
    result = _turn(s, "跑")
    assert "抱歉" in result.text  # fallback copy when there is no content at all


# ---------------------------------------------------------------------------
# Suspend/resume: wait_human (langgraph interrupt semantics)
# ---------------------------------------------------------------------------

def test_wait_human_suspends_and_resumes():
    def _n1(ec):
        if ec.resume_input is None:
            return TurnResult(content="请提供审批意见", wait_human=True)
        return TurnResult(next="n2")  # after resume, continue via next
    _SCRIPT["n1"] = _n1
    # n2 has no handler -> done:n2
    pattern = _graph_pattern()
    s = _session(pattern)

    result = _turn(s, "开始审批")
    assert result.text == "请提供审批意见"
    assert s.cxt.graph_state.get("__paused_node__") == "n1"
    assert s.cxt.graph_state.get("__step__") == 1
    assert any(a.get("graph_wait") for a in result.actions)

    # resume turn: the user message becomes the waiting node's resume_input; the node re-executes then continues via next
    result2 = _turn(s, "同意，继续")
    assert result2.text == "done:n2"
    resumed = [c for c in _CALLS if c[0] == "n1"]
    assert resumed[-1][1] == "同意，继续"  # resume_input delivered
    assert s.cxt.graph_state == {}  # cleared after running through to the end node


def test_resume_reruns_node_and_step_accounting():
    # step accounting persists across turns while suspended: max_steps=2,
    # suspended at step=0 -> only 1 step of budget remains after resume
    _SCRIPT["n1"] = lambda ec: TurnResult(content="等输入", wait_human=True)
    pattern = _graph_pattern(
        max_steps=2,
        nodes=[BaseNode(code="n1", sub_nodes=["n1"],
                        plugins={"loop": "grt_script"})])
    s = _session(pattern)
    _turn(s, "第一轮")
    assert s.cxt.graph_state.get("__step__") == 1
    # resume turn: waits again -> step increments to 2
    result2 = _turn(s, "第二轮")
    assert s.cxt.graph_state.get("__step__") == 2
    # third turn: step=2 has reached the budget -> forced close (nodes no longer execute)
    calls_before = len(_CALLS)
    result3 = _turn(s, "第三轮")
    assert len(_CALLS) == calls_before
    assert "抱歉" in result3.text


# ---------------------------------------------------------------------------
# pattern_type dispatch: FSM goes through the fsm slot executor
# ---------------------------------------------------------------------------

def test_fsm_dispatch_uses_fsm_slot_executor():
    class _FsmStub(NodeExecutor):
        async def execute(self, ec) -> TurnResult:
            _CALLS.append(("fsm_stub", ec.pattern.pattern_type, None))
            return TurnResult(content="fsm 回复")

    plugin_registry.register("executor", "grt_fsm_stub", _FsmStub)
    pattern = Pattern(
        code="grt_fsm", name="fsm", description="d", pattern_type="fsm",
        nodes=[BaseNode(code="f1", name="F1")],
        plugins={"fsm": "grt_fsm_stub"},
        stages=[{"nlu": None}])
    s = _session(pattern)
    result = _turn(s, "问一句")
    assert result.text == "fsm 回复"
    assert _CALLS and _CALLS[0][0] == "fsm_stub"
    assert _CALLS[0][1] == "fsm"


# ---------------------------------------------------------------------------
# Executor resolution chain: node.plugins.loop > pattern.plugins.loop
# ---------------------------------------------------------------------------

def test_node_loop_plugin_overrides_pattern():
    class _PatternLevel(NodeExecutor):
        async def execute(self, ec) -> TurnResult:
            _CALLS.append(("pattern_level", None, None))
            return TurnResult(content="pattern 级回复")

    plugin_registry.register("executor", "grt_pattern_level", _PatternLevel)
    pattern = Pattern(
        code="grt_pl", name="n", description="d",
        plugins={"loop": "grt_pattern_level"},
        nodes=[
            BaseNode(code="a", sub_nodes=["b"], plugins={"loop": "grt_script"}),
            BaseNode(code="b"),
        ])
    _SCRIPT["a"] = lambda ec: TurnResult(next="b")
    s = _session(pattern)
    result = _turn(s, "你好")
    # node a uses its own grt_script (routes to b); b has no declaration -> pattern-level executor
    assert ("pattern_level", None, None) in _CALLS
    assert result.text == "pattern 级回复"
