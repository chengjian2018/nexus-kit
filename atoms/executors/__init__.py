"""Executor atoms: the default module executors (agent loop / FSM / ROUTE).

Importing this package registers the kernel's builtin executors into the
plugin registry (kind="executor"): default_loop / default_fsm /
default_route. The kernel never imports atom implementations — this warm-up
(at host bootstrap and tests/conftest.py) is what makes the chat layer's
plugin dispatch runnable, mirroring atoms.stages' registration of default
stage factories.
"""

from atoms.executors.loop_executor import DefaultLoopExecutor
from atoms.executors.fsm_executor import DefaultFSMExecutor
from atoms.executors.route_executor import DefaultRouteExecutor
from nexus.registry.plugins import registry

registry.register("executor", "default_loop", DefaultLoopExecutor)
registry.register("executor", "default_fsm", DefaultFSMExecutor)
registry.register("executor", "default_route", DefaultRouteExecutor)
