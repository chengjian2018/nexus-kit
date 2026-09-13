"""TurnResult — the executor return contract, isolated from the loop module
so both the kernel (loop / execution) and the executor atoms can import it
without a cycle.

(content/next/sends/wait_human/actions/extra — see the dataclass docstrings
for the field split.)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class Send:
    """One worker-instance dispatch of a runtime fan-out (plan-⑨, borrowed
    from langgraph's Send API).

    A node executor returns ``TurnResult.sends=[Send(...), ...]`` to spawn N
    worker instances — heterogeneous fan-out is allowed: each send names
    its OWN worker node, which must be among the dispatching node's
    sub_nodes (a declared edge; the compile-time validated graph stays
    authoritative). The merge (join) node is the single node common to
    every targeted worker's sub_nodes (set intersection; an unresolvable
    merge — no common node, several common nodes, or 2+ outlier workers —
    is a template-correctness error, except exactly one outlier worker
    which the engine drops with a warning and runs the rest). ``input``
    is the instance's task payload: a str lands as the branch's explicit
    query verbatim, anything else is JSON-serialized into that slot. Folding
    whatever conversation context the branch needs into this payload is the
    dispatching node's job — branches run in a private workspace and see no
    session history.
    """

    node_code: str
    input: Any = None


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
                   accepted as legacy tolerance (the first element is
                   consumed serially) — runtime fan-out is declared via
                   ``sends`` instead. None = no explicit route (terminal if
                   the node has no successors / is_end).
    - sends      : the fan-out output (plan-⑨) — N worker instances of
                   declared nodes (heterogeneous targets allowed),
                   mutually exclusive with ``next`` (both set = contract
                   error). The engine runs the instances concurrently
                   (asyncio.gather), settles each into the graph_state
                   results board (completion order), then executes the
                   merge node common to every targeted worker's sub_nodes
                   (exactly one outlier worker without a common merge is
                   dropped with a warning; an unresolvable merge raises).
                   Worker content lands ONLY on the results board — it
                   never becomes the graph reply directly.
    - wait_human : the suspension signal (borrowed from langgraph's
                   interrupt): the graph pauses AT this node — the engine
                   persists the cursor into cxt.graph_state and ends the
                   turn; the NEXT user message re-executes this node with
                   the message available as ec.resume_input. Side-effect
                   idempotency across the re-execution is the node
                   executor's documented responsibility. Forbidden inside a
                   fan-out branch (the branch fails instead of suspending).
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
    sends: Optional[List[Send]] = None
    wait_human: bool = False
    actions: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
