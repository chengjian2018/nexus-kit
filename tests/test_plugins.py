"""Plugin registry tests — registration semantics, conflict policy,
resolution caching, and the chat layer's AGENT node-executor resolution
(``chat._resolve_node_executor_code`` — node.plugins["loop"] >
pattern.plugins["loop"] > default_loop)."""

import pytest

import atoms.executors  # noqa: F401 -- warm up default executor plugins
from nexus.engine.chat import _resolve_node_executor_code
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.plugins import (
    DEFAULT_EXECUTOR_CODES,
    PluginRegistry,
    registry,
)


# ---------------------------------------------------------------------------
# Registration semantics
# ---------------------------------------------------------------------------

class _Thing:
    def __init__(self, name):
        self.name = name


def test_register_and_resolve_instantiates_once():
    reg = PluginRegistry()
    calls = []

    def factory():
        calls.append(1)
        return _Thing("a")

    reg.register("kind_x", "code_a", factory)
    inst1 = reg.resolve("kind_x", "code_a")
    inst2 = reg.resolve("kind_x", "code_a")
    assert inst1 is inst2  # instance cache: factory called exactly once
    assert len(calls) == 1
    assert inst1.name == "a"


def test_register_same_factory_is_idempotent():
    reg = PluginRegistry()

    def factory():
        return _Thing("a")

    reg.register("kind_x", "code_a", factory)
    reg.register("kind_x", "code_a", factory)  # same object: no raise
    assert reg.has("kind_x", "code_a")


def test_register_conflicting_factory_raises():
    reg = PluginRegistry()
    reg.register("kind_x", "code_a", lambda: _Thing("a"))
    with pytest.raises(ValueError, match="插件冲突"):
        reg.register("kind_x", "code_a", lambda: _Thing("b"))


def test_register_rejects_empty_and_non_callable():
    reg = PluginRegistry()
    with pytest.raises(ValueError):
        reg.register("", "code", lambda: None)
    with pytest.raises(ValueError):
        reg.register("kind", "", lambda: None)
    with pytest.raises(ValueError):
        reg.register("kind", "code", "not-callable")


def test_kinds_are_isolated():
    reg = PluginRegistry()
    reg.register("kind_x", "shared_code", lambda: _Thing("x"))
    reg.register("kind_y", "shared_code", lambda: _Thing("y"))
    assert reg.resolve("kind_x", "shared_code").name == "x"
    assert reg.resolve("kind_y", "shared_code").name == "y"


def test_resolve_unknown_raises_with_hint():
    reg = PluginRegistry()
    with pytest.raises(KeyError, match="atoms.executors"):
        reg.resolve("executor", "nope")


def test_has_does_not_instantiate():
    reg = PluginRegistry()
    calls = []

    def factory():
        calls.append(1)
        return _Thing("a")

    reg.register("kind_x", "code_a", factory)
    assert reg.has("kind_x", "code_a")
    assert reg.list_codes("kind_x") == ["code_a"]
    assert calls == []  # has() is a validation-time query


def test_deregister_clears_instance_cache():
    reg = PluginRegistry()
    reg.register("kind_x", "code_a", lambda: _Thing("a"))
    reg.resolve("kind_x", "code_a")
    reg.deregister("kind_x", "code_a")
    assert not reg.has("kind_x", "code_a")
    with pytest.raises(KeyError):
        reg.resolve("kind_x", "code_a")


# ---------------------------------------------------------------------------
# Default executors registered by atoms.executors
# ---------------------------------------------------------------------------

def test_default_executors_registered():
    codes = registry.list_codes("executor")
    for code in DEFAULT_EXECUTOR_CODES.values():
        assert code in codes


def test_default_executor_codes_cover_pattern_types():
    # the module types collapsed to pattern types — the route family
    # is gone with the module layer
    assert DEFAULT_EXECUTOR_CODES == {
        "agent": "default_loop",
        "fsm": "default_fsm",
    }


# ---------------------------------------------------------------------------
# Chat-layer node-executor resolution (fallback chain)
# ---------------------------------------------------------------------------

def _pattern(plugins=None, node_plugins=None) -> Pattern:
    return Pattern(
        code="p", name="t", description="d",
        plugins=plugins,
        nodes=[BaseNode(code="n", name="n", plugins=node_plugins)],
    )


def test_resolve_defaults_to_default_loop():
    pattern = _pattern()
    assert _resolve_node_executor_code(pattern, pattern.nodes[0]) == "default_loop"


def test_resolve_pattern_level_overrides_default():
    pattern = _pattern(plugins={"loop": "custom_loop"})
    assert _resolve_node_executor_code(pattern, pattern.nodes[0]) == "custom_loop"


def test_resolve_node_level_overrides_pattern():
    pattern = _pattern(plugins={"loop": "pattern_loop"},
                       node_plugins={"loop": "node_loop"})
    assert _resolve_node_executor_code(pattern, pattern.nodes[0]) == "node_loop"


def test_default_executor_code_rejects_module_era_type():
    """Module-era types (route) have no default executor any more."""
    with pytest.raises(ValueError):
        registry.default_executor_code("route")
