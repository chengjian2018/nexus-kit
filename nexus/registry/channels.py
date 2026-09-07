"""Channel registry — the 4th registry, isomorphic with pattern/tool/provider.

Each channel module self-registers with a module-level
``registry.register(Spec())``; the AST scan auto-discovers and imports them
(in-repo via importlib.import_module; out-of-repo directories — e.g. test
tmp dirs — via spec_from_file_location). Default scan covers
``atoms/channels/`` (generic adapters) and every app directory under
``apps/`` (business adapters live next to their patterns).
"""

import ast
import importlib
import importlib.util
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")  # used in URL paths: lowercase snake/kebab


def _is_registry_register_call(node: ast.AST) -> bool:
    """True when *node* is a module-top-level ``registry.register(...)`` expression."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def _module_registers_channel(module_path: Path) -> bool:
    """True when the module contains a module-level registry.register() call (text pre-filter + AST)."""
    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "registry" not in source or "register" not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError:
        return False
    return any(_is_registry_register_call(stmt) for stmt in tree.body)


_EXCLUDED = {"__init__.py", "register.py", "base.py", "webhooks.py"}
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_dotted_name(path: Path) -> Optional[str]:
    """Dotted module name for an in-repo channel file; None when out-of-repo."""
    try:
        rel = path.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        return None
    parts = rel.with_suffix("").parts
    if len(parts) < 2 or parts[-1] == "__init__":
        return None
    return ".".join(parts)


def _import_channel_module(path: Path) -> Optional[str]:
    """Import a single channel file, returning the module name; on failure log a warning and return None.

    In-repo files go through import_module (re-import stays idempotent);
    out-of-repo (test tmp dirs) go through spec_from_file_location, with
    the module name ``_channel_ext_<stem>`` to avoid clashing with real modules.
    """
    try:
        dotted = _repo_dotted_name(path)
        if dotted is not None:
            mod_name = importlib.import_module(dotted).__name__
            return mod_name
        mod_name = f"_channel_ext_{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return mod_name
    except Exception as e:
        logger.warning("Could not import channel module %s: %s", path, e)
        return None


def discover_builtin_channels(channels_dir: Optional[Path] = None) -> List[str]:
    """Scan channel files and import those with a module-level registry.register().

    Default scan: ``atoms/channels/*.py`` plus every ``apps/*/`` directory.
    An explicit ``channels_dir`` (tests) scans just that directory.

    Returns: list of successfully imported module names (a file that fails to
    import is skipped with a warning).
    """
    if channels_dir is not None:
        scan_dirs = [Path(channels_dir)]
    else:
        scan_dirs = [_REPO_ROOT / "atoms" / "channels"]
        apps_root = _REPO_ROOT / "apps"
        if apps_root.is_dir():
            scan_dirs += [p for p in sorted(apps_root.iterdir()) if p.is_dir()]

    imported: List[str] = []
    for scan_dir in scan_dirs:
        for path in sorted(scan_dir.glob("*.py")):
            if path.name in _EXCLUDED or not _module_registers_channel(path):
                continue
            mod_name = _import_channel_module(path)
            if mod_name is not None:
                imported.append(mod_name)
    return imported


class ChannelRegistry:
    """Channel registry: name -> spec. Structural validation happens at register
    time, so bad declarations are stopped at import time."""

    def __init__(self):
        self._channels: dict = {}
        self._lock = threading.RLock()

    def register(self, spec: Any) -> Any:
        """Register a channel spec; raises ValueError on an invalid/duplicate name or non-callable hooks."""
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"channel name 非法（须匹配 {_NAME_RE.pattern}）: {name!r}")
        for hook in ("parse", "build_reply"):
            if not callable(getattr(spec, hook, None)):
                raise ValueError(f"channel '{name}' 的 {hook} 不可调用")
        with self._lock:
            if name in self._channels:
                raise ValueError(f"channel '{name}' 已注册（防同名冲突）")
            self._channels[name] = spec
        logger.info("Registered channel: %s", name)
        return spec

    def get(self, name: str) -> Optional[Any]:
        with self._lock:
            return self._channels.get(name)

    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._channels.keys())

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._channels


registry = ChannelRegistry()
