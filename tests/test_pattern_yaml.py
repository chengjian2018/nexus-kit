"""Pattern dict/yaml round-trip tests — serialize → load → serialize is
stable, and a loaded pattern behaves identically to its python-declared
twin (module_map/node_map/stages/declarations all equal)."""

import pytest

import atoms.executors  # noqa: F401 -- warm executor codes
import atoms.stages  # noqa: F401 -- warm stage codes
from nexus.model.pattern import Pattern
from nexus.model.serialization import (
    pattern_from_dict,
    pattern_from_yaml,
    pattern_to_dict,
    pattern_to_yaml,
)


def _sample_pattern():
    from nexus.model.module import AgentModule, FSMModule, RouteModule
    from nexus.model.node import BaseNode

    return Pattern(
        code="demo",
        name="演示",
        description="round-trip 样例",
        entry_module_code="root",
        stages=[
            {"pre_recall": None},
            {"query": "time_aug_query"},
            {"post_recall": None},
            {"nlu": None},
            {"clarify": None},
            {"nlg": None},
        ],
        modules=[
            RouteModule(
                module_code="root", module_name="路由",
                module_description="顶层路由", module_todo_description="分发",
                module_nodes=[
                    BaseNode(node_code="r_root", node_name="根",
                             sub_nodes=["r_menu"]),
                    BaseNode(node_code="r_menu", node_name="菜单",
                             jump_module="flow"),
                ],
                stages={"nlu": "route_unified", "nlg": "nlg_pass_through"},
                sub_modules=[{"target": "flow", "lend_knowledge": True,
                              "lend_tools": []}],
            ),
            FSMModule(
                module_code="flow", module_name="流程",
                module_description="流程模块",
                module_nodes=[
                    BaseNode(node_code="f1", node_name="第一步",
                             sub_nodes=["f2"], node_slots={"a": "槽A"}),
                    BaseNode(node_code="f2", node_name="第二步",
                             is_end=True),
                ],
                stages={"clarify": "clarify_default"},
            ),
            AgentModule(
                module_code="helper", module_name="助手",
                messages_builder="customer_agent_messages_builder",
                use_tools=["search_product_knowledge"],
            ),
        ],
        executor_loop="default_loop",
        max_hops=3,
    )


def test_round_trip_dict_stable():
    pattern = _sample_pattern()
    d1 = pattern_to_dict(pattern)
    loaded = pattern_from_dict(d1)
    d2 = pattern_to_dict(loaded)
    assert d1 == d2


def test_round_trip_yaml_stable():
    pattern = _sample_pattern()
    y1 = pattern_to_yaml(pattern)
    loaded = pattern_from_yaml(y1)
    y2 = pattern_to_yaml(loaded)
    assert y1 == y2


def test_loaded_pattern_structure_equivalent():
    pattern = _sample_pattern()
    loaded = pattern_from_dict(pattern_to_dict(pattern))

    assert loaded.code == pattern.code
    assert loaded.entry_module_code == pattern.entry_module_code
    assert loaded.stages == pattern.stages
    assert loaded.max_hops == pattern.max_hops
    assert loaded.executor_loop == pattern.executor_loop
    assert set(loaded.module_map) == set(pattern.module_map)
    assert set(loaded.node_map) == set(pattern.node_map)
    # module types survive
    from nexus.model.module import ModuleType
    assert loaded.module_map["root"].type == ModuleType.ROUTE
    assert loaded.module_map["flow"].type == ModuleType.FSM
    assert loaded.module_map["helper"].type == ModuleType.AGENT
    # stages declarations survive
    assert loaded.module_map["root"].stages == {
        "nlu": "route_unified", "nlg": "nlg_pass_through"}
    # jump_module survives (rides node kwargs)
    assert loaded.node_map["r_menu"].jump_module == "flow"
    # dict links survive
    assert loaded.module_map["root"].sub_modules == [
        {"target": "flow", "lend_knowledge": True, "lend_tools": []}]


def test_from_dict_graph_checks_still_run():
    """from_dict goes through the constructor: dangling edges still fail fast."""
    from nexus.model.module import AgentModule

    data = {
        "code": "bad", "name": "t", "description": "t",
        "entry_module_code": "a",
        "modules": [
            {"type": "agent", "module_code": "a",
             "sub_modules": [{"target": "missing"}]},
        ],
        "stages": [],
    }
    with pytest.raises(ValueError, match="悬空转移边"):
        pattern_from_dict(data)


def test_from_yaml_non_mapping_raises():
    with pytest.raises(ValueError, match="pattern 映射"):
        pattern_from_yaml("- just\n- a\n- list\n")


def test_none_skeleton_round_trips_to_default():
    from nexus.model.module import AgentModule

    data = {
        "code": "s", "name": "t", "description": "t",
        "entry_module_code": "a",
        "modules": [{"type": "agent", "module_code": "a"}],
    }
    loaded = pattern_from_dict(data)
    # empty stages → constructor normalizes to the default skeleton
    from nexus.pipeline import default_skeleton
    assert loaded.stages == default_skeleton()
