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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional

from nexus.engine.response import ChatResult

logger = logging.getLogger(__name__)


@dataclass
class ChatStreamEvent:
    """One streamed event of a dialogue turn."""

    kind: str  # "delta" | "round" | "done"
    text: str = ""                                  # delta: text increment
    round_info: Optional[Dict[str, Any]] = None     # round: outcome info
    result: Optional[ChatResult] = None             # done: terminal ChatResult


class StreamEmitter:
    """Collector handed to executors via ExecutionContext.stream.

    The executor forwards text deltas and round outcomes into the emitter;
    chat_turn_stream drains it after each executor step and re-yields the
    events to its consumer. (A pull-drain rather than a push-callback: the
    turn orchestration stays a plain function composition, no threads.)"""

    def __init__(self):
        self.events: List[ChatStreamEvent] = []

    def emit_delta(self, text: str) -> None:
        if text:
            self.events.append(ChatStreamEvent(kind="delta", text=text))

    def emit_round(self, outcome: str, round_idx: int) -> None:
        self.events.append(ChatStreamEvent(
            kind="round", round_info={"outcome": outcome,
                                      "round_idx": round_idx}))

    def drain(self) -> List[ChatStreamEvent]:
        out, self.events = self.events, []
        return out


def aggregate_turn(events: Generator[ChatStreamEvent, None, None]
                   ) -> ChatResult:
    """Aggregate a chat_turn_stream generator into its ChatResult (the last
    done event; raises if the stream produced none)."""
    result: Optional[ChatResult] = None
    for event in events:
        if event.kind == "done" and event.result is not None:
            result = event.result
    if result is None:
        raise RuntimeError("流式对话未产生终止事件（done）")
    return result
