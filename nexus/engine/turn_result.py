"""TurnResult — the executor return contract, isolated from the loop module
so both the kernel (loop / execution) and the executor atoms can import it
without a cycle.

(content/next/wait_human/actions/extra — see the dataclass docstring for
the field split.)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class TurnResult:
    """Result of executing one node (one executor invocation).

    Fields:
    - content    : the intrinsic field — this execution's reply text (the
                   graph's reply is the last non-empty content along the
                   run; empty on a silent intermediate node)
    - next       : the routing output — the AGENT graph's conditional edge.
                   A single node code (must be one of the executing node's
                   sub_nodes; the engine validates and tolerates
                   hallucinations by terminating with a warning). A list is
                   the extension slot for runtime fan-out (parallel
                   branches — NOT implemented; the engine consumes the first
                   element serially). None = no explicit route (terminal if
                   the node has no successors / is_end).
    - wait_human : the suspension signal (borrowed from langgraph's
                   interrupt): the graph pauses AT this node — the engine
                   persists the cursor into cxt.graph_state and ends the
                   turn; the NEXT user message re-executes this node with
                   the message available as ec.resume_input. Side-effect
                   idempotency across the re-execution is the node
                   executor's documented responsibility.
    - actions    : event channel, same shape as cxt.actions — dict-shaped
                   actions for the engine/consumers; the default executors
                   write events to cxt.actions and leave this empty
    - extra      : open-world extension bag — structured output for custom
                   executors (usage stats, round info, multimedia payloads,
                   ...); consumers ignore unknown keys, the chat layer
                   aggregates it through to ChatResult.extra
    """

    content: Optional[str] = None
    next: Optional[Union[str, List[str]]] = None
    wait_human: bool = False
    actions: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
