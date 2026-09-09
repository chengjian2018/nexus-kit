"""Plugin registry tests — registration semantics, conflict policy,
resolution caching, and the chat layer's executor dispatch."""

import pytest

import atoms.executors  # noqa: F401 -- warm up default executor plugins
from nexus.engine.chat import _resolve_executor_code
from nexus.model.module import AgentModule, FSMModule, RouteModule
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


def test_default_executor_codes_cover_module_types():
    assert DEFAULT_EXECUTOR_CODES == {
        "agent": "default_loop",
        "fsm": "default_fsm",
        "route": "default_route",
    }


# ---------------------------------------------------------------------------
# Chat-layer executor resolution (fallback chain)
# ---------------------------------------------------------------------------

class _ShimPattern:
    """Pattern stand-in exposing only the executor declarations."""

    def __init__(self, executor_loop=None, executor_fsm=None,
                 executor_route=None):
        self.executor_loop = executor_loop
        self.executor_fsm = executor_fsm
        self.executor_route = executor_route


class _ShimSession:
    def __init__(self, pattern):
        self.pattern = pattern


def test_resolve_defaults_by_module_type():
    session = _ShimSession(_ShimPattern())
    assert _resolve_executor_code(session, AgentModule()) == "default_loop"
    assert _resolve_executor_code(session, FSMModule()) == "default_fsm"
    assert _resolve_executor_code(session, RouteModule()) == "default_route"


def test_resolve_pattern_level_overrides_default():
    session = _ShimSession(_ShimPattern(executor_loop="custom_loop",
                                        executor_fsm="custom_fsm",
                                        executor_route="custom_route"))
    assert _resolve_executor_code(session, AgentModule()) == "custom_loop"
    assert _resolve_executor_code(session, FSMModule()) == "custom_fsm"
    assert _resolve_executor_code(session, RouteModule()) == "custom_route"


def test_resolve_module_level_overrides_pattern():
    session = _ShimSession(_ShimPattern(executor_loop="pattern_loop"))
    module = AgentModule(executor="module_loop")
    assert _resolve_executor_code(session, module) == "module_loop"


def test_resolve_unknown_module_type_raises():
    session = _ShimSession(_ShimPattern())
    weird = AgentModule()
    weird.type = "quantum"  # bare string not in DEFAULT_EXECUTOR_CODES
    with pytest.raises((AttributeError, KeyError, ValueError)):
        _resolve_executor_code(session, weird)
