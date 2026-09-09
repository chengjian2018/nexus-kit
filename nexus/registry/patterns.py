"""Pattern registry.

Each pattern file registers a Pattern at module level via ``registry.register()``;
the system auto-discovers and imports these files through AST scanning.

Follows the same registration pattern as llm/register.py.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from nexus.model.pattern import Pattern
from nexus.registry.discovery import import_modules, module_registers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-discovery helpers
# ---------------------------------------------------------------------------

def discover_builtin_patterns(apps_dir: Optional[Path] = None) -> List[str]:
    """Import self-registering pattern modules under apps/ and return their names.

    Every directory under ``apps/`` is one app (route.py / prompts.py /
    channel.py); a file is imported iff it carries a top-level
    ``registry.register()`` call, so prompts and channel adapters are skipped
    by the AST check itself — no exclusion list needed.
    """
    apps_path = (
        Path(apps_dir) if apps_dir is not None
        else Path(__file__).resolve().parents[2] / "apps"
    )
    module_names = []
    if apps_path.is_dir():
        for app_dir in sorted(p for p in apps_path.iterdir() if p.is_dir()):
            for path in sorted(app_dir.glob("*.py")):
                if path.name != "__init__.py" and module_registers(path):
                    module_names.append(f"apps.{app_dir.name}.{path.stem}")
    return import_modules(module_names, what="pattern module")


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

class PatternRegistry:
    """Singleton Pattern registry.

    Supports two registration styles:

    1. Passing a Pattern object directly:
       registry.register(pattern)

    2. Passing constructor args (backward compatible):
       registry.register("001", name="test", description="test template")
    """

    def __init__(self):
        self._patterns: Dict[str, Pattern] = {}
        self._lock = threading.RLock()
        self._generation: int = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        code_or_pattern: Any,
        name: str = "",
        description: str = "",
        modules: Optional[Dict[str, Any]] = None,
        nodes: Optional[Dict[str, Any]] = None,
        stages: Optional[List[Any]] = None,
        entry_module_code: str = "",
        llm_provider_code: str = "",
        **kwargs,
    ) -> Pattern:
        """Register a Pattern.

        Args:
            code_or_pattern: a Pattern object, or the pattern's code string.
            name: pattern name (used when the first arg is a string).
            description: pattern description.
            modules: module dict.
            nodes: node dict.
            stages: Pipeline stage list.
            entry_module_code: entry module code.
            llm_provider_code: LLM provider code.
            **kwargs: extra args passed to the Pattern constructor.

        Returns:
            the registered Pattern object.
        """
        with self._lock:
            if isinstance(code_or_pattern, Pattern):
                pattern = code_or_pattern
            else:
                pattern = Pattern(
                    code=code_or_pattern,
                    name=name,
                    description=description,
                    modules=modules,
                    stages=stages,
                    entry_module_code=entry_module_code,
                    **kwargs
                )

            code = pattern.code
            if code in self._patterns:
                logger.warning(
                    "Pattern '%s' is already registered; overwriting.", code
                )

            self._patterns[code] = pattern
            self._generation += 1
            logger.info("Registered pattern: %s (%s)", code, pattern.name)
            return pattern

    def deregister(self, code: str) -> None:
        """Remove a registered Pattern."""
        with self._lock:
            if code not in self._patterns:
                return
            del self._patterns[code]
            self._generation += 1
        logger.info("Deregistered pattern: %s", code)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, code: str) -> Optional[Pattern]:
        """Get a Pattern by code."""
        with self._lock:
            return self._patterns.get(code)

    def list_patterns(self) -> List[Pattern]:
        """List all registered Patterns."""
        with self._lock:
            return list(self._patterns.values())

    def list_codes(self) -> List[str]:
        """List all registered Pattern codes."""
        with self._lock:
            return sorted(self._patterns.keys())

    def is_registered(self, code: str) -> bool:
        """Check whether a code is registered."""
        with self._lock:
            return code in self._patterns


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

registry = PatternRegistry()