"""Engine-level streaming protocol (plan-⑤).

chat_turn_stream(...) is the generator form of chat_turn: it yields
ChatStreamEvent objects while the turn is being processed, the final event
carrying the complete ChatResult. chat_turn aggregates this generator, so
the non-streaming entry's behavior is byte-identical (the aggregation
equivalence is what the tests pin).

Event kinds:
- "delta": a text increment of the in-flight module's reply (optimistic
  forwarding — see the caveat below)
- "round": an agent-loop round boundary with its outcome
  ({"outcome": "tool"|"final"|"transfer"|"max_rounds", "round_idx": n})
- "trace": a state-transition observability event (jump routing / node
  transitions / tool calls — see TraceEvent; emitted only when state
  actually changes, never per-turn unconditionally, so existing exact
  event-sequence pins stay intact)
- "done": the terminal event; ``result`` holds the authoritative ChatResult

Optimistic-forwarding caveat: OpenAI's finish_reason only arrives at a
round's end, so mid-loop rounds' text is forwarded as it streams even
though a tool call may follow (the final reply then comes from a LATER
round). Aggregating consumers (HTTP / channels) are unaffected — they read
only done.result. Real-time consumers (the SSE debug endpoint) see the
interim text and use the round events to identify which text belongs to
the final round; done.result.text is always the authoritative reply.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from nexus.engine.response import ChatResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Turn-scoped streaming context (set by chat_turn_stream's turn task)
# ---------------------------------------------------------------------------

# The emitter of the in-flight turn. Stages receive only cxt (their execute
# signature carries no stream), so stage-level LLM streaming reads the
# emitter through this contextvar instead of a contract change. Same-task
# awaited chains (stages -> executors) all see the value.
current_emitter: ContextVar[Optional["StreamEmitter"]] = ContextVar(
    "nexus_current_emitter", default=None)

# Reply text already forwarded as deltas during THIS module's stage run
# (unified reply-field tap / plain NLG stream). Executors read it to avoid
# double-emitting the reply after stages complete; _run_stages resets it
# per module execution.
current_streamed_reply: ContextVar[str] = ContextVar(
    "nexus_current_streamed_reply", default="")


def reset_streamed_reply() -> None:
    """Zero the streamed-reply marker (start of each module execution)."""
    current_streamed_reply.set("")


@dataclass
class ChatStreamEvent:
    """One streamed event of a dialogue turn."""

    kind: str  # "delta" | "round" | "trace" | "done"
    text: str = ""                                  # delta: text increment
    round_info: Optional[Dict[str, Any]] = None     # round: outcome info
    trace: Optional["TraceEvent"] = None            # trace: transition event
    result: Optional[ChatResult] = None             # done: terminal ChatResult
    branch_id: str = ""                             # fan-out branch tag
                                                   # ("" = main path; plan-⑨)


# Canonical trace event names (all optional fields default to ""/{} —
# consumers render what is present; unknown names pass through untouched).
# plan-⑧: the module-jump family (module_jump / route_hit / route_root /
# defer_switch / module_start) is gone with the module layer; the graph
# runtime emits the node_* / graph_* family. plan-⑨ adds the fan-out family
# (worker instances of a runtime sends dispatch) + graph_compile.
TRACE_EVENT_NAMES = (
    "node_start",        # an AGENT graph node's execution begins
    "node_end",          # an AGENT graph node's execution finished
    "node_jump",         # FSM end-of-turn node transition
    "graph_compile",     # a fresh graph run started (compiled shape summary)
    "graph_wait",        # AGENT graph suspended (wait_human)
    "graph_resume",      # AGENT graph resumed from suspension
    "graph_done",        # AGENT graph run terminated (reason: terminal /
                         # is_end / max_steps / undeclared_edge)
    "fanout_start",      # plan-⑨: a node dispatched N worker instances
    "branch_start",      # plan-⑨: one worker instance began (branch_id)
    "branch_end",        # plan-⑨: one worker instance settled (ok/error)
    "fanout_join",       # plan-⑨: all instances settled, join node fires
    "tool_call",         # agent loop: one tool invocation issued
    "tool_result",       # agent loop: one tool invocation returned
    "conversation_end",  # FSM reached a terminal node (is_end)
)


@dataclass
class TraceEvent:
    """A state-transition observability event (kind="trace").

    Carries only plain serializable values (str / int / dict) — real-time
    consumers (CLI / SSE) render them live; aggregate_turn ignores them
    (done.result stays authoritative), so adding trace emissions never
    changes chat_turn/chat() behavior.
    """

    event: str                       # one of TRACE_EVENT_NAMES (open set)
    module_code: str = ""
    node_code: str = ""
    branch_id: str = ""              # fan-out branch tag ("" = main path)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Flat dict form (SSE wire / test assertions drop the dataclass)."""
        out: Dict[str, Any] = {"event": self.event,
                               "module_code": self.module_code}
        if self.node_code:
            out["node_code"] = self.node_code
        if self.branch_id:
            out["branch_id"] = self.branch_id
        if self.data:
            out["data"] = self.data
        return out


class StreamEmitter:
    """Collector handed to executors via ExecutionContext.stream.

    Two modes:
    - pull (default, sink=None): emit_* append to an internal list; the
      consumer drains after each step. Fine for coarse step granularity,
      but nothing reaches the consumer while a long step is in flight.
    - push (sink set): emit_* forward IMMEDIATELY to the sink callback —
      chat_turn_stream installs ``queue.put_nowait`` and runs the turn in
      a background task, so events stream out live while the turn runs
      (deltas as LLM chunks arrive, tool traces at dispatch time).

    The sink must be non-blocking (asyncio.Queue.put_nowait or similar);
    ordering is whatever the single turn task emits — FIFO by construction.
    """

    def __init__(self, sink: Optional[Callable[[ChatStreamEvent], None]] = None):
        self._sink = sink
        self.events: List[ChatStreamEvent] = []

    def _append(self, event: ChatStreamEvent) -> None:
        if self._sink is not None:
            self._sink(event)
        else:
            self.events.append(event)

    def emit_delta(self, text: str, branch_id: str = "") -> None:
        if text:
            self._append(ChatStreamEvent(kind="delta", text=text,
                                         branch_id=branch_id))

    def emit_round(self, outcome: str, round_idx: int,
                   branch_id: str = "") -> None:
        self._append(ChatStreamEvent(
            kind="round", branch_id=branch_id,
            round_info={"outcome": outcome,
                        "round_idx": round_idx,
                        **({"branch_id": branch_id} if branch_id else {})}))

    def emit_trace(self, event: str, module_code: str = "",
                   node_code: str = "", branch_id: str = "",
                   **data: Any) -> None:
        """Append a trace event (extra kwargs become TraceEvent.data)."""
        self._append(ChatStreamEvent(
            kind="trace",
            trace=TraceEvent(event=event, module_code=module_code,
                             node_code=node_code, branch_id=branch_id,
                             data=dict(data))))

    def drain(self) -> List[ChatStreamEvent]:
        out, self.events = self.events, []
        return out


class BranchStreamEmitter:
    """Tags every event of ONE fan-out branch with its branch_id (plan-⑨
    §4.1).

    A worker instance receives this as ``ec.stream`` instead of the turn
    emitter — the executor keeps calling emit_delta / emit_round /
    emit_trace unchanged and every event reaches the turn sink already
    tagged, so concurrent branches demultiplex cleanly on the consumer
    side (CLI branch prefixes / console lanes). Drain passthrough keeps
    pull-mode consumers working.
    """

    def __init__(self, inner: StreamEmitter, branch_id: str):
        self._inner = inner
        self._branch_id = branch_id

    def emit_delta(self, text: str) -> None:
        self._inner.emit_delta(text, branch_id=self._branch_id)

    def emit_round(self, outcome: str, round_idx: int) -> None:
        self._inner.emit_round(outcome, round_idx, branch_id=self._branch_id)

    def emit_trace(self, event: str, module_code: str = "",
                   node_code: str = "", **data: Any) -> None:
        self._inner.emit_trace(event, module_code=module_code,
                               node_code=node_code,
                               branch_id=self._branch_id, **data)

    def drain(self) -> List[ChatStreamEvent]:
        return self._inner.drain()


async def aggregate_turn(events: AsyncGenerator[ChatStreamEvent, None]
                         ) -> ChatResult:
    """Aggregate a chat_turn_stream async generator into its ChatResult (the
    last done event; raises if the stream produced none)."""
    result: Optional[ChatResult] = None
    async for event in events:
        if event.kind == "done" and event.result is not None:
            result = event.result
    if result is None:
        raise RuntimeError("流式对话未产生终止事件（done）")
    return result


# ---------------------------------------------------------------------------
# Stage-level LLM streaming (reply forwarding for FSM/ROUTE pipelines)
# ---------------------------------------------------------------------------

class ReplyFieldTap:
    """Incremental extractor for a JSON string field off a chunk stream.

    The unified stage's single call returns a JSON doc whose ``reply`` field
    IS the user-visible reply. Tap the raw stream: once the ``"reply"`` key's
    opening quote is seen, every decoded character of the value is streamed
    out as it arrives (escape sequences resolved; ``\\uXXXX`` held until the
    four hex digits complete, surrogate pairs combined). The closing
    unescaped quote ends the tap — later fields (next_node/slots) never leak.

    Never raises: on any surprise the tap just stops emitting (the parsed
    reply after aggregation is the fallback; the executor dedups via
    current_streamed_reply).
    """

    _KEY = '"reply"'

    def __init__(self):
        self._buf = ""            # unprocessed raw tail (seek phase)
        self._in_value = False
        self._pending = ""        # decode hold: partial escape / surrogate
        self._pending_high = 0    # held high surrogate while awaiting the low
        self._escape = ""         # "" | "\\" | "u" | "us_bs"|"us_u"|"us_hex"
                                   # (us_* = awaiting the low \uXXXX of a pair)
        self._closed = False
        self.emitted = ""

    def feed(self, text: str) -> str:
        """Feed a raw chunk; return the characters newly decoded."""
        if self._closed or not text:
            return ""
        if not self._in_value:
            # Seek phase: accumulate raw text until the "reply" key's opening
            # quote is seen. The remainder after that quote (same chunk!) is
            # the start of the value — it must flow into the value phase,
            # not be discarded.
            self._buf += text
            value_start = self._seek_lock()
            if value_start is None:
                return ""
            text = value_start
        new = []
        for ch in text:
            if self._closed:
                break
            self._value_char(ch, new)
        out = "".join(new)
        self.emitted += out
        return out

    def _seek_lock(self) -> Optional[str]:
        """Try to lock onto the value opening quote; on success return the
        raw text after it (the value body so far), else None (need more
        chunks / gave up).

        On "matched the key but nothing after it yet" the buffer is KEPT —
        the ':' / opening quote may arrive in a later chunk (a per-char
        chunk stream hits this on every feed until the colon arrives).
        """
        while True:
            idx = self._buf.find(self._KEY)
            if idx < 0:
                # keep a tail wide enough for a key split across chunks
                self._buf = self._buf[-(len(self._KEY) + 8):]
                return None
            stripped = self._buf[idx + len(self._KEY):].lstrip()
            if not stripped:
                return None                      # ':' may come in a later chunk
            if len(stripped) > 64:
                self._closed = True              # absurd gap — give up
                return None
            if stripped[0] != ":":
                # matched text inside another token (e.g. a value mentioning
                # "reply") — search past this occurrence
                self._buf = self._buf[idx + len(self._KEY):]
                continue
            value_side = stripped[1:].lstrip()
            if not value_side:
                return None                      # opening quote may come later
            if value_side[0] != '"':
                self._closed = True               # non-string value — give up
                return None
            self._in_value = True
            self._buf = ""
            return value_side[1:]

    def _value_char(self, ch: str, new: List[str]) -> None:
        """Process one raw char inside the value string (escape machinery)."""
        if self._escape == "u":
            self._pending += ch
            if len(self._pending) == 4:
                try:
                    point = int(self._pending, 16)
                except ValueError:
                    point = None
                if point is not None:
                    # surrogate pair combining (emoji etc.): the low half
                    # follows as \uXXXX — expect its escape prefix first
                    if 0xD800 <= point <= 0xDBFF:
                        self._pending_high = point
                        self._escape = "us_bs"
                        self._pending = ""
                        return
                    new.append(self._from_escaped(point))
                self._pending = ""
                self._escape = ""
            return
        if self._escape == "us_bs":
            self._escape = "us_u" if ch == "\\" else ""
            if self._escape != "us_u":
                self._pending_high = 0        # malformed pair — drop the high
            return
        if self._escape == "us_u":
            self._escape = "us_hex" if ch == "u" else ""
            if self._escape != "us_hex":
                self._pending_high = 0
            return
        if self._escape == "us_hex":
            self._pending += ch
            if len(self._pending) == 4:
                try:
                    low = int(self._pending, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        high = self._pending_high
                        new.append(self._from_escaped(
                            0x10000 + ((high - 0xD800) << 10)
                            + (low - 0xDC00)))
                except ValueError:
                    pass
                self._pending = ""
                self._escape = ""
                self._pending_high = 0
            return
        if self._escape == "\\":
            self._escape = ""
            mapping = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                       "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
            if ch == "u":
                self._escape = "u"
                self._pending = ""
            elif ch in mapping:
                new.append(mapping[ch])
            else:
                new.append(ch)                    # lenient: unknown escape
            return

        if ch == "\\":
            self._escape = "\\"
            return
        if ch == '"':
            self._closed = True                   # value closed — stop
            return
        new.append(ch)

    @staticmethod
    def _from_escaped(point: int) -> str:
        try:
            return chr(point)
        except (ValueError, OverflowError):
            return "�"


async def stream_llm_reply(chunks: AsyncGenerator[Any, None],
                           field: Optional[str] = None) -> Dict[str, Any]:
    """Consume an LLM chunk stream, forwarding user-visible text deltas via
    the current turn emitter; returns the aggregated legacy dict.

    - field=None: plain generation — every text chunk is user-visible (the
      two-stage NLG's reply wording), forwarded verbatim.
    - field="reply": the chunks form a JSON doc (the unified single-call
      protocol); only the named field's value is forwarded incrementally
      (ReplyFieldTap). Raw JSON of other fields never leaks.

    Also records what was forwarded into ``current_streamed_reply`` so the
    fsm/route executors skip re-emitting an identical complete reply. When
    no emitter is attached (non-streaming turn / tests), this is just a
    plain aggregation — callers fall back to it transparently.
    """
    from nexus.llm.aggregate import acollect_stream

    emitter = current_emitter.get()
    tap = ReplyFieldTap() if field else None
    forwarded: List[str] = []

    async def _tap():
        async for chunk in chunks:
            text = getattr(chunk, "text", "") or ""
            if text and emitter is not None:
                delta = tap.feed(text) if tap is not None else text
                if delta:
                    forwarded.append(delta)
                    emitter.emit_delta(delta)
            yield chunk

    result = await acollect_stream(_tap())
    current_streamed_reply.set(
        tap.emitted if tap is not None else "".join(forwarded))
    return result
