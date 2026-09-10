"""Executor contract — how one module's single hop is executed.

The chat layer's hop loop resolves a ModuleExecutor per module (plugin
registry kind="executor"; resolution order module.executor >
pattern.executor_<type> > type default code) and calls execute(ec). The
three default executors (agent loop / FSM pipeline / ROUTE pipeline) live in
atoms/executors/; the kernel keeps no default implementation — an unresolved
executor fails fast with a pointer to atoms.executors (same kernel-purity
pattern as pipeline.register_default_generate).

ExecutionContext wraps (not extends) DialogueContext: cxt is the persisted
data carrier (snapshotted to the session store), so transient execution
inputs — the pattern object graph, the module being executed, the
force-close flag, the stream emitter (added by the streaming plan) — live on
a per-execution wrapper instead of polluting cxt's serialization boundary.
Executors do not receive a Session: everything they need from it is either
on cxt (history / slots / position / llm_config / module_map / node_map) or
on this wrapper (pattern); compression and persistence stay in the chat
layer's hands.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from nexus.engine.turn_result import TurnResult  # noqa: F401 -- re-exported contract type


@dataclass
class ExecutionContext:
    """Per-execution inputs for a ModuleExecutor (transient, never persisted).

    Attributes:
        cxt: the live dialogue context (sole state carrier)
        pattern: the current Pattern object (stages / module_map / executor
            declarations); set by the chat layer before dispatch
        module: the module this hop executes (already resolved from
            module_map by the chat layer)
        force_close: max_hops exhausted — produce a closing reply, no new
            jumps (was run_agent's 4th parameter)
        stream: streaming emitter for delta forwarding (added by the
            streaming plan; None until then)
    """

    cxt: Any = None
    pattern: Any = None
    module: Any = None
    force_close: bool = False
    stream: Optional[Any] = None


@dataclass
class ModuleExecutor:
    """Interface: execute one module's single hop and return a TurnResult.

    Stateless by contract — all dialogue state lives on ec.cxt; the plugin
    registry caches one instance per code and shares it across sessions.
    Async since the asyncio rewrite (executors drive LLM/tools I/O).
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        raise NotImplementedError
