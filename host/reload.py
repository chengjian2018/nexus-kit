"""Hot reloader — runtime reload of llm config / pattern / plugin /
channel.

Two reload flavors with entirely different mechanisms:

**llm config (yaml data)**: ``nexus.settings.load_config`` ships its own
(mtime_ns, size) fingerprint cache — the per-turn R1 refresh only stats
the file and re-parses when it changed. So this module only calls
``invalidate_config_cache()`` for config (defending against fingerprint
invalidation like clock skew), never force-re reads.

**pattern / plugin / channel (python code)**: mtime-based re-import. Each
registry's registrations happen at module import time (AST discovery →
import → module-level ``registry.register()``), so "reload" = re-execute
the registrations:

1. track every **imported** module under the scan roots (``apps/*/``,
   ``atoms/executors/``) — not only registering modules: changes to pure
   data modules like prompts take effect via "replaying the consumers",
   invisible forever if untracked;
2. stat and compare mtimes to find the changed modules;
3. any change → full ordered replay (see the ordering notes below);
4. the registries absorb the new objects in replace mode (see the
   per-registry notes below).

**Ordering = topological order of AST import edges**:
``importlib.reload`` only re-executes the target module itself, without
cascading to its dependencies — replaying a consumer first (route.py)
would leave it importing the old prompts objects. Note that sys.modules
insertion order must NOT be used: the import machinery inserts a module
into sys.modules *before* its body runs, so consumers actually sit in
front of their dependencies. We parse each module's source import
statements (relative imports included) to build dependency edges and run
a layered topological sort; cycles degrade to appending in original order
(the order is only a replay heuristic, not a correctness boundary). No
AST registration-predicate filtering: imported modules already passed the
registration check at import time, and the on-disk version may be a
mid-edit syntax-error file — dropping it by predicate would discard
exactly the change that most needs reload feedback (replay failure →
warning + keep the old registration).

**Full replay** (changed ∪ unchanged registering modules ∪ unchanged
non-registering modules): only re-executing a consumer itself rebinds the
dependent's refreshed new objects (new classes / new constants produced
by the re-import); replaying unchanged modules is an idempotent
re-execution at negligible cost.

**Per-registry reload semantics**:

- pattern (``nexus.registry.patterns``): ``register`` already overwrites
  the same name (reload = new Pattern replaces old). **Running sessions
  are unaffected** — ``session.pattern`` holds the old object reference
  and finishes on the old topology; new sessions get the new object.
  Session rebinding (switching to the new pattern) is done by the host
  calling :func:`rebind_sessions`.
- plugin (``nexus.registry.plugins``): by default "same factory idempotent
  / different factory rejected". After a reload the class objects are
  necessarily different (re-import produces new classes), so
  ``replace_on_conflict`` must be switched on temporarily to replace
  entries and clear the instance cache — in-flight turns keep running on
  the old executor reference they hold; new resolutions take the new
  classes.
- channel (``nexus.registry.channels``): same-name duplicate registration
  rejected by default, likewise via replace mode. **The router is not
  rebuilt**: ``nexus.channels.webhooks``' handler fetches the spec live
  from the registry per request, so a replace takes effect on the next
  request (token / default pattern already read env per request).

**Out of scope**: tools / MCP / providers. ToolRegistry registrations
happen at import time with handler closures bound to connection objects —
a re-import would register duplicated tools; MCP connections have their
own lifecycle (``McpManager.arefresh``); providers are connection config,
not business code. Restart the process when needed.

Host mount points: the ``POST /api/v1/reload`` endpoint in
``host/main.py`` (baseline established at startup via
:func:`init_baseline`); the studio system-plugins page's selective replay
(``POST /api/v1/system/reload``); the
:class:`ReloadWatcher` background poller when ``NEXUS_RELOAD_WATCH=1``.
"""

import ast
import importlib
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _discover_module_names() -> List[str]:
    """Collect the imported modules inside the scan domain, in sys.modules
    insertion order.

    Scan domain = module name prefixes: ``apps.<pkg>.<mod>`` (deeper
    nesting counts too — prompts and other non-registering support modules
    are tracked alongside), ``atoms.executors.<mod>`` and
    ``atoms.hooks.<mod>`` (agent_hooks plugins register from module level
    like executors; the studio system-plugins page classifies them as
    hot-reloadable code, so discovery must track them or their reload
    silently lands in "unknown"). nexus/ is not
    scanned (the kernel does not reload) nor is atoms/stages (stage
    instances are referenced by pattern.stages declarations; reloading
    them needs cascading pattern rebuilds — restart the process when
    needed). Files never imported in this process do not count as changes
    — a first load is normal discovery; hot reload only manages what is
    already loaded.
    """
    names: List[str] = []
    for name in list(sys.modules):  # dict insertion order = import completion order
        parts = name.split(".")
        if len(parts) >= 3 and parts[0] == "apps":
            names.append(name)
        elif (len(parts) >= 3 and parts[0] == "atoms"
              and parts[1] in ("executors", "hooks")):
            names.append(name)
    return names


def _module_path(name: str) -> Optional[Path]:
    """Module name → source file path; None for sourceless modules
    (builtin / namespace packages)."""
    mod = sys.modules.get(name)
    origin = getattr(mod, "__file__", None)
    return Path(origin) if origin else None


# Module mtime baseline (established by init_baseline / the first
# reload_changed; refreshed after a successful reload — failed ones retry
# next time)
_MODULE_MTIMES: Dict[str, float] = {}


def _changed_modules(tracked: List[str]) -> List[str]:
    """Find changed modules by mtime comparison (later than the recorded
    baseline)."""
    changed: List[str] = []
    for name in tracked:
        path = _module_path(name)
        if path is None or not path.exists():
            continue
        mtime = path.stat().st_mtime
        last = _MODULE_MTIMES.get(name)
        if last is None:
            _MODULE_MTIMES[name] = mtime  # first sighting: establish the baseline
        elif mtime > last:
            changed.append(name)
    return changed


def _reload_module(name: str) -> bool:
    """Re-import a single module; on failure warn and return False (the
    rest of the replay continues)."""
    try:
        importlib.reload(sys.modules[name])
        return True
    except Exception as e:  # noqa: BLE001 -- one module's failure must not sink the rest
        logger.warning("[reload] 重载 %s 失败（保持旧注册）: %s", name, e)
        return False


# ============================================================================
# Dependency-ordered replay
# ============================================================================

def _module_imports(name: str, tracked: Set[str]) -> Set[str]:
    """The module's source import statements that land inside the tracked
    set (relative imports resolved).

    A syntax error / read failure returns the empty set — the module is
    treated as having no dependencies (placed first in the topological
    order; its own replay success is order-independent).
    """
    path = _module_path(name)
    if path is None:
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()

    parts = name.split(".")
    candidates: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    candidates.add(node.module)
                    # from <pkg> import <mod> form: <mod> is a submodule
                    candidates.update(
                        f"{node.module}.{a.name}" for a in node.names)
            elif node.level <= len(parts) - 1:
                # relative import: level=1 counts from this module's package
                base = ".".join(parts[:-node.level])
                if node.module:
                    prefix = f"{base}.{node.module}" if base else node.module
                    candidates.add(prefix)
                    candidates.update(f"{prefix}.{a.name}" for a in node.names)
                else:
                    candidates.update(f"{base}.{a.name}" for a in node.names)
    return candidates & tracked


def _replay_order(tracked: List[str]) -> List[str]:
    """Topologically sorted replay order (dependencies before consumers),
    discovery order as the stable baseline.

    **sys.modules insertion order must not be used directly**: the import
    machinery inserts a module into sys.modules *before* its body runs —
    a consumer gets inserted ahead of the dependencies its body imports
    (route ahead of prompts); replaying in insertion order is exactly
    backwards. Here we run a layered Kahn over the AST import edges (each
    round releases the modules whose dependencies are all placed, keeping
    discovery order within the round); cycles degrade to appending the
    remainder in original order — the order is only a replay heuristic,
    not a correctness boundary.
    """
    tracked_set = set(tracked)
    deps = {name: _module_imports(name, tracked_set) for name in tracked}

    ordered: List[str] = []
    placed: Set[str] = set()
    pending = list(tracked)
    while pending:
        ready = [n for n in pending if deps[n] <= placed]
        if not ready:
            ordered += pending  # dependency cycle: drop the rest in original order
            break
        ordered += ready
        placed.update(ready)
        pending = [n for n in pending if n not in placed]
    return ordered


# ============================================================================
# Registry replace mode
# ============================================================================

class _ReplaceMode:
    """Temporarily switch the registries to same-name replacement (context
    manager).

    Reloaded classes/objects are not identical to the old ones (re-import
    produces new classes), while the plugin / channel registries default
    to "same name, different thing → reject". Replace mode is switched on
    inside the replay window and restored to strict default when it ends
    (reentrant counter windows — see the registries' replace_window).
    """

    def __init__(self):
        from nexus.registry.channels import registry as channel_registry
        from nexus.registry.plugins import registry as plugin_registry
        self._windows = [plugin_registry.replace_window(),
                         channel_registry.replace_window()]

    def __enter__(self):
        for window in self._windows:
            window.__enter__()
        return self

    def __exit__(self, *exc):
        for window in reversed(self._windows):
            window.__exit__(*exc)
        return False


# ============================================================================
# Public entry points
# ============================================================================

def init_baseline() -> None:
    """Establish the mtime baseline of all tracked modules (idempotent
    refresh).

    Called by host startup — so the first ``/api/v1/reload`` can already
    detect changes since boot (otherwise the first run would only build
    the baseline without reloading). The watcher calls it at start too.
    """
    _changed_modules(_discover_module_names())


def reload_changed() -> Dict[str, List[str]]:
    """Detect and reload changed pattern / plugin / channel modules.

    Returns:
        {"changed": [...], "reloaded": [...], "failed": [...]} — changed
        are the modules detected via mtime (prompts and other
        non-registering modules included); reloaded are the successfully
        replayed ones (including the unchanged modules replayed along);
        failed are the failures (old registration kept, retried on the
        next reload).
    """
    tracked = _discover_module_names()
    changed = _changed_modules(tracked)
    if not changed:
        return {"changed": [], "reloaded": [], "failed": []}

    # Full replay in dependency order (unchanged modules included): reload
    # does not cascade to dependencies — a consumer must re-execute itself
    # to bind the dependent's new objects (a prompts change takes effect
    # via replaying route)
    replay = _replay_order(tracked)
    reloaded: List[str] = []
    failed: List[str] = []
    with _ReplaceMode():
        for name in replay:
            if _reload_module(name):
                reloaded.append(name)
            else:
                failed.append(name)

    # Refresh the mtime baseline of the successful modules (failed ones
    # keep theirs — retried on the next reload)
    for name in reloaded:
        path = _module_path(name)
        if path is not None and path.exists():
            _MODULE_MTIMES[name] = path.stat().st_mtime

    logger.info("[reload] 变更 %s，重放 %d 个模块（失败 %s）",
                changed, len(reloaded), failed or "无")
    return {"changed": changed, "reloaded": reloaded, "failed": failed}


def reload_all() -> Dict[str, List[str]]:
    """Full-reload entry (shared by the API endpoint / CLI / watcher).

    The config cache is dropped outright (the settings fingerprint check
    is the primary defense; this one guards against clock skew — the next
    resolution naturally re-reads); code modules go through
    :func:`reload_changed`.
    """
    from nexus import settings

    settings.invalidate_config_cache()
    result = reload_changed()
    result["config"] = "invalidated"
    return result


def reload_modules(selected: List[str]) -> Dict[str, Any]:
    """Selective module reload (the studio system-plugins page): replay the selected
    tracked modules **plus their tracked consumers**, in dependency order.

    Consumers are pulled in because importlib.reload does not cascade — a
    dependency's new objects (re-imported classes / constants) only bind
    into a consumer when the consumer itself re-executes (the same
    rationale as reload_changed's full replay). Baselines refresh for the
    successfully replayed modules; failures keep theirs and retry on the
    next reload.

    Returns ``{"changed": selected, "reloaded": [...], "failed": [...],
    "unknown": [...]}`` — unknown are the requested names outside the
    tracked domain (not imported yet / kernel modules), skipped untouched.
    """
    tracked = _discover_module_names()
    tracked_set = set(tracked)
    sel = [n for n in selected if n in tracked_set]
    unknown = [n for n in selected if n not in tracked_set]
    # Reverse-import closure: every tracked module importing (transitively)
    # a selected module replays too
    rev: Dict[str, Set[str]] = {}
    for name in tracked:
        for dep in _module_imports(name, tracked_set):
            rev.setdefault(dep, set()).add(name)
    todo: Set[str] = set(sel)
    stack = list(sel)
    while stack:
        for consumer in rev.get(stack.pop(), ()):
            if consumer not in todo:
                todo.add(consumer)
                stack.append(consumer)
    replay = [n for n in _replay_order(tracked) if n in todo]
    reloaded: List[str] = []
    failed: List[str] = []
    with _ReplaceMode():
        for name in replay:
            if _reload_module(name):
                reloaded.append(name)
            else:
                failed.append(name)
    for name in reloaded:
        path = _module_path(name)
        if path is not None and path.exists():
            _MODULE_MTIMES[name] = path.stat().st_mtime
    logger.info("[reload] 选择性重载 %s（含 consumer 共 %d 个，失败 %s）",
                sel, len(reloaded), failed or "无")
    return {"changed": sel, "reloaded": reloaded, "failed": failed,
            "unknown": unknown}


def rebind_sessions(sessions: Dict, pattern_registry) -> int:
    """Rebind in-memory sessions to the registry's latest pattern objects.

    After a reload the old pattern object can still finish in-flight
    turns, but its topology/prompts stay old forever; this function
    re-fetches the registry's current value by pattern_code and rebuilds
    the node_map references. Tests asserting old-object identity on
    sessions are unaffected (rebinding is an explicit call, never
    automatic).

    Args:
        sessions: session_id -> Session dict (modified in place)
        pattern_registry: ``nexus.registry.patterns.registry``

    Returns:
        The number of rebound sessions (sessions whose pattern was
        deregistered are skipped with a warning).
    """
    rebound = 0
    for session in list(sessions.values()):
        new_pattern = pattern_registry.get(session.pattern_code)
        if new_pattern is None:
            logger.warning("[reload] 会话 %s 的 pattern '%s' 已注销，保持旧引用",
                           getattr(session, "session_id", "?"), session.pattern_code)
            continue
        if new_pattern is session.pattern:
            continue  # unchanged (that pattern was not reloaded this round)
        session.pattern = new_pattern
        session.cxt.node_map = new_pattern.node_map
        rebound += 1
    return rebound


# ============================================================================
# Optional background watcher (mtime polling; off by default)
# ============================================================================

class ReloadWatcher:
    """Background thread polling mtimes, auto-reload_all on change.

    Disabled by default in production (explicit API/CLI triggers are
    recommended — controllable and observable); enable for long-lived
    dev processes that want to skip manual triggers (env
    ``NEXUS_RELOAD_WATCH=1``). uvicorn --reload restarts the whole
    process and cannot touch in-process registries — what this watcher
    covers is exactly the "refresh the registries without restarting the
    process" scenario.
    """

    def __init__(self, interval: float = 2.0, on_reload=None):
        self._interval = interval
        self._on_reload = on_reload or reload_all
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "ReloadWatcher":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name="nexus-reload-watcher", daemon=True)
        self._thread.start()
        logger.info("[reload] watcher 已启动（interval=%.1fs）", self._interval)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2 + 1)
            self._thread = None

    def _run(self) -> None:
        init_baseline()  # avoid misjudging pre-existing files at startup as "changed"
        while not self._stop.wait(self._interval):
            try:
                result = self._on_reload()
                if result.get("changed"):
                    logger.info("[reload] watcher 自动重载: %s", result["changed"])
            except Exception:  # noqa: BLE001 -- the watcher never exits
                logger.exception("[reload] watcher 轮次异常（继续）")


_watcher: Optional[ReloadWatcher] = None


def start_watcher(interval: float = 2.0) -> ReloadWatcher:
    """Start the global watcher (idempotent; auto-started by main.py at
    boot when NEXUS_RELOAD_WATCH=1)."""
    global _watcher
    if _watcher is None:
        _watcher = ReloadWatcher(interval=interval).start()
    return _watcher


def stop_watcher() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None
