"""Architecture gate — the executable form of the layering discipline.

Layers (may only import downwards):

    host (3) -> apps (2) -> atoms (1) -> nexus (0)

Additional invariants:
- no module may import the legacy flat roots from the old hermes-nexus layout
  (dialogue/chat/stages/tools/channel/llm/config/database/augmentation/
  model_tools/prompt/main/cli) — those packages must not exist here;
- nexus must not import atoms/apps/host (kernel purity: the kernel's builtin
  fallbacks go through registration hooks, never direct imports).

This test walks every Import/ImportFrom node anywhere in each file (including
lazy imports inside functions), so it catches runtime-only violations that
import-linter configs written per-package sometimes miss.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# layer index by top-level package name
LAYERS = {"nexus": 0, "atoms": 1, "apps": 2, "host": 3}
ALLOWED_TARGETS = {
    0: {"nexus"},
    1: {"nexus", "atoms"},
    2: {"nexus", "atoms", "apps"},
    3: {"nexus", "atoms", "apps", "host"},
}

# legacy roots from the old single-package layout — must never be imported
LEGACY_ROOTS = {
    "dialogue", "chat", "stages", "tools", "channel", "llm", "config",
    "database", "augmentation", "model_tools", "prompt", "main", "cli",
}

STDLIB_ALLOWLIST = {
    "__future__", "abc", "argparse", "ast", "asyncio", "collections",
    "concurrent", "contextlib", "dataclasses", "datetime", "enum", "functools",
    "hashlib", "importlib", "inspect", "itertools", "json", "logging", "math",
    "os", "pathlib", "random", "re", "sqlite3", "sys", "threading", "time",
    "typing", "uuid",
}


def _iter_module_roots(tree: ast.AST):
    """Yield (root, lineno) for every import statement anywhere in the module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module.split(".")[0], node.lineno


def _collect_violations():
    violations = []
    for py in sorted(REPO_ROOT.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT)
        if rel.parts[0] not in LAYERS:
            continue  # tests/conftest/root scripts are unrestricted
        layer = LAYERS[rel.parts[0]]
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for root, lineno in _iter_module_roots(tree):
            if root in LEGACY_ROOTS:
                violations.append(
                    f"{rel}:{lineno} imports legacy root '{root}.*' — "
                    f"use the nexus/atoms/apps/host layout")
            elif root in LAYERS and root not in ALLOWED_TARGETS[layer]:
                violations.append(
                    f"{rel}:{lineno} layer '{rel.parts[0]}' imports "
                    f"'{root}.*' — allowed: {sorted(ALLOWED_TARGETS[layer])}")
    return violations


def test_layer_boundaries():
    violations = _collect_violations()
    assert not violations, "\n".join(violations)


def test_legacy_flat_packages_absent():
    leftovers = [p.name for p in REPO_ROOT.iterdir()
                 if p.is_dir() and p.name in LEGACY_ROOTS]
    assert not leftovers, f"legacy package dirs still present: {leftovers}"
