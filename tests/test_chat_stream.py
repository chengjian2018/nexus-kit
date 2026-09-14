"""Engine streaming protocol tests — chat_turn_stream event sequences, the
aggregation-equivalence safety net (done.result == chat_turn return), round
markers on the agent loop, and the always-mounted SSE endpoint (the studio
模版测试 page's dialogue channel; formerly NEXUS_STREAM_DEBUG-gated)."""

import json
from unittest.mock import patch

import pytest

import atoms.executors  # noqa: F401 -- default executors registered
import atoms.stages  # noqa: F401 -- default stages registered
from async_utils import arun
from nexus.engine.chat import chat_turn, chat_turn_stream
from nexus.engine.session import Session
from nexus.engine.streaming import aggregate_turn
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern


# ============================================================================
# Fixture: a single-node AGENT pattern driven by a scripted provider
# ============================================================================

class _StreamProvider:
    """Provider that streams scripted rounds (LLMChunks); records calls."""

    def __init__(self, rounds):
        # rounds: list of list-of-chunk-tuples [(text, tool_calls, finish)]
        from nexus.llm.types import LLMChunk
        self.rounds = [
            [LLMChunk(text=t, tool_calls=tc, finish_reason=fr)
             for (t, tc, fr) in rnd]
            for rnd in rounds
        ]
        self.stream_calls = 0

    async def achat_completion_stream(self, messages, model, temperature=0.7,
                                      max_tokens=2048, **kwargs):
        self.stream_calls += 1
        for chunk in self.rounds.pop(0):
            yield chunk


def _stream_session():
    p = Pattern(code="sp", name="t", description="t",
                nodes=[BaseNode(code="n1", name="主节点")])
    s = Session(session_id="ss", pattern_code="sp")
    s.pattern = p
    s.cxt.node_map = p.node_map
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    s.cxt.llm_config = {"code": "x", "model": "m"}
    return s


async def _collect_events(agen):
    return [e async for e in agen]


def _tc(name="noop", args="{}", cid="c1"):
    return {"index": 0, "id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


# ============================================================================
# Event sequence + aggregation equivalence
# ============================================================================

def test_done_result_equals_chat_turn_return():
    """The streaming protocol's safety net: done.result == chat_turn()."""
    provider = _StreamProvider([[("最终答复", [], "stop")]])
    s1, s2 = _stream_session(), _stream_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = arun(_collect_events(chat_turn_stream("你好", "ss", {"ss": s1})))
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=_StreamProvider([[("最终答复", [], "stop")]])):
        result = arun(chat_turn("你好", "ss", {"ss": s2}))
    dones = [e for e in events if e.kind == "done"]
    assert len(dones) == 1
    assert dones[-1].result.text == result.text == "最终答复"
    assert dones[-1].result.actions == result.actions


def test_delta_then_round_then_done_sequence():
    provider = _StreamProvider([[("你", [], ""), ("好", [], "stop")]])
    s = _stream_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = arun(_collect_events(chat_turn_stream("q", "ss", {"ss": s})))
    kinds = [(e.kind, getattr(e.trace, "event", None)) for e in events]
    # graph_compile opens every fresh run (编译形状可观测), deltas arrive as streamed (inside the node execution, wrapped
    # by the graph runtime's node_start/node_end/graph_done traces), then done
    assert kinds == [("trace", "graph_compile"),
                     ("trace", "node_start"),
                     ("delta", None), ("delta", None), ("round", None),
                     ("trace", "node_end"), ("trace", "graph_done"),
                     ("done", None)]
    assert "".join(e.text for e in events if e.kind == "delta") == "你好"
    rounds = [e.round_info for e in events if e.kind == "round"]
    assert rounds == [{"outcome": "final", "round_idx": 0}]
    assert events[-1].result.text == "你好"


def test_tool_round_marker_then_final_round():
    """A tool round emits outcome=tool; the final round emits outcome=final
    (optimistic forwarding: deltas of the tool round stream too)."""
    provider = _StreamProvider([
        [("让我查查", [], ""), ("", [_tc()], "tool_calls")],   # round 0: tool
        [("答案是42", [], "stop")],                              # round 1: final
    ])
    s = _stream_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        events = arun(_collect_events(chat_turn_stream("q", "ss", {"ss": s})))
    rounds = [e.round_info for e in events if e.kind == "round"]
    assert rounds == [
        {"outcome": "tool", "round_idx": 0},
        {"outcome": "final", "round_idx": 1},
    ]
    # tool round's text also forwarded (optimistic); done is authoritative
    assert events[-1].result.text == "答案是42"


def test_tool_call_and_result_traces_stream():
    """The kernel dispatch's tool_call/tool_result traces reach the stream
    with the shared data vocabulary (tool_name / args / result / synthetic)
    — the same contract the private research dispatch mirrors."""
    from nexus.engine.session import Session
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern
    from nexus.registry.tools import registry as tool_registry

    def _handler(args):
        return json.dumps({"ok": True, "echo": args.get("q")},
                          ensure_ascii=False)

    tool_registry.register(
        name="fake_stream_tool", toolset="fake_set",
        schema={"name": "fake_stream_tool", "description": "测试工具",
                "parameters": {"type": "object",
                               "properties": {"q": {"type": "string"}}}},
        handler=_handler)
    try:
        p = Pattern(code="sp_tool", name="t", description="t",
                    allow_toolset=["fake_set"],
                    nodes=[BaseNode(code="n1", name="主节点",
                                    use_tools=["fake_stream_tool"])])
        s = Session(session_id="ss2", pattern_code="sp_tool")
        s.pattern = p
        s.cxt.node_map = p.node_map
        s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
        s.cxt.llm_config = {"code": "x", "model": "m"}

        provider = _StreamProvider([
            [("", [_tc(name="fake_stream_tool", args='{"q": "hi"}')],
              "tool_calls")],
            [("完成", [], "stop")],
        ])
        with patch("atoms.executors.loop_executor.build_provider",
                   return_value=provider):
            events = arun(_collect_events(
                chat_turn_stream("q", "ss2", {"ss2": s})))
    finally:
        tool_registry.deregister("fake_stream_tool")

    tool_traces = [e.trace for e in events if e.kind == "trace"
                   and e.trace.event in ("tool_call", "tool_result")]
    assert [t.event for t in tool_traces] == ["tool_call", "tool_result"]
    assert tool_traces[0].node_code == "n1"
    assert tool_traces[0].data["tool_name"] == "fake_stream_tool"
    assert tool_traces[0].data["args"] == {"q": "hi"}
    assert tool_traces[1].data["tool_name"] == "fake_stream_tool"
    assert not tool_traces[1].data["synthetic"]
    assert "hi" in tool_traces[1].data["result"]
    assert events[-1].result.text == "完成"


def test_aggregate_turn_helper():
    provider = _StreamProvider([[("ok", [], "stop")]])
    s = _stream_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        result = arun(aggregate_turn(chat_turn_stream("q", "ss", {"ss": s})))
    assert result.text == "ok"


def test_aggregate_turn_requires_done():
    from nexus.engine.streaming import ChatStreamEvent

    async def _agen():
        yield ChatStreamEvent(kind="delta", text="x")

    with pytest.raises(RuntimeError, match="done"):
        arun(aggregate_turn(_agen()))


def test_fallback_without_stream_method_still_works():
    """Duck-typed provider without achat_completion_stream: the loop falls
    back to achat_completion, no delta events, result identical."""

    class _Legacy:
        async def achat_completion(self, messages, model, temperature=0.7,
                                   max_tokens=2048, **kwargs):
            return {"content": "legacy 答复", "tool_calls": [],
                    "finish_reason": "stop"}

    s = _stream_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=_Legacy()):
        events = arun(_collect_events(chat_turn_stream("q", "ss", {"ss": s})))
    kinds = [e.kind for e in events]
    # no deltas (non-streaming provider); only the round marker between the
    # graph_compile opener and the node lifecycle traces and done
    assert kinds == ["trace", "trace", "round", "trace", "trace", "done"]
    assert events[-1].result.text == "legacy 答复"


def test_turn_error_trace_before_generic_done():
    """A failing turn emits the turn_error trace before the generic-text
    done — real-time consumers flag failures without string-matching;
    aggregation is unaffected (done stays the only authoritative result)."""

    class _Boom:
        async def achat_completion_stream(self, messages, model,
                                          temperature=0.7, max_tokens=2048,
                                          **kwargs):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

    s = _stream_session()
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=_Boom()):
        events = arun(_collect_events(chat_turn_stream("q", "ss", {"ss": s})))
    traces = [e.trace.event for e in events if e.kind == "trace"]
    assert "turn_error" in traces
    assert events[-1].kind == "done"
    assert events[-1].result.text == "对话处理异常，请稍后重试"


# ============================================================================
# SSE endpoint (always mounted — the studio 模版测试 dialogue channel)
# ============================================================================

def test_sse_endpoint_streams_events(_host_main, monkeypatch):
    from fastapi.testclient import TestClient

    host_main = _host_main
    client = TestClient(host_main.app)

    provider = _StreamProvider([[("流式", [], ""), ("回复", [], "stop")]])
    s = _stream_session()
    host_main.governor.register_new(s)

    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider):
        resp = client.post("/api/v1/chat/stream",
                           json={"request_id": "r1", "session_id": "ss",
                                 "query": "你好"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    payloads = [json.loads(line[6:]) for line in resp.text.splitlines()
                if line.startswith("data: ")]
    kinds = [p["kind"] for p in payloads]
    # graph_compile opener + node lifecycle traces wrap the
    # streamed deltas / round marker
    assert kinds == ["trace", "trace", "delta", "delta", "round",
                     "trace", "trace", "done"]
    assert payloads[-1]["result"]["text"] == "流式回复"


@pytest.fixture()
def _host_main():
    import host.main as host_main
    return host_main


def test_sse_endpoint_mounted_without_env(monkeypatch):
    """The endpoint is first-class now (studio 模版测试 consumes it): mounted
    with no NEXUS_STREAM_DEBUG set at all (unknown session → JSON 404, which
    proves the route exists)."""
    monkeypatch.delenv("NEXUS_STREAM_DEBUG", raising=False)
    import host.main as host_main
    from fastapi.testclient import TestClient

    client = TestClient(host_main.app)
    resp = client.post("/api/v1/chat/stream",
                       json={"request_id": "r1", "session_id": "x",
                             "query": "y"})
    assert resp.status_code == 404
    assert resp.json()["status"] is False
