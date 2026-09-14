"""Validation tests — collected (numbered) error reporting, soft warnings,
and the check items of validate_base_info / validate_plugin_declarations /
validate_tools / validate_pattern (two-layer form).

The hard structural checks (duplicate node codes / dangling sub_nodes edges /
entry resolvability / agent declaring stages or slots) run in the Pattern
constructor and raise there; the validators below carry the soft remainder
plus the plugin-declaration / toolset resolvability checks.
"""

import pytest

import atoms.executors  # noqa: F401 -- warm executor codes
import atoms.stages  # noqa: F401 -- warm stage codes
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.model.validation import (
    validate_base_info,
    validate_pattern,
    validate_plugin_declarations,
    validate_tools,
)
from nexus.registry.tools import registry as tool_registry


def _node(code, name="节点", **kw):
    return BaseNode(code=code, name=name, **kw)


def _pattern(nodes=None, **kw):
    return Pattern(code=kw.pop("code", "p"), name=kw.pop("name", "t"),
                   description="t", nodes=nodes if nodes is not None
                   else [_node("n1")], **kw)


def _fsm_pattern(nodes=None, **kw):
    kw.setdefault("pattern_type", "fsm")
    return _pattern(nodes=nodes, **kw)


# ============================================================================
# Constructor fail-fast (hard structural checks)
# ============================================================================

def test_duplicate_node_code_raises_at_construction():
    with pytest.raises(ValueError, match="重复"):
        _pattern([_node("n1"), _node("n1")])


def test_dangling_sub_nodes_edge_raises_at_construction():
    with pytest.raises(ValueError, match="悬空边"):
        _pattern([_node("n1", sub_nodes=["ghost"])])


def test_unresolvable_entry_raises_at_construction():
    with pytest.raises(ValueError, match="entry_node_code"):
        _pattern([_node("n1")], entry_node_code="ghost")


def test_illegal_pattern_type_raises():
    with pytest.raises(ValueError, match="pattern_type"):
        _pattern(pattern_type="route")


def test_agent_pattern_declaring_stages_raises():
    with pytest.raises(ValueError, match="stages"):
        _agent = BaseNode(code="n1")
        Pattern(code="p", name="t", description="t", pattern_type="agent",
                nodes=[_agent], stages=[{"nlu": None}])


def test_agent_node_declaring_slots_raises():
    with pytest.raises(ValueError, match="slots"):
        _pattern([BaseNode(code="n1", slots={"brand": "品牌"})])


def test_agent_node_declaring_stages_reported_by_validator():
    """The constructor raises only on pattern-level stages; a node-level
    stages declaration on an AGENT pattern is caught by validate_base_info."""
    n = _node("n1")
    n.stages = {"nlu": "fsm_unified"}
    errors = validate_base_info(_pattern([n]))
    assert any("AGENT" in e and "stages" in e for e in errors)


# ============================================================================
# Base info
# ============================================================================

def test_valid_pattern_no_errors():
    p = _pattern([_node("r1", name="路由"), _node("a", name="助手")],
                 entry_node_code="r1")
    assert validate_base_info(p) == []
    assert validate_plugin_declarations(p) == []


def test_missing_name_is_soft_warning(caplog):
    p = _pattern([_node("n1")])
    p.name = None
    with caplog.at_level("WARNING"):
        errors = validate_base_info(p)
    assert errors == []  # soft: no raise-able errors
    assert any("缺少 name" in r.message for r in caplog.records)


def test_unreachable_node_is_soft_warning(caplog):
    """A disconnected node (not reachable from entry) is an authoring smell,
    not a hard structural error."""
    p = _pattern([_node("n1"), _node("orphan", name="孤岛")],
                 entry_node_code="n1")
    with caplog.at_level("WARNING"):
        errors = validate_base_info(p)
    assert errors == []
    assert any("不可达" in r.message for r in caplog.records)


# ============================================================================
# Plugin declarations
# ============================================================================

def test_unknown_node_loop_code_reported():
    p = _pattern([_node("n1", plugins={"loop": "ghost_loop"})])
    errors = validate_plugin_declarations(p)
    assert any("executor_loop" in e and "ghost_loop" in e for e in errors)


def test_unknown_pattern_executor_reported():
    p = _pattern(plugins={"loop": "ghost_loop"})
    errors = validate_plugin_declarations(p)
    assert any("executor_loop" in e for e in errors)

    p2 = _fsm_pattern(plugins={"fsm": "ghost_fsm"})
    errors2 = validate_plugin_declarations(p2)
    assert any("executor_fsm" in e for e in errors2)


def test_unknown_stage_code_reported():
    n = _node("n1", stages={"nlu": "ghost_stage"})
    p = _fsm_pattern([n])
    errors = validate_plugin_declarations(p)
    assert any("ghost_stage" in e for e in errors)


def test_slot_not_in_skeleton_reported():
    # skeleton without a clarify slot, node declares one
    n = _node("n1", stages={"clarify": "clarify_default"})
    p = _fsm_pattern([n], stages=[{"nlu": None}, {"nlg": None}])
    errors = validate_plugin_declarations(p)
    assert any("骨架不存在" in e and "clarify" in e for e in errors)


def test_builtin_stages_resolve_in_skeleton():
    p = _fsm_pattern(stages=[{"nlu": "fsm_unified"}, {"nlg": "fsm_unified"}])
    assert validate_plugin_declarations(p) == []


def test_unified_pair_legal_duplicate():
    p = _fsm_pattern(stages=[{"nlu": "fsm_unified"}, {"nlg": "fsm_unified"}])
    # nlu/nlg sharing one code is the unified form — no duplicate complaint
    assert not any("多个槽位" in e for e in validate_plugin_declarations(p))


def test_illegal_duplicate_code_reported():
    # query and nlu sharing one code across layers: not the unified pair
    n = _node("n1", stages={"query": "time_aug_query"})
    p = _fsm_pattern([n], stages=[{"query": None}, {"nlu": "time_aug_query"},
                                  {"nlg": "fsm_nlg"}])
    errors = validate_plugin_declarations(p)
    assert any("多个槽位" in e for e in errors)


def test_unknown_messages_builder_reported():
    p = _pattern(plugins={"messages_builder": "ghost_builder"})
    errors = validate_plugin_declarations(p)
    assert any("messages_builder" in e for e in errors)


def test_unknown_agent_hooks_reported():
    p = _pattern(plugins={"agent_hooks": "ghost_hooks"})
    errors = validate_plugin_declarations(p)
    assert any("agent_hooks" in e for e in errors)


# ============================================================================
# Tools authorization (deny-by-default; see also test_tools_permission.py)
# ============================================================================

def _register_tool(name, toolset):
    tool_registry.register(
        name=name, toolset=toolset,
        schema={"name": name, "description": f"test tool {name}",
                "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, _n=name: f"{_n} ok",
    )


# Unique toolset names: the tool registry is process-global — sharing the
# builtin "knowledge"/"mcp" toolsets would pollute other suites' set
# assertions (tests/test_tools_permission.py).
_TS_KB, _TS_MCP = "val-kb", "val-mcp"


def test_use_tools_dangling_name_reported():
    _register_tool("val_kb_search", _TS_KB)
    p = _pattern([_node("n1", use_tools=["ghost_tool"])],
                 allow_toolset=[_TS_KB])
    errors = validate_tools(p)
    assert any("未注册" in e for e in errors)


def test_use_tools_cross_toolset_reported():
    _register_tool("val_mcp_list", _TS_MCP)
    p = _pattern([_node("n1", use_tools=["val_mcp_list"])],
                 allow_toolset=[_TS_KB])
    errors = validate_tools(p)
    assert any("越集" in e for e in errors)


def test_legal_tool_declaration_passes():
    _register_tool("val_kb_search", _TS_KB)
    p = _pattern([_node("n1", use_tools=["val_kb_search"])],
                 allow_toolset=[_TS_KB])
    assert validate_tools(p) == []


# ============================================================================
# Collected reporting (validate_pattern)
# ============================================================================

def test_all_errors_collected_in_one_raise():
    p = _pattern(
        [_node("n1", plugins={"loop": "ghost_loop"}),
         _node("n2", use_tools=["ghost_tool"])],
        plugins={"messages_builder": "ghost_builder"},
        allow_toolset=["knowledge"])
    with pytest.raises(ValueError) as ei:
        validate_pattern(p)
    msg = str(ei.value)
    assert "共 3 项" in msg
    assert "[1]" in msg and "[3]" in msg
    assert "ghost_loop" in msg and "ghost_builder" in msg
    assert "ghost_tool" in msg


def test_minimal_pattern_validates_cleanly():
    """A minimal AGENT pattern (auto default node shape) validates: no tools,
    no plugin declarations, nothing dangling."""
    p = Pattern(code="mini", name="m", description="d")
    validate_pattern(p)  # no raise
