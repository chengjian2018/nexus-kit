"""Pattern YAML round-trip tests (two-layer shape).

- dict/yaml serialize → load → re-serialize stable
- loaded structure equivalent to the python declaration (pattern_type /
  nodes / stages / plugins / allow_toolset / config)
- from_dict goes through the full construction path (compile fail-fast on
  dangling sub_nodes edges)
- non-mapping yaml raises
- empty stages normalize to the default skeleton (FSM only)
"""

from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.model.serialization import (
    pattern_from_dict,
    pattern_from_yaml,
    pattern_to_dict,
    pattern_to_yaml,
)
from nexus.pipeline import DEFAULT_SKELETON_SLOTS


def _fsm_pattern() -> Pattern:
    return Pattern(
        code="yaml_fsm",
        name="预约安装",
        description="外呼预约安装 FSM",
        pattern_type="fsm",
        nodes=[
            BaseNode(code="n_open", name="外呼开场", description="开场确认",
                     task_description="确认是否需要安装",
                     sub_nodes=["n_end"], slots={"addr": "安装地址"},
                     answer_examples=["您好，请问是..."]),
            BaseNode(code="n_end", name="通话结束语", is_end=True),
        ],
        stages=[{"nlu": None}, {"nlg": None}],
    )


def _agent_pattern() -> Pattern:
    return Pattern(
        code="yaml_agent",
        name="图应用",
        description="agent 图",
        pattern_type="agent",
        nodes=[
            BaseNode(code="root", name="根", sub_nodes=["leaf"],
                     use_tools=["t1"]),
            BaseNode(code="leaf", name="叶"),
        ],
        allow_toolset=["knowledge"],
        plugins={"loop": "default_loop"},
        max_steps=5,
    )


def test_yaml_roundtrip_stable():
    for pattern in (_fsm_pattern(), _agent_pattern()):
        text1 = pattern_to_yaml(pattern)
        loaded = pattern_from_yaml(text1)
        text2 = pattern_to_yaml(loaded)
        assert text1 == text2


def test_dict_roundtrip_structure():
    p = _agent_pattern()
    loaded = pattern_from_dict(pattern_to_dict(p))
    assert loaded.pattern_type == "agent"
    assert [n.code for n in loaded.nodes] == ["root", "leaf"]
    assert loaded.node_map["root"].sub_nodes == ["leaf"]
    assert loaded.node_map["root"].use_tools == ["t1"]
    assert loaded.allow_toolset == ["knowledge"]
    assert loaded.plugins == {"loop": "default_loop"}
    assert loaded.max_steps == 5
    assert loaded.entry_node_code == "root"


def test_fsm_node_fields_roundtrip():
    loaded = pattern_from_dict(pattern_to_dict(_fsm_pattern()))
    node = loaded.node_map["n_open"]
    assert node.slots == {"addr": "安装地址"}
    assert node.answer_examples == ["您好，请问是..."]
    assert node.task_description == "确认是否需要安装"
    assert loaded.node_map["n_end"].is_end is True
    assert [slot for entry in loaded.stages for slot in entry] == ["nlu", "nlg"]


def test_node_config_rides_along():
    p = Pattern(code="cfg_p", name="n", description="d",
                nodes=[BaseNode(code="only", base_prompt="你是客服")])
    loaded = pattern_from_dict(pattern_to_dict(p))
    assert loaded.node_map["only"].config["base_prompt"] == "你是客服"


def test_from_dict_keeps_compile_failfast():
    data = pattern_to_dict(_agent_pattern())
    data["nodes"][0]["sub_nodes"] = ["ghost"]
    try:
        pattern_from_dict(data)
        raise AssertionError("悬空边应当在构造期 raise")
    except ValueError as e:
        assert "悬空边" in str(e)


def test_non_mapping_yaml_raises():
    try:
        pattern_from_yaml("- a\n- b\n")
        raise AssertionError("非映射 yaml 应当 raise")
    except ValueError:
        pass


def test_empty_stages_default_skeleton():
    p = pattern_from_dict({"code": "sk", "name": "sk", "description": "d",
                           "pattern_type": "fsm",
                           "nodes": [{"code": "a"}]})
    assert [slot for entry in p.stages for slot in entry] == \
        DEFAULT_SKELETON_SLOTS


def test_agent_pattern_serializes_without_stages():
    data = pattern_to_dict(_agent_pattern())
    assert "stages" not in data  # AGENT 无骨架，不出现在序列化形态
