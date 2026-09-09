"""Shared AST auto-discovery helpers for the registries.

Every registry (tools / providers / patterns / channels / plugins) discovers
self-registering modules the same way: a file is imported iff it carries a
top-level ``registry.register(...)`` call expression. The four registries
each carried a private copy of this scanner; it is consolidated here (the
channel registry keeps its out-of-repo import bridge locally — only the AST
predicate is shared).

The idiom is deliberately "module-level ``registry.register()`` + AST scan"
(no decorator): the scanner matches the ``registry.register(...)`` call
expression itself, and decorators would need a second discovery rule for no
gain.
"""

import ast
import importlib
import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def _is_registry_register_call(node: ast.AST) -> bool:
    """Return True when *node* is a ``registry.register(...)`` call expression."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def module_registers(module_path: Path) -> bool:
    """Return True when the module contains a top-level ``registry.register(...)`` call.

    Only inspects module-body statements so that helper modules which happen
    to call ``registry.register()`` inside a function are not picked up.

    A cheap text prefilter avoids the ``ast.parse`` cost for files that do not
    mention both ``registry`` and ``register`` — a necessary condition for a
    top-level ``registry.register()`` call to exist.
    """
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


def import_modules(module_names: List[str], what: str = "module") -> List[str]:
    """Import the given dotted module names, tolerating individual failures.

    Returns the successfully imported names (a module that fails to import is
    skipped with a warning — one bad atom must not take the host down).
    """
    imported: List[str] = []
    for mod_name in module_names:
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as e:
            logger.warning("Could not import %s %s: %s", what, mod_name, e)
    return imported
