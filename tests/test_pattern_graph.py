"""Tests for Pattern construction-time compilation (plan-⑧):
node codes / sub_nodes edges / entry resolution / pattern_type dispatch
semantics / config single-source folding / slots & stages FSM-only."""

import pytest

from nexus.model.node import BaseNode
from nexus.model.pattern import DEFAULT_MAX_STEPS, Pattern


def test_duplicate_node_code_raises():
    with pytest.raises(ValueError, match="重复"):
        Pattern(code="p", name="n", description="d",
                nodes=[BaseNode(code="a"), BaseNode(code="a")])


def test_dangling_sub_nodes_edge_raises():
    with pytest.raises(ValueError, match="悬空边"):
        Pattern(code="p", name="n", description="d",
                nodes=[BaseNode(code="a", sub_nodes=["ghost"])])


def test_entry_node_code_validated():
    with pytest.raises(ValueError, match="entry_node_code"):
        Pattern(code="p", name="n", description="d",
                nodes=[BaseNode(code="a")], entry_node_code="ghost")
    p = Pattern(code="p", name="n", description="d",
                nodes=[BaseNode(code="a"), BaseNode(code="b")],
                entry_node_code="b")
    assert p.entry_node_code == "b"


def test_entry_defaults_to_first_node():
    p = Pattern(code="p", name="n", description="d",
                nodes=[BaseNode(code="a"), BaseNode(code="b")])
    assert p.entry_node_code == "a"


def test_default_pattern_type_agent():
    p = Pattern(code="p", name="n", description="d")
    assert p.pattern_type == "agent"


def test_illegal_pattern_type_raises():
    with pytest.raises(ValueError, match="pattern_type"):
        Pattern(code="p", name="n", description="d", pattern_type="route")


def test_empty_nodes_autocreate_default():
    p = Pattern(code="solo", name="单节点", description="d")
    assert [n.code for n in p.nodes] == ["solo"]
    assert p.entry_node_code == "solo"
    assert p.node_map["solo"].name == "单节点"


def test_agent_node_slots_raises():
    with pytest.raises(ValueError, match="slots"):
        Pattern(code="p", name="n", description="d",
                pattern_type="agent",
                nodes=[BaseNode(code="a", slots={"s": "v"})])


def test_agent_pattern_stages_raises():
    with pytest.raises(ValueError, match="stages"):
        Pattern(code="p", name="n", description="d",
                pattern_type="agent", stages=[{"nlu": None}])


def test_fsm_slots_and_stages_legal():
    p = Pattern(code="p", name="n", description="d", pattern_type="fsm",
                nodes=[BaseNode(code="a", slots={"s": "v"},
                                sub_nodes=["b"]),
                       BaseNode(code="b", is_end=True)],
                stages=[{"nlu": None}])
    assert p.stages == [{"nlu": None}]
    assert p.node_map["a"].slots == {"s": "v"}


def test_nodes_accept_inline_dicts():
    p = Pattern(code="p", name="n", description="d",
                nodes=[{"code": "a", "name": "A", "sub_nodes": []}])
    assert p.node_map["a"].name == "A"


def test_config_single_source_and_precedence():
    p = Pattern(code="p", name="n", description="d",
                config={"max_steps": 3, "flavor": "x"})
    assert p.max_steps == 3
    assert p.config["flavor"] == "x"
    # explicit param wins over the same config key
    p2 = Pattern(code="p2", name="n", description="d",
                 max_steps=9, config={"max_steps": 3})
    assert p2.max_steps == 9
    # config key can also carry what the param left unset
    p3 = Pattern(code="p3", name="n", description="d",
                 config={"pattern_type": "fsm"})
    assert p3.pattern_type == "fsm"
    # free kwargs ride into config
    p4 = Pattern(code="p4", name="n", description="d", custom_key="v")
    assert p4.config["custom_key"] == "v"


def test_default_max_steps():
    p = Pattern(code="p", name="n", description="d")
    assert p.max_steps == DEFAULT_MAX_STEPS == 10


def test_self_loop_edge_legal():
    # 环是合法语义（AGENT 图环由 max_steps 预算防护；FSM 环是自然推进）
    p = Pattern(code="p", name="n", description="d",
                nodes=[BaseNode(code="a", sub_nodes=["a"])])
    assert p.node_map["a"].sub_nodes == ["a"]
