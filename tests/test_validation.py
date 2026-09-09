"""Validation tests — collected (numbered) error reporting, soft warnings,
and every check item of validate_base_info / validate_plugin_declarations."""

import pytest

import atoms.executors  # noqa: F401 -- warm executor codes
import atoms.stages  # noqa: F401 -- warm stage codes
import apps.xianyu_agent.route  # noqa: F401 -- warm app-local stage codes
from nexus.model.module import AgentModule, FSMModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.model.validation import (
    validate_base_info,
    validate_pattern,
    validate_plugin_declarations,
)


def _node(code, name="节点", **kw):
    return BaseNode(node_code=code, node_name=name, **kw)


def _pattern(modules, **kw):
    return Pattern(code=kw.pop("code", "p"), name=kw.pop("name", "t"),
                   description="t", modules=modules, **kw)


# ============================================================================
# Base info
# ============================================================================

def test_valid_pattern_no_errors():
    p = _pattern([
        RouteModule(module_code="root", module_name="路由",
                    module_nodes=[_node("r1")]),
        AgentModule(module_code="a", module_name="助手"),
    ], entry_module_code="root")
    assert validate_base_info(p) == []
    assert validate_plugin_declarations(p) == []


def test_missing_entry_module_reported():
    p = _pattern([AgentModule(module_code="a", module_name="A")],
                 entry_module_code="ghost")
    errors = validate_base_info(p)
    assert any("entry_module_code" in e and "不在" in e for e in errors)


def test_duplicate_module_code_reported():
    p = _pattern([
        AgentModule(module_code="a", module_name="A1"),
        AgentModule(module_code="a", module_name="A2"),
    ], entry_module_code="a")
    errors = validate_base_info(p)
    assert any("module_code 重复" in e for e in errors)


def test_fsm_without_nodes_reported():
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[])],
                 entry_module_code="f")
    errors = validate_base_info(p)
    assert any("没有任何节点" in e for e in errors)


def test_duplicate_node_code_within_module():
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[_node("n1"), _node("n1")])],
                 entry_module_code="f")
    errors = validate_base_info(p)
    assert any("node_code 重复" in e for e in errors)


def test_missing_name_is_soft_warning(caplog):
    p = _pattern([AgentModule(module_code="a")], entry_module_code="a")
    p.name = None
    with caplog.at_level("WARNING"):
        errors = validate_base_info(p)
    assert errors == []  # soft: no raise-able errors
    assert any("缺少 name" in r.message for r in caplog.records)


# ============================================================================
# Plugin declarations
# ============================================================================

def test_unknown_executor_code_reported():
    p = _pattern([AgentModule(module_code="a", module_name="A",
                              executor="ghost_loop")],
                 entry_module_code="a")
    errors = validate_plugin_declarations(p)
    assert any("executor" in e and "ghost_loop" in e for e in errors)


def test_unknown_pattern_executor_reported():
    p = _pattern([AgentModule(module_code="a", module_name="A")],
                 entry_module_code="a", executor_fsm="ghost_fsm")
    errors = validate_plugin_declarations(p)
    assert any("executor_fsm" in e for e in errors)


def test_unknown_stage_code_reported():
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[_node("n1")],
                            stages={"nlu": "ghost_stage"})],
                 entry_module_code="f")
    errors = validate_plugin_declarations(p)
    assert any("ghost_stage" in e for e in errors)


def test_slot_not_in_skeleton_reported():
    # skeleton without a clarify slot, module declares one
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[_node("n1")],
                            stages={"clarify": "clarify_default"})],
                 entry_module_code="f",
                 stages=[{"nlu": None}, {"nlg": None}])
    errors = validate_plugin_declarations(p)
    assert any("骨架不存在" in e and "clarify" in e for e in errors)


def test_builtin_stages_resolve_in_skeleton():
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[_node("n1")],
                            stages={"nlu": "fsm_unified",
                                    "nlg": "fsm_unified"})],
                 entry_module_code="f")
    assert validate_plugin_declarations(p) == []


def test_unified_pair_legal_duplicate():
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[_node("n1")],
                            stages={"nlu": "fsm_unified",
                                    "nlg": "fsm_unified"})],
                 entry_module_code="f",
                 stages=[{"nlu": None}, {"nlg": None}])
    # nlu/nlg sharing one code is the unified form — no duplicate complaint
    assert not any("多个槽位" in e for e in validate_plugin_declarations(p))


def test_illegal_duplicate_code_reported():
    p = _pattern([FSMModule(module_code="f", module_name="F",
                            module_nodes=[_node("n1", stages={
                                "query": "time_aug_query"})],
                            stages={"nlu": "time_aug_query",
                                    "nlg": "fsm_nlg"})],
                 entry_module_code="f",
                 stages=[{"query": None}, {"nlu": None}, {"nlg": None}])
    errors = validate_plugin_declarations(p)
    assert any("多个槽位" in e for e in errors)


def test_unknown_messages_builder_reported():
    p = _pattern([AgentModule(module_code="a", module_name="A",
                              messages_builder="ghost_builder")],
                 entry_module_code="a")
    errors = validate_plugin_declarations(p)
    assert any("messages_builder" in e for e in errors)


def test_unknown_agent_hooks_reported():
    p = _pattern([AgentModule(module_code="a", module_name="A",
                              agent_hooks="ghost_hooks")],
                 entry_module_code="a")
    errors = validate_plugin_declarations(p)
    assert any("agent_hooks" in e for e in errors)


# ============================================================================
# Collected reporting (validate_pattern)
# ============================================================================

def test_all_errors_collected_in_one_raise():
    p = _pattern([
        AgentModule(module_code="a", module_name="A",
                    executor="ghost_loop"),
        AgentModule(module_code="a", module_name="A2"),  # duplicate code
    ], entry_module_code="ghost", executor_fsm="ghost_fsm")
    with pytest.raises(ValueError) as ei:
        validate_pattern(p)
    msg = str(ei.value)
    assert "共 4 项" in msg
    assert "[1]" in msg and "[4]" in msg
    assert "ghost_loop" in msg and "ghost_fsm" in msg
    assert "重复" in msg and "entry_module_code" in msg


def test_registered_patterns_pass_validation():
    """The two production patterns validate cleanly against the warmed registry."""
    import apps.customer_agent.route  # noqa: F401
    from nexus.registry.patterns import discover_builtin_patterns, registry

    discover_builtin_patterns()
    for code in ("xianyu_agent", "customer_agent"):
        p = registry.get(code)
        assert p is not None, code
        validate_pattern(p)  # no raise
