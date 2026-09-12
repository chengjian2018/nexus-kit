"""Trace-event streaming tests — graph / node / tool observability (plan-⑧).

chat_turn_stream emits kind="trace" events (nexus.engine.streaming.
TraceEvent) at every state transition a CLI consumer wants to see live:

- FSM    : node_jump (only when the node moved) + conversation_end + the
           reply as a single delta (stages are one-shot, no token stream)
- AGENT  : node_start / node_end per graph step (with node_code + step),
           graph_wait / graph_resume around a wait_human suspension,
           graph_done on termination (terminal / is_end / max_steps /
           undeclared_edge), plus tool_call / tool_result per dispatch
           (synthetic flag marks the intercepted-backfill rows)

The module-jump family (module_jump / route_hit / route_root / defer_switch
/ module_start) is gone with the module layer. FSM stay-in-place turns emit
no trace (suppression contract). chat()/chat_turn aggregation ignores trace
events, so the compat entries are untouched (asserted for the FSM shape).
"""

import json
from unittest.mock import patch

import atoms.executors  # noqa: F401 -- default executors registered
import atoms.stages  # noqa: F401 -- default stages registered
from async_utils import arun
from nexus.engine.chat import chat_turn, chat_turn_stream
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.session import Session
from nexus.engine.turn_result import TurnResult
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.plugins import registry as plugin_registry


# ============================================================================
# Fixtures
# ============================================================================

class _ScriptedProvider:
    """Non-streaming provider serving a FIFO queue of complete replies."""

    def __init__(self, replies):
        self.replies = list(replies)

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, **kwargs):
        return {"content": self.replies.pop(0), "tool_calls": [],
                "finish_reason": "stop"}


class _StreamProvider:
    """Streaming provider: rounds of LLMChunk lists (mirrors test_chat_stream)."""

    def __init__(self, rounds):
        from nexus.llm.types import LLMChunk
        self.rounds = [
            [LLMChunk(text=t, tool_calls=tc, finish_reason=fr)
             for (t, tc, fr) in rnd]
            for rnd in rounds
        ]

    async def achat_completion_stream(self, messages, model, temperature=0.7,
                                      max_tokens=2048, **kwargs):
        for chunk in self.rounds.pop(0):
            yield chunk


def _bind(session: Session, pattern: Pattern) -> Session:
    session.pattern = pattern
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    session.cxt.llm_config = {"code": "x", "model": "m"}
    return session


def _fsm_pattern() -> Pattern:
    n1 = BaseNode(code="n1", name="收集", sub_nodes=["n2"])
    n2 = BaseNode(code="n2", name="结束", is_end=True)
    return Pattern(code="fp", name="t", description="t", pattern_type="fsm",
                   nodes=[n1, n2],
                   stages=[{"nlu": "fsm_unified"}, {"nlg": "nlg_pass_through"}])


def _agent_session():
    p = Pattern(code="sp", name="t", description="t",
                nodes=[BaseNode(code="n1", name="主节点")])
    return _bind(Session(session_id="as", pattern_code="sp"), p)


def _collect(agen):
    async def _run():
        return [e async for e in agen]
    return arun(_run())


def _kinds(events):
    """[(kind, trace.event?)] — the shape assertions below pin exact order."""
    return [(e.kind, getattr(e.trace, "event", None)) for e in events]


class _RawProvider:
    """Returns scripted full responses as-is (dicts with content/tool_calls)."""

    def __init__(self, script):
        self.script = list(script)

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, **kwargs):
        return self.script.pop(0)


def _tc(name="noop", args="{}", cid="c1"):
    return {"index": 0, "id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


# ============================================================================
# FSM: node_jump + conversation_end + one-shot reply delta
# ============================================================================

def test_fsm_node_jump_turn_event_sequence():
    s = _bind(Session(session_id="fs", pattern_code="fp"), _fsm_pattern())
    reply_json = json.dumps({"reply": "好的已记录", "next_node": "n2",
                             "slots": {"a": "1"}}, ensure_ascii=False)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        events = _collect(chat_turn_stream("q", "fs", {"fs": s}))

    assert _kinds(events) == [("trace", "node_jump"),
                              ("trace", "conversation_end"),
                              ("delta", None), ("done", None)]
    jump = events[0].trace
    assert jump.node_code == "n2"
    assert jump.data["from_node"] == "n1"
    assert jump.data["to_node"] == "n2"
    assert "".join(e.text for e in events if e.kind == "delta") == "好的已记录"
    assert events[-1].result.text == "好的已记录"
    assert s.cxt.current_node_code == "n2"


def test_fsm_stay_turn_emits_no_trace():
    """Stay-in-place turn: delta + done only (suppression contract)."""
    s = _bind(Session(session_id="fs", pattern_code="fp"), _fsm_pattern())
    reply_json = json.dumps({"reply": "请补充", "next_node": "",
                             "slots": {}}, ensure_ascii=False)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        events = _collect(chat_turn_stream("q", "fs", {"fs": s}))
    assert _kinds(events) == [("delta", None), ("done", None)]


def test_fsm_chat_compat_untouched_by_trace():
    """chat_turn aggregation ignores trace events — the compat entry's reply
    is byte-identical to done.result.text (no conflict with chat())."""
    p = _fsm_pattern()
    reply_json = json.dumps({"reply": "好的已记录", "next_node": "n2",
                             "slots": {}}, ensure_ascii=False)
    s1 = _bind(Session(session_id="fs", pattern_code="fp"), p)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        events = _collect(chat_turn_stream("q", "fs", {"fs": s1}))
    s2 = _bind(Session(session_id="fs", pattern_code="fp"), p)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        result = arun(chat_turn("q", "fs", {"fs": s2}))
    assert events[-1].result.text == result.text == "好的已记录"


# ============================================================================
# AGENT graph: node_start / node_end / graph_done per step
# ============================================================================

def test_agent_node_step_events_around_the_loop():
    """A single-node AGENT turn: node_start → (loop's delta/round) → node_end
    → graph_done(terminal). The node events carry node_code + step."""
    provider = _StreamProvider([[("直接答复", [], "stop")]])
    s = _agent_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = _collect(chat_turn_stream("q", "as", {"as": s}))

    assert _kinds(events) == [("trace", "graph_compile"),
                              ("trace", "node_start"),
                              ("delta", None),
                              ("round", None),
                              ("trace", "node_end"),
                              ("trace", "graph_done"),
                              ("done", None)]
    start, end, done = events[1].trace, events[4].trace, events[5].trace
    assert (start.node_code, start.data["step"]) == ("n1", 0)
    assert (end.node_code, end.data["step"]) == ("n1", 0)
    assert done.data["reason"] == "terminal"
    assert events[-1].result.text == "直接答复"


def test_agent_tool_round_traces():
    """Tool round: delta → tool_call → tool_result (synthetic: intercepted
    backfill, the node declares no tools) → round(tool) → final round."""
    provider = _StreamProvider([
        [("让我查查", [], ""), ("", [_tc()], "tool_calls")],
        [("答案是42", [], "stop")],
    ])
    s = _agent_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = _collect(chat_turn_stream("q", "as", {"as": s}))

    assert _kinds(events) == [("trace", "graph_compile"),
                              ("trace", "node_start"),
                              ("delta", None), ("trace", "tool_call"),
                              ("trace", "tool_result"), ("round", None),
                              ("delta", None), ("round", None),
                              ("trace", "node_end"),
                              ("trace", "graph_done"),
                              ("done", None)]
    call = events[3].trace
    result = events[4].trace
    assert call.data["tool_name"] == "noop"
    assert call.data["args"] == {}
    assert call.data["round_idx"] == 0
    assert call.node_code == "n1"
    assert result.data["tool_name"] == "noop"
    assert result.data["synthetic"] is True
    assert "不存在" in result.data["result"]
    assert events[-1].result.text == "答案是42"


# ============================================================================
# AGENT graph: graph_wait / graph_resume around a wait_human suspension
# ============================================================================

_WAIT_SCRIPT = {}


class _WaitScriptExecutor(NodeExecutor):
    """Scripted node executor: node n1 waits for human input on the first
    execution and routes to n2 on resume; n2 replies done."""

    async def execute(self, ec: ExecutionContext) -> TurnResult:
        if ec.node.code == "n1":
            if ec.resume_input is None:
                return TurnResult(content="请提供审批意见", wait_human=True)
            return TurnResult(next="n2")
        return TurnResult(content="done:n2")


plugin_registry.register("executor", "te_wait_script", _WaitScriptExecutor)


def _wait_graph_session():
    p = Pattern(code="wg", name="t", description="t",
                nodes=[BaseNode(code="n1", name="审批", sub_nodes=["n2"],
                                plugins={"loop": "te_wait_script"}),
                       BaseNode(code="n2", name="完成",
                                plugins={"loop": "te_wait_script"})])
    return _bind(Session(session_id="wg", pattern_code="wg"), p)


def test_graph_wait_then_resume_trace_events():
    s = _wait_graph_session()
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        events = _collect(chat_turn_stream("开始", "wg", {"wg": s}))

    # suspension turn: graph_compile → node_start → node_end → graph_wait
    # → done（恢复轮不重发 graph_compile，以 graph_resume 开头）
    assert _kinds(events) == [("trace", "graph_compile"),
                              ("trace", "node_start"),
                              ("trace", "node_end"),
                              ("trace", "graph_wait"),
                              ("done", None)]
    wait = events[3].trace
    assert (wait.node_code, wait.data["step"]) == ("n1", 0)
    assert events[-1].result.text == "请提供审批意见"

    # resume turn: graph_resume (carrying the paused node + step) → the node
    # re-executes → routes to n2 → graph_done(terminal)
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        events2 = _collect(chat_turn_stream("同意", "wg", {"wg": s}))
    assert _kinds(events2) == [("trace", "graph_resume"),
                              ("trace", "node_start"),
                              ("trace", "node_end"),
                              ("trace", "node_start"),
                              ("trace", "node_end"),
                              ("trace", "graph_done"),
                              ("done", None)]
    resume = events2[0].trace
    assert (resume.node_code, resume.data["step"]) == ("n1", 1)
    assert events2[-1].result.text == "done:n2"


def test_max_steps_graph_done_reason():
    """A routing cycle exhausts max_steps → graph_done carries reason
    max_steps and the force-close reply is delivered."""
    class _CycleExecutor(NodeExecutor):
        async def execute(self, ec: ExecutionContext) -> TurnResult:
            return TurnResult(content="步进", next="c1")

    plugin_registry.register("executor", "te_cycle", _CycleExecutor)
    p = Pattern(code="cy", name="t", description="t", max_steps=2,
                nodes=[BaseNode(code="c1", name="环", sub_nodes=["c1"],
                                plugins={"loop": "te_cycle"})])
    s = _bind(Session(session_id="cy", pattern_code="cy"), p)
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        events = _collect(chat_turn_stream("跑", "cy", {"cy": s}))
    dones = [t for k, t in _kinds(events) if t == "graph_done"]
    assert dones
    graph_done = next(e.trace for e in events
                      if getattr(e.trace, "event", "") == "graph_done")
    assert graph_done.data["reason"] == "max_steps"
    assert graph_done.data["step"] == 2
    assert events[-1].result.text == "步进"


# ============================================================================
# TraceEvent wire shape + CLI rendering (host.cli)
# ============================================================================

def test_trace_event_to_dict_wire_shape():
    from nexus.engine.streaming import TraceEvent

    d = TraceEvent("node_jump", node_code="n2",
                   data={"from_node": "n1", "to_node": "n2"}).to_dict()
    assert d == {"event": "node_jump", "module_code": "",
                 "node_code": "n2", "data": {"from_node": "n1",
                                             "to_node": "n2"}}
    # node_code/data omitted when empty (compact wire form)
    assert TraceEvent("node_start", node_code="n1").to_dict() == {
        "event": "node_start", "module_code": "", "node_code": "n1"}


def test_render_trace_event_lines():
    from host.cli import render_trace_event
    from nexus.engine.streaming import TraceEvent

    renderings = {
        "node_start": TraceEvent("node_start", node_code="n1",
                                 data={"step": 0}),
        "node_end": TraceEvent("node_end", node_code="n1",
                               data={"step": 0}),
        "node_jump": TraceEvent("node_jump", data={
            "from_node": "n1", "to_node": "n2"}),
        "graph_wait": TraceEvent("graph_wait", node_code="n1",
                                 data={"step": 2}),
        "graph_resume": TraceEvent("graph_resume", node_code="n1",
                                   data={"step": 3}),
        "graph_done": TraceEvent("graph_done", data={
            "reason": "is_end", "step": 1}),
        "tool_call": TraceEvent("tool_call", data={
            "tool_name": "search", "args": {"q": "订单"}}),
        "tool_result": TraceEvent("tool_result", data={
            "tool_name": "search", "result": "找到了"}),
        "conversation_end": TraceEvent("conversation_end", node_code="n9"),
        "custom_thing": TraceEvent("custom_thing", node_code="x"),
    }
    lines = {k: render_trace_event(v) for k, v in renderings.items()}
    assert "开始 n1（step 0）" in lines["node_start"]
    assert "结束 n1（step 0）" in lines["node_end"]
    assert "n1 → n2" in lines["node_jump"]
    assert "挂起等待人工输入: n1（step 2）" in lines["graph_wait"]
    assert "从挂起恢复: n1（step 3）" in lines["graph_resume"]
    assert "图运行结束（终节点，step 1）" in lines["graph_done"]
    assert "[tool_call] search" in lines["tool_call"]
    assert "订单" in lines["tool_call"]
    assert "[tool_result] search: 找到了" in lines["tool_result"]
    assert "到达终节点 n9" in lines["conversation_end"]
    # open set: unknown names render generically instead of raising
    assert "[custom_thing] x" in lines["custom_thing"]


def test_stream_event_printer_line_discipline():
    from host.cli import StreamEventPrinter
    from nexus.engine.streaming import ChatStreamEvent, TraceEvent

    out = []
    p = StreamEventPrinter(write=lambda t, nl=True: out.append((t, nl)))

    p.handle(ChatStreamEvent(kind="delta", text="你"))
    p.handle(ChatStreamEvent(kind="delta", text="好"))
    p.handle(ChatStreamEvent(kind="trace", trace=TraceEvent(
        "tool_call", data={"tool_name": "s"})))
    p.handle(ChatStreamEvent(kind="round",
                             round_info={"outcome": "tool", "round_idx": 0}))
    p.handle(ChatStreamEvent(kind="delta", text="改口了"))

    # while streaming, writes carry nl=False; a trace/round first closes the line
    assert out[0] == ("助手: ", False)
    assert ("", True) in out                      # line closed before the trace
    assert ("  [tool_call] s {}", True) in out    # args rendered inline
    assert ("  [round 0] tool", True) in out

    p.close("最终答复")                            # differs from streamed → reprint
    assert ("助手: 最终答复", True) in out

    # identical stream: no reprint
    out2 = []
    p2 = StreamEventPrinter(write=lambda t, nl=True: out2.append((t, nl)))
    p2.handle(ChatStreamEvent(kind="delta", text="一致"))
    p2.close("一致")
    assert not any(t.startswith("助手: 一致") and nl for t, nl in out2)


# ============================================================================
# Real-time delivery: events reach the consumer WHILE the turn is executing
# ============================================================================

def test_events_arrive_during_turn_execution():
    """The queue+task bridge streams events live: a delta is observed while
    the provider is still blocked mid-generation (pre-drain design could
    only deliver after the node finished)."""
    import asyncio

    from nexus.llm.types import LLMChunk

    class _GatedProvider:
        def __init__(self, gate):
            self.gate = gate

        async def achat_completion_stream(self, messages, model,
                                          temperature=0.7, max_tokens=2048,
                                          **kwargs):
            yield LLMChunk(text="第一段", tool_calls=[], finish_reason="")
            await self.gate.wait()               # turn CANNOT finish yet
            yield LLMChunk(text="第二段", tool_calls=[], finish_reason="stop")

    async def _scenario():
        gate = asyncio.Event()
        s = _agent_session()
        with patch("atoms.executors.loop_executor.build_provider",
                   return_value=_GatedProvider(gate)):
            agen = chat_turn_stream("q", "as", {"as": s})
            first = None
            async for ev in agen:
                if ev.kind == "delta":
                    first = ev.text
                    break
            # observed a delta while the gate was still closed → the event
            # crossed the bridge before turn completion, i.e. real-time
            assert first == "第一段"
            assert not gate.is_set()
            gate.set()
            rest = [e async for e in agen]
        return rest

    rest = arun(_scenario())
    kinds = [e.kind for e in rest]
    # the second delta + final round, then node_end / graph_done, then done
    # (node_start was already consumed before the first-delta break)
    assert kinds == ["delta", "round", "trace", "trace", "done"]
    assert rest[-1].result.text == "第一段第二段"


# ============================================================================
# Unified stage: the reply field streams incrementally off the JSON doc
# ============================================================================

class _ChunkProvider:
    """Streams one scripted raw text (chunk list) per call."""

    def __init__(self, chunk_texts):
        self.chunk_texts = list(chunk_texts)

    async def achat_completion_stream(self, messages, model, temperature=0.7,
                                      max_tokens=2048, **kwargs):
        from nexus.llm.types import LLMChunk
        n = len(self.chunk_texts)
        for i, t in enumerate(self.chunk_texts):
            yield LLMChunk(text=t, tool_calls=[],
                           finish_reason="stop" if i == n - 1 else "")


def test_unified_stage_streams_reply_incrementally():
    """FSM unified call streams: the reply VALUE arrives as incremental
    deltas (escape split across chunks included), the rest of the JSON never
    leaks, and the executor does not re-emit the full reply afterwards."""
    # raw wire text: “ / ” split across the chunk boundary
    raw = ('{"reply": "已为您\\u20', '1c测试\\u201d", ',
           '"next_node": "n2", "slots": {}}')
    provider = _ChunkProvider(raw)
    s = _bind(Session(session_id="us", pattern_code="fp"), _fsm_pattern())
    with patch("nexus.llm.resolve.build_provider", return_value=provider):
        events = _collect(chat_turn_stream("q", "us", {"us": s}))

    deltas = [e.text for e in events if e.kind == "delta"]
    assert "".join(deltas) == "已为您“测试”"
    assert len(deltas) >= 2                       # incremental, not one blob
    for d in deltas:                              # raw JSON never leaks
        assert "next_node" not in d and "slots" not in d and "{" not in d
    assert events[-1].result.text == "已为您“测试”"
    assert s.cxt.current_node_code == "n2"
    # the trace/done ordering still holds (node_jump + conversation_end after the deltas)
    assert _kinds(events) == [("delta", None)] * len(deltas) + [
        ("trace", "node_jump"), ("trace", "conversation_end"),
        ("done", None)]


def test_two_stage_nlg_streams_reply():
    """The classic two-stage pipeline streams at the NLG step: NLU parses via
    the one-shot call, NLG wording arrives as plain-text deltas."""
    n1 = BaseNode(code="n1", name="收集", sub_nodes=["n2"])
    n2 = BaseNode(code="n2", name="完成")
    p = Pattern(code="f2p", name="t", description="t", pattern_type="fsm",
                nodes=[n1, n2])                   # no stages → builtin defaults
    s = _bind(Session(session_id="ts", pattern_code="f2p"), p)

    class _MixedProvider:
        async def achat_completion(self, messages, model, temperature=0.7,
                                   max_tokens=2048, **kwargs):
            return {"content": '{"next_node": "n2", "slots": {}}',
                    "tool_calls": [], "finish_reason": "stop"}

        async def achat_completion_stream(self, messages, model,
                                          temperature=0.7, max_tokens=2048,
                                          **kwargs):
            from nexus.llm.types import LLMChunk
            yield LLMChunk(text="好的，", tool_calls=[], finish_reason="")
            yield LLMChunk(text="已为您登记。", tool_calls=[],
                           finish_reason="stop")

    with patch("atoms.stages.nlu.nlu.build_provider",
               return_value=_MixedProvider()), \
         patch("atoms.stages.nlg.nlg.build_provider",
               return_value=_MixedProvider()):
        events = _collect(chat_turn_stream("q", "ts", {"ts": s}))

    deltas = [e.text for e in events if e.kind == "delta"]
    assert deltas == ["好的，", "已为您登记。"]    # streamed, not one blob
    assert events[-1].result.text == "好的，已为您登记。"
    assert _kinds(events) == [("delta", None), ("delta", None),
                              ("trace", "node_jump"), ("done", None)]


# ============================================================================
# ReplyFieldTap unit tests (escape machinery / fences / lock failures)
# ============================================================================

def _tap_all(raw: str, split: int = 1):
    from nexus.engine.streaming import ReplyFieldTap

    tap = ReplyFieldTap()
    out = []
    for i in range(0, len(raw), split):
        out.append(tap.feed(raw[i:i + split]))
    return "".join(out), tap


def test_reply_field_tap_char_by_char_with_escapes():
    raw = ('{"reply": "包含\\"引号\\"、\\\\反斜杠、\\n换行、'
           '\\u4e2d文、\\ud83d\\ude00表情", "next_node": "n2"}')
    emitted, tap = _tap_all(raw, split=1)          # worst-case chunking
    expected = ('包含"引号"、\\反斜杠、\n换行、中文、\U0001f600表情')
    assert emitted == expected
    assert tap.emitted == expected
    assert tap._closed                          # stopped at the closing quote


def test_reply_field_tap_ignores_fence_and_false_keys():
    raw = ('```json\n{"reply_mode": "on", "reply": "围栏内的真实回复", '
           '"next_node": ""}\n```')
    emitted, _ = _tap_all(raw)
    assert emitted == "围栏内的真实回复"

    # marker inside a list (no colon) → restart finds the real key later
    raw2 = '["reply"], {"reply": "真正的值"}'
    emitted2, _ = _tap_all(raw2)
    assert emitted2 == "真正的值"


def test_reply_field_tap_gives_up_cleanly():
    # non-string reply value → closed, nothing emitted, never raises
    emitted, tap = _tap_all('{"reply": 42, "next_node": "n2"}')
    assert emitted == "" and tap._closed
    # no reply key at all → stays open but silent
    emitted2, tap2 = _tap_all('{"other": "x", "next_node": "n2"}')
    assert emitted2 == "" and not tap2._closed
