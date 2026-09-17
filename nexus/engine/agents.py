"""Compat module — the executor injection point moved to the plugin registry.

The old AgentRunner protocol + LoopAgentRunner (a parameter-level injection
with no registry) have been superseded by the plugin registry
(nexus/registry/plugins.py, kind="executor") and the ModuleExecutor contract
(nexus/engine/execution.py): executors are resolved per node/pattern
(node.plugins[slot] > pattern.plugins[slot] > the type default code) and
receive an ExecutionContext. The default loop implementation lives in
atoms/executors/loop_executor.py.

The "no new global singletons" note that used to live here is obsolete —
the plugin registry is deliberately the one central extension-point store
(see ARCHITECTURE.md); the four domain registries (tools / providers /
patterns / channels) remain unchanged alongside it.

This module is kept only as a transitional re-export; import executors from
atoms.executors or resolve them via nexus.registry.plugins.
"""

from nexus.engine.execution import ExecutionContext, ModuleExecutor  # noqa: F401
from nexus.engine.loop import TurnResult, run_agent  # noqa: F401
