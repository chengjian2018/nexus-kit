"""Plugin registry — the central store for swappable engine extension points.

Kinds of plugins (kind is a plain string, so new extension points do not need
registry API changes):

- ``executor``       : module executors (agent loop / FSM pipeline / ROUTE
                       pipeline); interface ModuleExecutor.execute(ec) ->
                       TurnResult (nexus/engine/execution.py). Default
                       implementations live in atoms/executors/ — the kernel
                       never imports them; the host/test warm-up registers
                       them (same pattern as register_default_generate).
- ``stage_factory`` : builtin default-stage factories (internal storage
                       behind pipeline.register_default_generate/_clarify;
                       the public pipeline API stays unchanged).
- future kinds (``stage``, ``messages_builder``, ``agent_hooks``, ...) are
  added by later refactor plans without touching this registry.

Registration idiom (same as the four domain registries, discovered by the
shared AST scanner in nexus/registry/discovery.py):

    from nexus.registry.plugins import registry
    registry.register("executor", "default_loop", DefaultLoopExecutor)

Conflict policy: registering a (kind, code) already taken by a **different**
factory raises ValueError (a genuine same-name/different-thing clash);
re-registering the same factory is idempotent (test suites may import atom
modules more than once).
"""

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nexus.registry.discovery import import_modules, module_registers

logger = logging.getLogger(__name__)

# Factory: zero-arg callable returning the plugin instance. Executors must be
# stateless (all state lives on cxt) — resolve() caches one instance per
# (kind, code) and reuses it across sessions.
Factory = Callable[[], Any]

# Default executor codes per module type (fallback chain tail:
# module.executor > pattern.executor_<type> > these codes)
DEFAULT_EXECUTOR_CODES: Dict[str, str] = {
    "agent": "default_loop",
    "fsm": "default_fsm",
    "route": "default_route",
}


class PluginRegistry:
    """Central plugin registry: (kind, code) -> factory, with instance caching."""

    def __init__(self):
        self._factories: Dict[tuple, Factory] = {}
        self._instances: Dict[tuple, Any] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, kind: str, code: str, factory: Factory) -> None:
        """Register a plugin factory under (kind, code).

        Raises ValueError when the slot is already taken by a different
        factory (same-name conflict); re-registering the identical factory is
        an idempotent no-op.
        """
        if not kind or not code:
            raise ValueError(f"plugin kind/code 不能为空: kind={kind!r}, code={code!r}")
        if not callable(factory):
            raise ValueError(f"plugin factory 必须可调用: kind={kind!r}, code={code!r}")
        key = (kind, code)
        with self._lock:
            existing = self._factories.get(key)
            if existing is not None and existing is not factory:
                raise ValueError(
                    f"插件冲突: ({kind!r}, {code!r}) 已被其它 factory 注册"
                    f"（同名插件不允许，请更换 code）"
                )
            if existing is factory:
                return  # idempotent re-registration (module re-import)
            self._factories[key] = factory
        logger.info("Registered plugin: kind=%s, code=%s", kind, code)

    def deregister(self, kind: str, code: str) -> None:
        """Remove a plugin registration (and its cached instance)."""
        key = (kind, code)
        with self._lock:
            self._factories.pop(key, None)
            self._instances.pop(key, None)

    # ------------------------------------------------------------------
    # Resolution / queries
    # ------------------------------------------------------------------

    def resolve(self, kind: str, code: str) -> Any:
        """Resolve (kind, code) to a plugin instance (factory called once, then cached).

        Executors are stateless by contract, so the cached instance is safely
        shared across sessions.
        """
        key = (kind, code)
        with self._lock:
            instance = self._instances.get(key)
            if instance is not None:
                return instance
            factory = self._factories.get(key)
            if factory is None:
                raise KeyError(
                    f"插件未注册: kind={kind!r}, code={code!r}"
                    f"（请 import 对应原子模块，如 atoms.executors）"
                )
            instance = factory()
            self._instances[key] = instance
            return instance

    def has(self, kind: str, code: str) -> bool:
        """Check registration without instantiating (validation-time query)."""
        with self._lock:
            return (kind, code) in self._factories

    def list_codes(self, kind: str) -> List[str]:
        """List registered codes of a kind (sorted)."""
        with self._lock:
            return sorted(code for (k, code) in self._factories if k == kind)

    def default_executor_code(self, module_type_value: str) -> str:
        """The default executor code for a module type value (ModuleType.X.value)."""
        code = DEFAULT_EXECUTOR_CODES.get(module_type_value)
        if code is None:
            raise ValueError(
                f"module type {module_type_value!r} 没有默认 executor code"
            )
        return code


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

registry = PluginRegistry()


# ---------------------------------------------------------------------------
# Auto-discovery (same idiom as the four domain registries)
# ---------------------------------------------------------------------------

def discover_builtin_plugins(plugins_dir: Optional[Path] = None) -> List[str]:
    """Import self-registering plugin modules under atoms/executors/ and return their names.

    A file is imported iff it carries a top-level ``registry.register(...)``
    call (shared AST scanner); atoms/executors/__init__.py registers the
    three default executors, and custom executor files self-register the
    same way.
    """
    plugins_path = (
        Path(plugins_dir) if plugins_dir is not None
        else Path(__file__).resolve().parents[2] / "atoms" / "executors"
    )
    module_names = [
        f"atoms.executors.{path.stem}"
        for path in sorted(plugins_path.glob("*.py"))
        if path.name != "__init__.py"
        and module_registers(path)
    ]
    return import_modules(module_names, what="plugin module")
