"""Executor contract — how one node's single execution runs (plan-⑧).

The engine resolves a NodeExecutor per node (AGENT graph) or per pattern
(FSM pipeline) — plugin registry kind="executor"; resolution order
node.plugins[slot] > pattern.plugins[slot] > the type default code ("loop"
slot for AGENT nodes, "fsm" slot for FSM patterns). The default executors
(the ReAct node loop / the FSM pipeline) live in atoms/executors/; the
kernel keeps no default implementation — an unresolved executor fails fast
with a pointer to atoms.executors (same kernel-purity pattern as
pipeline.register_default_generate).

ExecutionContext wraps (not extends) DialogueContext: cxt is the persisted
data carrier (snapshotted to the session store), so transient execution
inputs — the pattern object graph, the node being executed, the force-close
flag, the stream emitter, the resume payload — live on a per-execution
wrapper instead of polluting cxt's serialization boundary. Executors do not
receive a Session: everything they need from it is either on cxt (history /
slots / position / llm_config / node_map) or on this wrapper (pattern);
compression and persistence stay in the chat layer's hands.
"""

from dataclasses import dataclass
from typing import Any, Optional

from nexus.engine.turn_result import TurnResult  # noqa: F401 -- re-exported contract type


@dataclass
class ExecutionContext:
    """Per-execution inputs for a NodeExecutor (transient, never persisted).

    Attributes:
        cxt: the live dialogue context (sole state carrier)
        pattern: the current Pattern object (stages / node_map / executor
            declarations); set by the engine before dispatch
        node: the node this execution runs (AGENT graph step; for the FSM
            pipeline executor this is the pattern's entry/current node —
            the FSM executor re-resolves its own current node from cxt)
        force_close: step budget exhausted — produce a closing reply, no
            further routing (graph runtime's terminal guard)
        stream: streaming emitter for delta forwarding
        resume_input: the user message that resumed a wait_human
            suspension (None on fresh executions / FSM turns). A node
            executor that previously returned wait_human re-executes with
            this payload — the langgraph-interrupt-style handoff
        step: the graph step index of this execution (0-based; trace
            observability)
    """

    cxt: Any = None
    pattern: Any = None
    node: Any = None
    force_close: bool = False
    stream: Optional[Any] = None
    resume_input: Optional[str] = None
    step: int = 0


@dataclass
class NodeExecutor:
    """Interface: execute one node once and return a TurnResult.

    Stateless by contract — all dialogue state lives on ec.cxt; the plugin
    registry caches one instance per code and shares it across sessions.
    Async since the asyncio rewrite (executors drive LLM/tools I/O).
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        raise NotImplementedError


# Pre-merge name kept as an alias (import anchor during the plan-⑧ migration)
ModuleExecutor = NodeExecutor
