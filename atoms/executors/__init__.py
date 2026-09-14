"""Executor atoms: the default node/pattern executors (agent loop / FSM).

Importing this package registers the kernel's builtin executors into the
plugin registry (kind="executor"): default_loop / default_fsm. The kernel
never imports atom implementations — this warm-up (at host bootstrap and
tests/conftest.py) is what makes the chat layer's plugin dispatch runnable,
mirroring atoms.stages' registration of default stage factories.

There is deliberately no default_route executor (the ROUTE module type is
gone; routing apps declare an AGENT graph whose routing nodes are plain
loop executors).
"""

from atoms.executors.loop_executor import DefaultLoopExecutor
from atoms.executors.fsm_executor import DefaultFSMExecutor
from nexus.registry.plugins import registry

registry.register("executor", "default_loop", DefaultLoopExecutor)
registry.register("executor", "default_fsm", DefaultFSMExecutor)
