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
- ``stage``           : named stages (the string codes referenced by
                       stages declarations); registered by
                       atoms/stages/__init__ and app-owned stages.
- ``messages_builder``: AGENT messages builders (the kernel registers the
                       "default" builder; apps may register their own).
- ``agent_hooks``     : hooks packages (atoms/hooks/tool_guard.py is the
                       in-repo package — P4 dangerous-op announce; further
                       packages register the same way).

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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nexus.registry.discovery import import_modules, module_registers

logger = logging.getLogger(__name__)

# Factory: zero-arg callable returning the plugin instance. Executors must be
# stateless (all state lives on cxt) — resolve() caches one instance per
# (kind, code) and reuses it across sessions.
Factory = Callable[[], Any]

# Default executor codes per pattern type (fallback chain tail:
# node.plugins[slot] > pattern.plugins[slot] > these codes)
DEFAULT_EXECUTOR_CODES: Dict[str, str] = {
    "agent": "default_loop",
    "fsm": "default_fsm",
}


class PluginRegistry:
    """Central plugin registry: (kind, code) -> factory, with instance caching."""

    def __init__(self):
        self._factories: Dict[tuple, Factory] = {}
        self._instances: Dict[tuple, Any] = {}
        # Owner module per (kind, code) — the factory's defining module
        # (``factory.__module__``). Observability metadata for the ops UI
        # (the studio "system plugins" page): studio_plugin_<stem> → hosted
        # plugins; apps.* / atoms.executors.* → code modules (selectively
        # hot-reloadable); nexus.* → kernel.
        self._owners: Dict[tuple, str] = {}
        self._lock = threading.RLock()
        # Hot-reload window switch: when open, registering a
        # same-name/different-factory plugin becomes "replace + drop
        # instance cache" instead of rejection. Reentrant DEPTH counter
        # guarded by the registry lock — concurrent openers (studio apply
        # in the threadpool vs generate on the event loop) can no longer
        # restore each other's save/restore of a plain bool and leave the
        # process-wide switch stuck open. Default closed — strict mode
        # intercepts genuine same-name conflicts.
        self._replace_depth = 0

    @property
    def replace_on_conflict(self) -> bool:
        """True while at least one replace window is open."""
        return self._replace_depth > 0

    @replace_on_conflict.setter
    def replace_on_conflict(self, value: bool) -> None:
        """Legacy direct assignment: maps to depth 1/0 (non-reentrant —
        fine for sequential tests/callers; nested or concurrent use must
        go through :meth:`replace_window`)."""
        with self._lock:
            self._replace_depth = 1 if value else 0

    @contextmanager
    def replace_window(self):
        """Reentrant, interleave-safe replace window (see the counter note
        in ``__init__``); held by host.reload._ReplaceMode and the studio
        store's plugin re-exec path."""
        with self._lock:
            self._replace_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._replace_depth -= 1

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, kind: str, code: str, factory: Factory) -> None:
        """Register a plugin factory under (kind, code).

        Raises ValueError when the slot is already taken by a different
        factory (same-name conflict); re-registering the identical factory is
        an idempotent no-op. With ``replace_on_conflict`` set (hot-reload
        window), a different factory replaces the entry and drops its cached
        instance instead — re-imported classes are never the same object.
        """
        if not kind or not code:
            raise ValueError(f"plugin kind/code 不能为空: kind={kind!r}, code={code!r}")
        if not callable(factory):
            raise ValueError(f"plugin factory 必须可调用: kind={kind!r}, code={code!r}")
        key = (kind, code)
        owner = getattr(factory, "__module__", "") or ""
        with self._lock:
            existing = self._factories.get(key)
            if existing is not None and existing is not factory:
                if not self.replace_on_conflict:
                    raise ValueError(
                        f"插件冲突: ({kind!r}, {code!r}) 已被其它 factory 注册"
                        f"（同名插件不允许，请更换 code）"
                    )
                self._factories[key] = factory
                self._instances.pop(key, None)
                self._owners[key] = owner
                logger.info("Replaced plugin (reload): kind=%s, code=%s", kind, code)
                return
            if existing is factory:
                return  # idempotent re-registration (module re-import)
            self._factories[key] = factory
            self._owners[key] = owner
        logger.info("Registered plugin: kind=%s, code=%s", kind, code)

    def deregister(self, kind: str, code: str) -> None:
        """Remove a plugin registration (and its cached instance)."""
        key = (kind, code)
        with self._lock:
            self._factories.pop(key, None)
            self._instances.pop(key, None)
            self._owners.pop(key, None)

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

    def owner_of(self, kind: str, code: str) -> str:
        """The factory's defining module for (kind, code)（"" when unknown）."""
        with self._lock:
            return self._owners.get((kind, code), "")

    def default_executor_code(self, pattern_type_value: str) -> str:
        """The default executor code for a pattern type ("fsm"/"agent")."""
        code = DEFAULT_EXECUTOR_CODES.get(pattern_type_value)
        if code is None:
            raise ValueError(
                f"pattern type {pattern_type_value!r} 没有默认 executor code"
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
    """Import self-registering plugin modules under atoms/executors/ and
    atoms/hooks/ and return their names.

    A file is imported iff it carries a top-level ``registry.register(...)``
    call (shared AST scanner); atoms/executors/__init__.py registers the
    three default executors, and custom executor / hooks files self-register
    the same way. ``plugins_dir`` (compat) narrows the scan to that single
    directory instead of the two builtin ones.
    """
    if plugins_dir is not None:
        search_dirs = [Path(plugins_dir)]
    else:
        atoms_base = Path(__file__).resolve().parents[2] / "atoms"
        search_dirs = [atoms_base / "executors", atoms_base / "hooks"]
    module_names = []
    for directory in search_dirs:
        package = f"atoms.{directory.name}"
        for path in sorted(directory.glob("*.py")):
            if path.name != "__init__.py" and module_registers(path):
                module_names.append(f"{package}.{path.stem}")
    return import_modules(module_names, what="plugin module")
