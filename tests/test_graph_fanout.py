"""Runtime fan-out contract tests (including the heterogeneous fan-out
revision): sends dispatch / barrier join / results board (completion
order) / branch workspace isolation / heterogeneous targets / merge
intersection resolution (outlier workers ignored, non-uniquely-resolvable
targets rejected) / guards (next+sends mutual exclusion, width,
undeclared targets) / branch failure tolerance / budget accounting
(workers do not consume graph steps) / FSM rejects sends / event
vocabulary (fanout_* + branch_id tagging + graph_compile).

Drives chat_turn / chat_turn_stream with a scripted node executor (no LLM);
get_llm_config patched at the chat namespace (the R1/R4 patch-anchor
convention, same as test_graph_runtime).
"""

import asyncio
import inspect

import pytest
from unittest.mock import patch

import atoms.executors  # noqa: F401 -- warm executor codes
from async_utils import arun
from nexus.engine.chat import chat_turn, chat_turn_stream
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.session import Session
from nexus.engine.turn_result import Send, TurnResult
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.plugins import registry as plugin_registry

# Shared script: node_code -> handler(ec) -> TurnResult (sync or async both OK);
# _CALLS records the execution trace
_CALLS = []
_SCRIPT = {}


class _ScriptExecutor(NodeExecutor):
    async def execute(self, ec: ExecutionContext) -> TurnResult:
        _CALLS.append((ec.node.code, ec.branch_id, ec.branch_input))
        handler = _SCRIPT.get(ec.node.code)
        if handler is None:
            return TurnResult(content=f"done:{ec.node.code}")
        out = handler(ec)
        if inspect.iscoroutine(out):
            out = await out
        return out


plugin_registry.register("executor", "gft_script", _ScriptExecutor)


def _fanout_pattern(**kwargs) -> Pattern:
    """disp --sends--> work (sub_nodes=[join]) --> join (terminal node).

    disp.sub_nodes also declares join (a legal edge for the orphan-degradation
    direct route, same shape as deep_research).
    """
    defaults = dict(
        code="gft_graph",
        name="扇出测试",
        description="d",
        pattern_type="agent",
        nodes=[
            BaseNode(code="disp", name="D", sub_nodes=["work", "join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="work", name="W", sub_nodes=["join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="join", name="J", plugins={"loop": "gft_script"}),
        ],
    )
    defaults.update(kwargs)
    return Pattern(**defaults)


def _session(pattern: Pattern) -> Session:
    s = Session(session_id="gft_session", pattern_code=pattern.code)
    s.pattern = pattern
    s.cxt.node_map = pattern.node_map
    return s


def _turn(session: Session, query: str):
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        return arun(chat_turn(query, session.session_id,
                              {session.session_id: session}))


def _stream_turn(session: Session, query: str):
    """Consume chat_turn_stream, returning every ChatStreamEvent."""
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        async def _collect():
            events = []
            async for ev in chat_turn_stream(
                    query, session.session_id, {session.session_id: session}):
                events.append(ev)
            return events
        return arun(_collect())


@pytest.fixture(autouse=True)
def _reset():
    _CALLS.clear()
    _SCRIPT.clear()
    yield


# ---------------------------------------------------------------------------
# Main path: dispatch -> concurrent instances -> results board -> join
# ---------------------------------------------------------------------------

def test_fanout_runs_workers_and_joins():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", f"任务{i}") for i in (1, 2, 3)])
    _SCRIPT["work"] = lambda ec: TurnResult(content=f"结果:{ec.branch_input}")
    seen = {}

    def _join(ec):
        seen["board"] = list(ec.cxt.graph_state.get("__fanout_results__"))
        return TurnResult(content=f"汇总{len(seen['board'])}条")

    _SCRIPT["join"] = _join
    s = _session(_fanout_pattern())
    result = _turn(s, "调研一下")
    assert result.text == "汇总3条"

    codes = [c[0] for c in _CALLS]
    assert codes[0] == "disp" and codes[-1] == "join"
    assert sorted(codes[1:-1]) == ["work", "work", "work"]

    # results board entries: branch_id / ok / content (worker content only
    # lands on the board, never becomes the graph reply)
    board = seen["board"]
    assert len(board) == 3 and all(e["ok"] for e in board)
    assert {(e["branch_id"], e["content"]) for e in board} == {
        ("work#1", "结果:任务1"), ("work#2", "结果:任务2"),
        ("work#3", "结果:任务3"),
    }
    assert s.cxt.graph_state == {}  # graph termination clears the state board


def test_branch_input_delivery_and_workspace_isolation():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", {"q": "子问题"}),   # dict -> JSON-serialized as an explicit query
               Send("work", "纯文本任务")])     # str -> passed through as-is
    seen = []

    async def _work(ec):
        seen.append((ec.branch_id, ec.cxt.user_query, len(ec.cxt.history)))
        await ec.cxt.add_message("assistant", "分支内部产物", stage="agent")
        return TurnResult(content="ok")

    _SCRIPT["work"] = _work
    s = _session(_fanout_pattern())
    _turn(s, "主问题")
    queries = {bid: q for bid, q, _ in seen}
    assert queries["work#1"] == '{"q": "子问题"}'
    assert queries["work#2"] == "纯文本任务"
    # private workspaces: branches start with empty history; branch writes
    # never land in the main session history
    assert all(h == 0 for _, _, h in seen)
    assert all(row.content != "分支内部产物" for row in s.cxt.history)


def test_board_records_completion_order():
    delays = {"work#1": 0.03, "work#2": 0.0, "work#3": 0.012}
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", i) for i in (1, 2, 3)])

    async def _work(ec):
        await asyncio.sleep(delays[ec.branch_id])
        return TurnResult(content=ec.branch_id)

    _SCRIPT["work"] = _work
    seen = {}

    def _join(ec):
        seen["order"] = [e["branch_id"]
                         for e in ec.cxt.graph_state["__fanout_results__"]]
        return TurnResult(content="汇")

    _SCRIPT["join"] = _join
    s = _session(_fanout_pattern())
    _turn(s, "跑")
    assert seen["order"] == ["work#2", "work#3", "work#1"]


def test_workers_do_not_consume_graph_steps():
    # max_steps=3: disp (1 step) + join (1 step); 4 workers consume no steps -> completes within budget
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", i) for i in range(4)])
    _SCRIPT["work"] = lambda ec: TurnResult(content="w")
    _SCRIPT["join"] = lambda ec: TurnResult(content="join 收尾")
    pattern = _fanout_pattern(max_steps=3)
    s = _session(pattern)
    result = _turn(s, "跑")
    assert result.text == "join 收尾"
    assert len(_CALLS) == 6  # disp + 4 workers + join


# ---------------------------------------------------------------------------
# Heterogeneous fan-out and merge intersection resolution (§9 revision)
# ---------------------------------------------------------------------------

def _hetero_pattern(**kwargs) -> Pattern:
    """disp --sends--> work / agg (each with sub_nodes=[join]) --> join
    (terminal node).

    Heterogeneous shape: work = retrieval-type worker, agg = synthesis-type
    worker, sharing one join.
    """
    defaults = dict(
        code="gft_hetero",
        name="异构扇出测试",
        description="d",
        pattern_type="agent",
        nodes=[
            BaseNode(code="disp", name="D",
                     sub_nodes=["work", "agg", "join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="work", name="W", sub_nodes=["join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="agg", name="A", sub_nodes=["join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="join", name="J", plugins={"loop": "gft_script"}),
        ],
    )
    defaults.update(kwargs)
    return Pattern(**defaults)


def test_heterogeneous_targets_run_and_join():
    # mixed fan-out across different worker nodes: each executes, the results
    # board distinguishes by node_code, join fires once
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", "检索任务"), Send("agg", "归纳任务")])
    _SCRIPT["work"] = lambda ec: TurnResult(content=f"检索:{ec.branch_input}")
    _SCRIPT["agg"] = lambda ec: TurnResult(content=f"归纳:{ec.branch_input}")
    seen = {}

    def _join(ec):
        seen["board"] = list(ec.cxt.graph_state.get("__fanout_results__"))
        return TurnResult(content="汇总完成")

    _SCRIPT["join"] = _join
    s = _session(_hetero_pattern())
    result = _turn(s, "调研一下")
    assert result.text == "汇总完成"
    assert [c[0] for c in _CALLS] == ["disp", "work", "agg", "join"]

    board = seen["board"]
    assert len(board) == 2 and all(e["ok"] for e in board)
    assert {(e["node_code"], e["content"]) for e in board} == {
        ("work", "检索:检索任务"), ("agg", "归纳:归纳任务")}
    # branch_id stays {node_code}#{global sequence}, unique across workers
    assert {e["branch_id"] for e in board} == {"work#1", "agg#2"}
    assert s.cxt.graph_state == {}


def test_heterogeneous_mixed_width_runs_each_target():
    # multiple instances of one worker mixed with a single instance of
    # another; width counted by total instance count
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", 1), Send("agg", "a"), Send("work", 2)])
    _SCRIPT["work"] = lambda ec: TurnResult(content=f"w{ec.branch_input}")
    _SCRIPT["agg"] = lambda ec: TurnResult(content="agg")
    seen = {}

    def _join(ec):
        seen["board"] = list(ec.cxt.graph_state.get("__fanout_results__"))
        return TurnResult(content="join")

    _SCRIPT["join"] = _join
    s = _session(_hetero_pattern())
    result = _turn(s, "跑")
    assert result.text == "join"
    assert [c[0] for c in _CALLS][0] == "disp" and _CALLS[-1][0] == "join"
    assert sorted(c[0] for c in _CALLS[1:-1]) == ["agg", "work", "work"]
    assert {e["branch_id"] for e in seen["board"]} == {
        "work#1", "agg#2", "work#3"}


def test_join_target_send_dropped_as_outlier():
    # sending directly to join (no successors -> no common merge): the sole
    # outlier -> ignored, the rest proceed as usual
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", "a"), Send("join", "b")])
    _SCRIPT["work"] = lambda ec: TurnResult(content="结果a")
    s = _session(_fanout_pattern())
    result = _turn(s, "跑")
    assert result.text == "done:join"
    assert [c[0] for c in _CALLS] == ["disp", "work", "join"]
    # outlier drops land in the fanout_start actions snapshot
    assert any(a.get("fanout_start", {}).get("dropped") == ["join"]
               for a in result.actions)
    assert any(a.get("fanout_start", {}).get("branches") == 1
               for a in result.actions)


def test_outlier_worker_with_foreign_merge_dropped():
    # w1/w2 -> join, w3 -> other: w3 is dropped as an outlier, w1/w2 execute, join fires normally
    pattern = Pattern(
        code="gft_outlier", name="离群忽略", description="d",
        pattern_type="agent",
        nodes=[
            BaseNode(code="disp", sub_nodes=["w1", "w2", "w3"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w1", sub_nodes=["join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w2", sub_nodes=["join"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w3", sub_nodes=["other"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="join", plugins={"loop": "gft_script"}),
            BaseNode(code="other", plugins={"loop": "gft_script"}),
        ],
    )
    _SCRIPT["disp"] = lambda ec: TurnResult(sends=[
        Send("w1", "a"), Send("w2", "b"), Send("w3", "c")])
    seen = {}

    def _join(ec):
        seen["board"] = list(ec.cxt.graph_state.get("__fanout_results__"))
        return TurnResult(content="join 完成")

    _SCRIPT["join"] = _join
    s = _session(pattern)
    result = _turn(s, "跑")
    assert result.text == "join 完成"
    assert [c[0] for c in _CALLS] == ["disp", "w1", "w2", "join"]  # w3 never executed
    assert {e["node_code"] for e in seen["board"]} == {"w1", "w2"}
    assert any(a.get("fanout_start", {}).get("dropped") == ["w3"]
               for a in result.actions)


def test_disjoint_merges_raise():
    # the two workers point at different merges (two outliers) -> refuse to
    # execute, flagging the template bug
    pattern = Pattern(
        code="gft_disjoint", name="无共同merge", description="d",
        pattern_type="agent",
        nodes=[
            BaseNode(code="disp", sub_nodes=["w1", "w2"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w1", sub_nodes=["j1"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w2", sub_nodes=["j2"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="j1", plugins={"loop": "gft_script"}),
            BaseNode(code="j2", plugins={"loop": "gft_script"}),
        ],
    )
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("w1", "a"), Send("w2", "b")])
    s = _session(pattern)
    result = _turn(s, "跑")
    assert result.text == "对话处理异常，请稍后重试"
    assert [c[0] for c in _CALLS] == ["disp"]  # no worker executes at all


def test_ambiguous_common_merge_raises():
    # the common successor is not unique (w1/w2 both declare join+extra) -> refuse to execute
    pattern = Pattern(
        code="gft_ambiguous", name="merge不唯一", description="d",
        pattern_type="agent",
        nodes=[
            BaseNode(code="disp", sub_nodes=["w1", "w2"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w1", sub_nodes=["join", "extra"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="w2", sub_nodes=["join", "extra"],
                     plugins={"loop": "gft_script"}),
            BaseNode(code="join", plugins={"loop": "gft_script"}),
            BaseNode(code="extra", plugins={"loop": "gft_script"}),
        ],
    )
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("w1", "a"), Send("w2", "b")])
    s = _session(pattern)
    result = _turn(s, "跑")
    assert result.text == "对话处理异常，请稍后重试"
    assert [c[0] for c in _CALLS] == ["disp"]


# ---------------------------------------------------------------------------
# Guards: mutual exclusion / width / undeclared targets / join resolvability / FSM
# ---------------------------------------------------------------------------

def test_next_and_sends_mutex_raises():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        next="join", sends=[Send("work", "x")])
    s = _session(_fanout_pattern())
    result = _turn(s, "跑")
    assert result.text == "对话处理异常，请稍后重试"
    assert [c[0] for c in _CALLS] == ["disp"]


def test_fanout_width_over_max_raises():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", i) for i in range(3)])
    s = _session(_fanout_pattern(max_fanout=2))
    result = _turn(s, "跑")
    assert result.text == "对话处理异常，请稍后重试"
    assert [c[0] for c in _CALLS] == ["disp"]


def test_undeclared_target_terminates_tolerantly():
    # target not in sub_nodes -> tolerant termination of the same family as
    # an undeclared next edge (no raise)
    _SCRIPT["disp"] = lambda ec: TurnResult(
        content="派发前的话", sends=[Send("ghost", "x")])
    s = _session(_fanout_pattern())
    result = _turn(s, "跑")
    assert result.text == "派发前的话"
    assert [c[0] for c in _CALLS] == ["disp"]
    assert s.cxt.graph_state == {}


def test_join_unresolvable_raises():
    # a single worker declaring two successors -> the common successor is
    # not unique, the merge is unresolvable
    pattern = _fanout_pattern(nodes=[
        BaseNode(code="disp", sub_nodes=["work"],
                 plugins={"loop": "gft_script"}),
        BaseNode(code="work", sub_nodes=["join", "other"],
                 plugins={"loop": "gft_script"}),
        BaseNode(code="join", plugins={"loop": "gft_script"}),
        BaseNode(code="other", plugins={"loop": "gft_script"}),
    ])
    _SCRIPT["disp"] = lambda ec: TurnResult(sends=[Send("work", "x")])
    s = _session(pattern)
    result = _turn(s, "跑")
    assert result.text == "对话处理异常，请稍后重试"


def test_fsm_rejects_sends():
    class _FsmStub(NodeExecutor):
        async def execute(self, ec) -> TurnResult:
            return TurnResult(content="不该到达", sends=[Send("f1", "x")])

    plugin_registry.register("executor", "gft_fsm_stub", _FsmStub)
    pattern = Pattern(
        code="gft_fsm", name="fsm", description="d", pattern_type="fsm",
        nodes=[BaseNode(code="f1", name="F1")],
        plugins={"fsm": "gft_fsm_stub"},
        stages=[{"nlu": None}])
    s = _session(pattern)
    result = _turn(s, "问一句")
    assert result.text == "对话处理异常，请稍后重试"


def test_pattern_max_fanout_defaults_and_folding():
    assert _fanout_pattern().max_fanout == 8
    assert _fanout_pattern(config={"max_fanout": 3}).max_fanout == 3
    assert _fanout_pattern(max_fanout=5).max_fanout == 5     # kwargs sugar
    assert _fanout_pattern(max_fanout=0).max_fanout == 8     # invalid values fall back to the default


# ---------------------------------------------------------------------------
# Failure semantics: a failed branch = an error entry, join still fires;
# wait_human / nesting count as branch failures
# ---------------------------------------------------------------------------

def test_branch_failure_settles_error_and_join_fires():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", "好任务"), Send("work", "坏任务")])

    async def _work(ec):
        if ec.branch_input == "坏任务":
            raise RuntimeError("工具炸了")
        return TurnResult(content="好结果")

    _SCRIPT["work"] = _work
    seen = {}

    def _join(ec):
        seen["board"] = list(ec.cxt.graph_state["__fanout_results__"])
        return TurnResult(content="部分成功")

    _SCRIPT["join"] = _join
    s = _session(_fanout_pattern())
    result = _turn(s, "跑")
    assert result.text == "部分成功"  # a single branch failure does not kill the graph
    board = {(e["branch_id"]): e for e in seen["board"]}
    bad = [e for e in seen["board"] if not e["ok"]]
    assert len(bad) == 1 and "工具炸了" in bad[0]["error"]
    assert any(a.get("fanout_join", {}).get("failed") == 1
               for a in result.actions)


def test_branch_wait_human_is_branch_failure():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", "a"), Send("work", "b")])

    async def _work(ec):
        if ec.branch_input == "a":
            return TurnResult(content="等人工", wait_human=True)
        return TurnResult(content="ok")

    _SCRIPT["work"] = _work
    seen = {}

    def _join(ec):
        seen["board"] = list(ec.cxt.graph_state["__fanout_results__"])
        return TurnResult(content="join 完成")

    _SCRIPT["join"] = _join
    s = _session(_fanout_pattern())
    result = _turn(s, "跑")
    assert result.text == "join 完成"          # does not pause
    assert s.cxt.graph_state == {}             # no paused cursor
    bad = [e for e in seen["board"] if not e["ok"]]
    assert len(bad) == 1 and "wait_human" in bad[0]["error"]


# ---------------------------------------------------------------------------
# Event surface: fanout_* vocabulary + branch_id tagging + graph_compile + actions snapshot
# ---------------------------------------------------------------------------

def test_fanout_event_vocabulary_and_branch_tagging():
    _SCRIPT["disp"] = lambda ec: TurnResult(
        sends=[Send("work", "a"), Send("work", "b")])

    async def _work(ec):
        if ec.stream is not None:
            ec.stream.emit_delta("分支增量")
            ec.stream.emit_round("final", 0)
        return TurnResult(content="ok")

    _SCRIPT["work"] = _work
    _SCRIPT["join"] = lambda ec: TurnResult(content="join")
    s = _session(_fanout_pattern())
    events = _stream_turn(s, "跑")

    traces = [ev.trace.to_dict() for ev in events
              if ev.kind == "trace" and ev.trace is not None]
    names = [t["event"] for t in traces]
    assert "graph_compile" in names
    assert names.count("fanout_start") == 1
    assert names.count("branch_start") == 2
    assert names.count("branch_end") == 2
    assert names.count("fanout_join") == 1

    starts = [t for t in traces if t["event"] == "branch_start"]
    assert {t["branch_id"] for t in starts} == {"work#1", "work#2"}
    fj = [t for t in traces if t["event"] == "fanout_join"][0]
    assert fj["data"]["total"] == 2 and fj["data"]["failed"] == 0

    deltas = [ev for ev in events if ev.kind == "delta"]
    assert deltas and all(ev.branch_id.startswith("work#") for ev in deltas)
    rounds = [ev for ev in events if ev.kind == "round"]
    assert rounds and all(ev.branch_id for ev in rounds)


def test_actions_snapshot_fanout_summary():
    _SCRIPT["disp"] = lambda ec: TurnResult(sends=[Send("work", "x")])
    _SCRIPT["work"] = lambda ec: TurnResult(content="w")
    _SCRIPT["join"] = lambda ec: TurnResult(content="join")
    s = _session(_fanout_pattern())
    result = _turn(s, "跑")
    assert any(a.get("fanout_start", {}).get("branches") == 1
               for a in result.actions)
    assert any(a.get("fanout_join", {}).get("total") == 1
               for a in result.actions)
