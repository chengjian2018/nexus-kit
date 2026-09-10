"""Trace-event streaming tests — jump / node / route / tool observability.

chat_turn_stream emits kind="trace" events (nexus.engine.streaming.
TraceEvent) at every state transition a CLI consumer wants to see live:

- FSM    : node_jump (only when the node moved) + conversation_end + the
           reply as a single delta (stages are one-shot, no token stream)
- ROUTE  : route_hit (menu landed) → module_jump (hop-loop reroute) → the
           target module continues; non-jump turns add route_root at reset
- AGENT  : tool_call / tool_result per dispatch (synthetic flag marks the
           intercepted-backfill rows), plus the existing delta/round events
- defer  : defer_switch at end-of-turn base application

Suppression contract: stay-in-place turns emit nothing (root→root routing,
node unchanged) — existing exact event-sequence pins rely on the quiet
path. chat()/chat_turn aggregation ignores trace events, so the compat
entries are untouched (asserted for the FSM shape).
"""

import json
from unittest.mock import patch

import atoms.executors  # noqa: F401 -- default executors registered
import atoms.stages  # noqa: F401 -- default stages registered
from async_utils import arun
from nexus.engine.chat import chat_turn, chat_turn_stream
from nexus.engine.session import Session
from nexus.model.module import AgentModule, FSMModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern


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


def _bind(session: Session, pattern: Pattern, module_code: str) -> Session:
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.current_module_code = module_code
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    session.cxt.llm_config = {"code": "x", "model": "m"}
    return session


def _fsm_pattern(module_code: str = "fm") -> Pattern:
    n1 = BaseNode(node_code="n1", node_name="收集", sub_nodes=["n2"])
    n2 = BaseNode(node_code="n2", node_name="结束", is_end=True)
    m = FSMModule(module_code=module_code, module_name="f",
                  module_description="d", module_nodes=[n1, n2],
                  stages={"nlu": "fsm_unified", "nlg": "nlg_pass_through"})
    return Pattern(code="fp", name="t", description="t",
                   entry_module_code=module_code, modules=[m])


def _route_pattern(menu_jump: str = "") -> Pattern:
    """ROUTE rt (root r → menu m1) + FSM target fm2; m1.jump_module=menu_jump."""
    root = BaseNode(node_code="r", node_name="root", sub_nodes=["m1"])
    m1 = BaseNode(node_code="m1", node_name="菜单1", jump_module=menu_jump or None)
    rt = RouteModule(module_code="rt", module_name="route",
                     module_description="d", module_nodes=[root, m1],
                     stages={"nlu": "route_unified", "nlg": "nlg_pass_through"})
    t1 = BaseNode(node_code="t1", node_name="目标节点")
    fm2 = FSMModule(module_code="fm2", module_name="t", module_description="d",
                    module_nodes=[t1],
                    stages={"nlu": "fsm_unified", "nlg": "nlg_pass_through"})
    return Pattern(code="rp", name="t", description="t",
                   entry_module_code="rt", modules=[rt, fm2])


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
    s = _bind(Session(session_id="fs", pattern_code="fp"), _fsm_pattern(), "fm")
    reply_json = json.dumps({"reply": "好的已记录", "next_node": "n2",
                             "slots": {"a": "1"}}, ensure_ascii=False)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        events = _collect(chat_turn_stream("q", "fs", {"fs": s}))

    assert _kinds(events) == [("trace", "node_jump"),
                              ("trace", "conversation_end"),
                              ("delta", None), ("done", None)]
    jump = events[0].trace
    assert (jump.module_code, jump.node_code) == ("fm", "n2")
    assert jump.data["from_node"] == "n1"
    assert jump.data["to_node"] == "n2"
    assert "".join(e.text for e in events if e.kind == "delta") == "好的已记录"
    assert events[-1].result.text == "好的已记录"
    assert s.cxt.current_node_code == "n2"


def test_fsm_stay_turn_emits_no_trace():
    """Stay-in-place turn: delta + done only (suppression contract)."""
    s = _bind(Session(session_id="fs", pattern_code="fp"), _fsm_pattern(), "fm")
    reply_json = json.dumps({"reply": "请补充", "next_node": "",
                             "slots": {}}, ensure_ascii=False)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        events = _collect(chat_turn_stream("q", "fs", {"fs": s}))
    assert _kinds(events) == [("delta", None), ("done", None)]


def test_fsm_chat_compat_untouched_by_trace():
    """chat_turn aggregation ignores trace events — the compat entry's reply
    is byte-identical to done.result.text (与 chat() 不冲突)."""
    p = _fsm_pattern()
    reply_json = json.dumps({"reply": "好的已记录", "next_node": "n2",
                             "slots": {}}, ensure_ascii=False)
    s1 = _bind(Session(session_id="fs", pattern_code="fp"), p, "fm")
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        events = _collect(chat_turn_stream("q", "fs", {"fs": s1}))
    s2 = _bind(Session(session_id="fs", pattern_code="fp"), p, "fm")
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([reply_json])):
        result = arun(chat_turn("q", "fs", {"fs": s2}))
    assert events[-1].result.text == result.text == "好的已记录"


# ============================================================================
# ROUTE: route_hit → module_jump → target reply / route_root on plain turns
# ============================================================================

def test_route_jump_turn_event_sequence():
    p = _route_pattern(menu_jump="fm2")
    s = _bind(Session(session_id="rs", pattern_code="rp"), p, "rt")
    route_reply = json.dumps({"reply": "为您转接", "next_node": "m1",
                              "slots": {}}, ensure_ascii=False)
    target_reply = json.dumps({"reply": "FAQ答复", "next_node": "",
                               "slots": {}}, ensure_ascii=False)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([route_reply, target_reply])):
        events = _collect(chat_turn_stream("q", "rs", {"rs": s}))

    assert _kinds(events) == [("trace", "route_hit"),
                              ("trace", "module_jump"),
                              ("delta", None), ("done", None)]
    hit = events[0].trace
    assert (hit.module_code, hit.node_code) == ("rt", "m1")
    assert hit.data["from_node"] == "r"
    jump = events[1].trace
    assert jump.data["from_module"] == "rt"
    assert jump.data["to_module"] == "fm2"
    assert jump.data["source"] == "route_menu"
    assert jump.data["hop"] == 1
    assert events[-1].result.text == "FAQ答复"
    # the hop actually rerouted the session onto the target
    assert s.cxt.current_module_code == "fm2"


def test_route_plain_turn_resets_to_root():
    p = _route_pattern(menu_jump="")
    s = _bind(Session(session_id="rs", pattern_code="rp"), p, "rt")
    route_reply = json.dumps({"reply": "菜单答复", "next_node": "m1",
                              "slots": {}}, ensure_ascii=False)
    with patch("nexus.llm.resolve.build_provider",
               return_value=_ScriptedProvider([route_reply])):
        events = _collect(chat_turn_stream("q", "rs", {"rs": s}))

    assert _kinds(events) == [("trace", "route_hit"),
                              ("trace", "route_root"),
                              ("delta", None), ("done", None)]
    reset = events[1].trace
    assert reset.data["from_node"] == "m1"
    assert reset.data["to_node"] == "r"
    # end-of-turn position is back on root while the module stays ROUTE
    assert (s.cxt.current_module_code, s.cxt.current_node_code) == ("rt", "r")


# ============================================================================
# AGENT: tool_call / tool_result traces around the round marker
# ============================================================================

def _agent_session():
    main = AgentModule(module_code="sm", module_name="主", module_description="d")
    p = Pattern(code="sp", name="t", description="t",
                entry_module_code="sm", modules=[main])
    return _bind(Session(session_id="as", pattern_code="sp"), p, "sm")


def test_agent_tool_round_traces():
    """Tool round: delta → tool_call → tool_result (synthetic: intercepted
    backfill, the module declares no tools) → round(tool) → final round."""
    provider = _StreamProvider([
        [("让我查查", [], ""), ("", [_tc()], "tool_calls")],
        [("答案是42", [], "stop")],
    ])
    s = _agent_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = _collect(chat_turn_stream("q", "as", {"as": s}))

    assert _kinds(events) == [("delta", None), ("trace", "tool_call"),
                              ("trace", "tool_result"), ("round", None),
                              ("delta", None), ("round", None),
                              ("done", None)]
    call, result = events[1].trace, events[2].trace
    assert call.data["tool_name"] == "noop"
    assert call.data["args"] == {}
    assert call.data["round_idx"] == 0
    assert result.data["tool_name"] == "noop"
    assert result.data["synthetic"] is True
    assert "不存在" in result.data["result"]
    assert events[-1].result.text == "答案是42"


def test_agent_direct_reply_stays_trace_free():
    provider = _StreamProvider([[("直接答复", [], "stop")]])
    s = _agent_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = _collect(chat_turn_stream("q", "as", {"as": s}))
    # no transition happened → no trace (pre-streaming exact pins hold)
    assert _kinds(events) == [("delta", None), ("round", None), ("done", None)]


# ============================================================================
# defer: end-of-turn defer_switch trace
# ============================================================================

def _reception_pattern():
    reception = AgentModule(module_code="reception", module_name="前台",
                            module_description="接待",
                            sub_modules=["after_sales"])
    after_sales = AgentModule(module_code="after_sales", module_name="售后",
                              module_description="维保")
    return Pattern(code="p", name="t", description="t",
                   entry_module_code="reception",
                   modules=[reception, after_sales])


def test_defer_switch_trace_at_end_of_turn():
    s = _bind(Session(session_id="ds", pattern_code="p"),
              _reception_pattern(), "reception")
    provider = _RawProvider([
        {"content": "好的，先登记切换", "tool_calls": [{
            "id": "c1", "function": {
                "name": "defer_to_module",
                "arguments": '{"module_code": "after_sales",'
                             ' "reason": "深入流程"}'}}]},
        {"content": "本轮答复完成，后续由售后底座承接。", "tool_calls": []},
    ])
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = _collect(chat_turn_stream("帮我全程处理售后", "ds", {"ds": s}))

    kinds = _kinds(events)
    assert kinds[-2:] == [("trace", "defer_switch"), ("done", None)]
    switch = events[-2].trace
    assert switch.module_code == "after_sales"
    assert switch.data["from_module"] == "reception"
    assert switch.data["to_module"] == "after_sales"
    assert switch.data["source"] == "projection"
    # the base switch actually applied for the next turn
    assert s.cxt.current_module_code == "after_sales"
    # the defer tool rows were traced too (executor-side branch)
    assert ("trace", "tool_call") in kinds
    assert ("trace", "tool_result") in kinds


# ============================================================================
# TraceEvent wire shape + CLI rendering (host.cli)
# ============================================================================

def test_trace_event_to_dict_wire_shape():
    from nexus.engine.streaming import TraceEvent

    d = TraceEvent("node_jump", module_code="fm", node_code="n2",
                   data={"from_node": "n1", "to_node": "n2"}).to_dict()
    assert d == {"event": "node_jump", "module_code": "fm",
                 "node_code": "n2", "data": {"from_node": "n1",
                                             "to_node": "n2"}}
    # node_code/data omitted when empty (compact wire form)
    assert TraceEvent("route_root", module_code="rt").to_dict() == {
        "event": "route_root", "module_code": "rt"}


def test_render_trace_event_lines():
    from host.cli import render_trace_event
    from nexus.engine.streaming import TraceEvent

    renderings = {
        "module_jump": TraceEvent("module_jump", data={
            "from_module": "rt", "to_module": "faq", "source": "route_menu"}),
        "node_jump": TraceEvent("node_jump", data={
            "from_node": "n1", "to_node": "n2"}),
        "route_hit": TraceEvent("route_hit", data={"to_node": "m1"}),
        "tool_call": TraceEvent("tool_call", data={
            "tool_name": "search", "args": {"q": "订单"}}),
        "tool_result": TraceEvent("tool_result", data={
            "tool_name": "search", "result": "找到了"}),
        "defer_switch": TraceEvent("defer_switch", data={"to_module": "faq"}),
        "custom_thing": TraceEvent("custom_thing", module_code="x"),
    }
    lines = {k: render_trace_event(v) for k, v in renderings.items()}
    assert "[jump] rt → faq (route_menu)" in lines["module_jump"]
    assert "[node] n1 → n2" in lines["node_jump"]
    assert "命中菜单节点 m1" in lines["route_hit"]
    assert "[tool_call] search" in lines["tool_call"]
    assert "订单" in lines["tool_call"]
    assert "[tool_result] search: 找到了" in lines["tool_result"]
    assert "底座轮末切换 → faq" in lines["defer_switch"]
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
    only deliver after the module finished)."""
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
    assert kinds == ["delta", "round", "done"]
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
    s = _bind(Session(session_id="us", pattern_code="fp"), _fsm_pattern(), "fm")
    with patch("nexus.llm.resolve.build_provider", return_value=provider):
        events = _collect(chat_turn_stream("q", "us", {"us": s}))

    deltas = [e.text for e in events if e.kind == "delta"]
    assert "".join(deltas) == "已为您“测试”"
    assert len(deltas) >= 2                       # incremental, not one blob
    for d in deltas:                              # raw JSON never leaks
        assert "next_node" not in d and "slots" not in d and "{" not in d
    assert events[-1].result.text == "已为您“测试”"
    assert s.cxt.current_node_code == "n2"
    # the trace/done ordering still holds (node_jump after the deltas)
    assert _kinds(events) == [("delta", None)] * len(deltas) + [
        ("trace", "node_jump"), ("trace", "conversation_end"),
        ("done", None)]


def test_two_stage_nlg_streams_reply():
    """The classic two-stage pipeline streams at the NLG step: NLU parses via
    the one-shot call, NLG wording arrives as plain-text deltas."""
    n1 = BaseNode(node_code="n1", node_name="收集", sub_nodes=["n2"])
    n2 = BaseNode(node_code="n2", node_name="完成")
    m = FSMModule(module_code="fm2s", module_name="f", module_description="d",
                  module_nodes=[n1, n2])          # no stages → builtin defaults
    p = Pattern(code="f2p", name="t", description="t",
                entry_module_code="fm2s", modules=[m])
    s = _bind(Session(session_id="ts", pattern_code="f2p"), p, "fm2s")

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
