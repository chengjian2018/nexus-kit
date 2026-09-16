"""Studio hosted-artifact repository — fixed directories + a loader
(replayed at startup/reload).

Two fixed hosted directories (matching studio's placement in
docs/design/ops-console-prd.md §7.1 D-1):

- ``host/config/plugins/<stem>.py``    auto-generated plugin modules. The
  module-level registration idiom is the same as apps/*/route.py
  (``plugin_registry.register("executor", code, Factory)``); the first-line
  comment convention is ``# studio-plugin: file=<stem>.py`` (parsed by
  agent.py; the loader does not depend on it).
- ``host/config/patterns/<code>.yml``  console-hosted patterns. For the same
  code, the later registration wins — i.e. fork-to-edit semantics (the
  console version overrides the code version).

Load order is fixed "plugins first, then patterns": pattern validation needs
to resolve plugin codes, and if plugins are not registered first,
validate_plugin_declarations would falsely report them as unregistered.
Loading only does two kinds of things:

1. Plugins: loaded via importlib under a synthetic module name
   (``studio_plugin_<stem>``) — a brand-new module object is re-executed
   every time, and the module-level registration happens naturally during
   exec; a repeated load produces new class objects that share names with
   but are distinct from the old registrations, so the registry's replace
   window must be opened (aligned with host.reload._ReplaceMode).
2. Patterns: the ``pattern_from_yaml → validate_pattern → register``
   three-stage pipeline (the same path as the CLI ``pattern-load``).

Failure semantics: a single-file failure only logs an ERROR and is collected
into the report (visible in the studio list), never blocking the service —
broken files are skipped with degradation and everything else proceeds.
Security boundary: only existing files under these two fixed directories
are loaded.

Test-friendliness: the directory constants are module-level ``Path`` objects,
so tests can monkeypatch ``PLUGINS_DIR`` / ``PATTERNS_DIR`` as needed
(tests/test_studio_api.py isolates the repo directories exactly this way).
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from nexus.model.serialization import pattern_from_yaml
from nexus.model.validation import validate_pattern
from nexus.registry.patterns import registry as pattern_registry
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "host" / "config" / "plugins"
PATTERNS_DIR = REPO_ROOT / "host" / "config" / "patterns"

# Legal charset for hosted filenames and pattern codes (lowercase letter first, then lowercase letters/digits/underscores)
_FILENAME_STEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# The latest load report (the studio list page shows bad files from it; a process-global with no concurrent write races —
# loading happens only at the three serialized entries: startup / reload / apply)
_last_report: Dict[str, Any] = {
    "plugins": {"loaded": [], "failed": {}},
    "patterns": {"loaded": [], "failed": {}},
}


# ---------------------------------------------------------------------------
# Directory views
# ---------------------------------------------------------------------------

def plugin_files(plugins_dir: Optional[Path] = None) -> List[Path]:
    """Hosted plugin files (stable order; __init__ and other underscore-prefixed files skipped)."""
    base = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.glob("*.py")
        if not p.name.startswith("_")
        and _FILENAME_STEM_RE.match(p.stem)
    )


def pattern_files(patterns_dir: Optional[Path] = None) -> List[Path]:
    """Hosted pattern files (both .yml/.yaml accepted, stable order)."""
    base = Path(patterns_dir) if patterns_dir is not None else PATTERNS_DIR
    if not base.is_dir():
        return []
    return sorted(list(base.glob("*.yml")) + list(base.glob("*.yaml")))


def console_pattern_codes(patterns_dir: Optional[Path] = None) -> Set[str]:
    """Persisted console pattern codes (the source-badge decision basis)."""
    return {p.stem for p in pattern_files(patterns_dir)}


def last_report() -> Dict[str, Any]:
    return _last_report


def pattern_load_error(code: str) -> Optional[str]:
    """A console pattern's latest load failure reason (None = no record / success)."""
    return _last_report["patterns"]["failed"].get(code)


# ---------------------------------------------------------------------------
# Write / delete (the persistence face of apply / publish / fork; the stem whitelist IS the security boundary)
# ---------------------------------------------------------------------------

def check_stem(stem: str) -> str:
    """Validate a hosted filename stem (plugin files and pattern codes share one legal charset)."""
    if not _FILENAME_STEM_RE.match(stem or ""):
        raise ValueError(
            f"非法文件名/code: {stem!r}（需小写字母开头，仅小写字母/数字/下划线，"
            f"长度 1-64）")
    return stem


def write_plugin_file(stem: str, code_text: str,
                      plugins_dir: Optional[Path] = None) -> Path:
    """Write (overwrite) one hosted plugin module file."""
    check_stem(stem)
    base = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{stem}.py"
    path.write_text(code_text, encoding="utf-8")
    return path


def write_pattern_file(code: str, yaml_text: str,
                       patterns_dir: Optional[Path] = None) -> Path:
    """Write (overwrite) one hosted pattern YAML file."""
    check_stem(code)
    base = Path(patterns_dir) if patterns_dir is not None else PATTERNS_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{code}.yml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def delete_pattern_file(code: str,
                        patterns_dir: Optional[Path] = None) -> bool:
    """Delete a hosted pattern file (False when absent; each suffix tried in turn)."""
    check_stem(code)
    base = Path(patterns_dir) if patterns_dir is not None else PATTERNS_DIR
    removed = False
    for suffix in (".yml", ".yaml"):
        path = base / f"{code}{suffix}"
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def delete_plugin_file(stem: str,
                       plugins_dir: Optional[Path] = None) -> bool:
    base = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    path = base / f"{stem}.py"
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Plugin module loading
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _plugin_replace_window():
    """Temporarily open the plugin registry's same-name replacement window (a re-exec necessarily produces new class objects,
    which strict mode would reject as same-name re-registrations; the semantics match host.reload._ReplaceMode).

    The registry side is a lock-protected reentrant counting window: when apply (threadpool) and generate (event
    loop) open windows concurrently they cannot overwrite each other's restore value and permanently wedge the
    process-level switch open."""
    with plugin_registry.replace_window():
        yield


def plugin_module_name(stem: str) -> str:
    return f"studio_plugin_{stem}"


def import_plugin_module(path: Path, stem: Optional[str] = None) -> str:
    """Exec a plugin module file under a synthetic module name, returning the module name.

    Every call builds a fresh module object (overwriting the sys.modules entry of the same name) — repeated loading =
    re-exec + re-registration inside the replacement window, naturally supporting reload replay and refresh after
    apply. On an exec failure the sys.modules placeholder is reclaimed and the error propagates (the caller decides
    to degrade or abort).
    """
    name = plugin_module_name(check_stem(stem if stem is not None else path.stem))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法为插件文件构建 import spec: {path}")
    module = importlib.util.module_from_spec(spec)
    with _plugin_replace_window():
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return name


def load_pattern_text(yaml_text: str):
    """Three-stage single-file load: construct → validate → register (the same path as the CLI pattern-load,
    but lenient on the tool surface — a generation-workbench app's pattern in the hosted directory may reference
    tools registered later; strict validation would make the startup replay load fail)."""
    pattern = pattern_from_yaml(yaml_text)
    validate_pattern(pattern, strict_tools=False)
    pattern_registry.register(pattern)
    return pattern


# ---------------------------------------------------------------------------
# Full loading (the startup / reload replay entry)
# ---------------------------------------------------------------------------

def load_console_artifacts(plugins_dir: Optional[Path] = None,
                           patterns_dir: Optional[Path] = None,
                           ) -> Dict[str, Any]:
    """Replay the hosted directories: plugins first, then patterns; returns the report and records it in _last_report.

    The report looks like::

        {"plugins": {"loaded": ["a.py"], "failed": {"b.py": "<error>"}},
         "patterns": {"loaded": ["p1"], "failed": {"p2": "<error>"}}}
    """
    global _last_report
    report: Dict[str, Any] = {"plugins": {"loaded": [], "failed": {}},
                              "patterns": {"loaded": [], "failed": {}}}

    for path in plugin_files(plugins_dir):
        try:
            import_plugin_module(path)
            report["plugins"]["loaded"].append(path.name)
        except Exception as e:
            logger.exception("studio 插件装载失败，跳过: %s", path.name)
            report["plugins"]["failed"][path.name] = str(e)

    for path in pattern_files(patterns_dir):
        try:
            text = path.read_text(encoding="utf-8")
            pattern = load_pattern_text(text)
            report["patterns"]["loaded"].append(pattern.code)
        except Exception as e:
            logger.exception("studio pattern 装载失败，跳过: %s", path.name)
            report["patterns"]["failed"][path.stem] = str(e)

    _last_report = report
    loaded = (len(report["plugins"]["loaded"])
              + len(report["patterns"]["loaded"]))
    failed = (len(report["plugins"]["failed"])
              + len(report["patterns"]["failed"]))
    logger.info("studio 托管产物装载完成: 成功 %d，失败 %d", loaded, failed)
    return report
