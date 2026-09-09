"""TurnResult — the executor return contract, isolated from the loop module
so both the kernel (loop / execution) and the executor atoms can import it
without a cycle.

(content/actions/extra — see the dataclass docstring for the field split.)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TurnResult:
    """Result of running one turn of a single module.

    Fields:
    - content  : the intrinsic field — this module's reply text (empty on a
                 silent transfer turn, whose jump event is already in
                 cxt.actions for the chat layer's hop loop)
    - actions  : event channel, same shape as cxt.actions — events that
                 drive engine flow (jump / deferred switch); the default
                 executors write events to cxt.actions and leave this empty
    - extra    : open-world extension bag — structured output for custom
                 executors (usage stats, round info, multimedia payloads,
                 ...); consumers ignore unknown keys, the chat layer
                 aggregates it through to ChatResult.extra
    """

    content: Optional[str] = None
    actions: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
