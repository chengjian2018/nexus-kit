"""Tests for pattern module-topology registration and registration-time fail fast."""

import pytest

from nexus.model.module import AgentModule, FSMModule, ModuleLink
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern


def _mk_pattern(modules, **kw):
    return Pattern(
        code="p_test", name="t", description="t",
        entry_module_code=modules[0].module_code, modules=modules, **kw,
    )


def test_module_map_and_node_map_registered():
    a = AgentModule(module_code="a", sub_modules=["b", ModuleLink(target="c")])
    b = AgentModule(module_code="b")
    c = FSMModule(module_code="c")
    p = _mk_pattern([a, b, c])
    assert set(p.module_map) == {"a", "b", "c"}
    # Adjacency declared via links produces no runtime graph (jump detection
    # only checks module_map membership); it is validated at registration only
    assert not hasattr(p, "dispatch_graph")


def test_dangling_link_raises():
    a = AgentModule(module_code="a", sub_modules=["ghost"])
    b = AgentModule(module_code="b")
    with pytest.raises(ValueError, match="悬空"):
        _mk_pattern([a, b])


def test_dangling_jump_module_raises():
    """A node's jump_module pointing at a nonexistent module -> dangling-reference fail fast at registration."""
    menu = BaseNode(node_code="menu_x", node_name="x", jump_module="ghost")
    root = BaseNode(node_code="root", node_name="r", sub_nodes=["menu_x"])
    route_mod = AgentModule(module_code="rt", module_nodes=[root, menu])
    with pytest.raises(ValueError, match="悬空"):
        _mk_pattern([route_mod])


def test_unauthorized_lend_raises():
    b = AgentModule(module_code="b", use_tools=["t1"])
    a = AgentModule(
        module_code="a",
        sub_modules=[ModuleLink(target="b", lend_tools=["t_not_in_b"])],
    )
    with pytest.raises(ValueError, match="借出"):
        _mk_pattern([a, b])


def test_self_loop_raises():
    a = AgentModule(module_code="a", sub_modules=["a"])
    with pytest.raises(ValueError, match="自环"):
        _mk_pattern([a])


def test_agent_to_fsm_link_allowed():
    """Mixed pattern: an AGENT -> FSM edge is legal (not blocked)."""
    a = AgentModule(module_code="a", sub_modules=["f"])
    f = FSMModule(module_code="f", module_nodes=[
        BaseNode(node_code="f1", node_name="n1", is_end=True)
    ])
    p = _mk_pattern([a, f])
    assert p.module_map["f"].type.value == "fsm"


def test_max_hops_default_and_override():
    a = AgentModule(module_code="a")
    assert _mk_pattern([a]).max_hops == 2
    assert _mk_pattern([a], max_hops=1).max_hops == 1


def test_route_jump_module_self_loop_raises():
    """M-1: jump_module pointing at its own module -> self-loop fail fast at registration."""
    menu = BaseNode(node_code="menu_self", node_name="m", jump_module="rt")
    root = BaseNode(node_code="root2", node_name="r", sub_nodes=["menu_self"])
    route_mod = AgentModule(module_code="rt", module_nodes=[root, menu])
    with pytest.raises(ValueError, match="自环"):
        _mk_pattern([route_mod])
