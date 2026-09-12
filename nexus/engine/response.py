"""
Turn output wrapper — a reply is more than text: text + actions.

The chat() compat entry keeps returning str (.text); chat_turn() returns
this module's ChatResult, reserving a channel for the API layer to consume
actions later (send card / graph wait / conversation end — actions beyond
the reply itself).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from nexus.context import DialogueContext


@dataclass
class ChatResult:
    """The complete output of one dialogue turn.

    - text    : final reply text (compatible with chat()'s str return)
    - actions : action channel (snapshotted from cxt.actions at end of
                turn; dict-shaped actions such as conversation_end /
                graph_wait — all passed through verbatim)
    """

    text: str
    actions: List[Dict[str, Any]] = field(default_factory=list)


def _snapshot_action(item: Any) -> Dict[str, Any]:
    """Convert an action entry to its observation dict (dict entries pass through)."""
    return item


def build_chat_result(text: str, cxt: DialogueContext) -> ChatResult:
    """Build a ChatResult from cxt at end of turn: snapshot the actions."""
    return ChatResult(
        text=text,
        actions=[_snapshot_action(item) for item in (cxt.actions or [])],
    )
