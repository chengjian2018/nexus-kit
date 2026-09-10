"""pattern → module(s) 转换（nexus/model/convert.py）测试：

- 单模块 pattern：类型保留（AGENT/FSM/ROUTE）、头字段取 pattern 身份、
  节点树深拷贝、plugins 折入（pattern 层填空槽 / 模块层优先）
- 多模块 pattern：**整组随行**——兄弟模块保留、邻接边 / 节点跳转组内
  自洽、pattern 层 plugins 分发给全部模块、头模块居首
- 头模块改名：组内指向旧 code 的边与跳转改写，图保持一致
- executor 直配字段（类型无关最高优先级）随行
- 产物纯声明式：module_to_dict 再序列化稳定；整组可直接嵌入宿主 pattern
- 入口模块不可解析 → ValueError
"""

import pytest

from nexus.model.convert import pattern_to_module, pattern_to_modules
from nexus.model.module import (
    AgentModule,
    FSMModule,
    ModuleType,
    RouteModule,
)
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.model.serialization import (
    module_to_dict,
    pattern_from_dict,
    pattern_to_dict,
)


def _single_agent_pattern(**pattern_kwargs):
    module = AgentModule(
        module_code="research",
        module_name="深度研究",
        module_description="结构化深度研究",
        module_todo_description="产出研究报告",
        base_prompt="研究提示词",
        use_tools=["search"],
        answer_examples=["报告范式"],
        **pattern_kwargs.pop("module_kwargs", {}),
    )
    return Pattern(
        code="deep_research",
        name="深度研究助手",
        description="Deep research 配方",
        entry_module_code="research",
        modules=[module],
        **pattern_kwargs,
    )


def _multi_module_pattern(**pattern_kwargs):
    """root（ROUTE 头模块，菜单节点跳 flow + 邻接边到 flow）+
    flow（FSM，节点跳回头模块 root + 邻接边回 root）——组内双向引用，
    用于验证拓扑随行与头模块改名改写。"""
    return Pattern(
        code="multi", name="多模块", description="d",
        entry_module_code="root",
        modules=[
            RouteModule(
                module_code="root", module_name="路由",
                module_nodes=[BaseNode(node_code="r_menu", node_name="菜单",
                                       jump_module="flow")],
                use_tools=["search"],  # flow 的 lend_tools=["search"] 需授权
                sub_modules=[{"target": "flow", "lend_knowledge": True,
                              "lend_tools": []}],
            ),
            FSMModule(
                module_code="flow", module_name="流程",
                module_nodes=[BaseNode(node_code="f1", node_name="第一步",
                                       jump_module="root")],
                sub_modules=[{"target": "root", "lend_knowledge": False,
                              "lend_tools": ["search"]}],
            ),
        ],
        **pattern_kwargs,
    )


# ---------------------------------------------------------------------------
# 单模块 pattern：结构与身份
# ---------------------------------------------------------------------------

def test_single_module_type_preserved_and_header_from_pattern():
    pattern = _single_agent_pattern()
    module = pattern_to_module(pattern)
    assert isinstance(module, AgentModule)
    assert module.type == ModuleType.AGENT
    # 头字段取 pattern 身份（module 代表整个 pattern）
    assert module.module_code == "deep_research"
    assert module.module_name == "深度研究助手"
    assert module.module_description == "Deep research 配方"
    # 入口模块的流字段随行
    assert module.module_todo_description == "产出研究报告"
    assert module.base_prompt == "研究提示词"
    assert module.use_tools == ["search"]
    assert module.answer_examples == ["报告范式"]


def test_explicit_header_overrides():
    module = pattern_to_module(_single_agent_pattern(),
                               module_code="alias",
                               module_name="别名",
                               module_description="覆盖描述")
    assert module.module_code == "alias"
    assert module.module_name == "别名"
    assert module.module_description == "覆盖描述"


def test_fsm_pattern_preserves_nodes_and_stages():
    pattern = Pattern(
        code="flow_p", name="流程", description="d",
        entry_module_code="flow",
        modules=[FSMModule(
            module_code="flow", module_name="流程",
            module_nodes=[
                BaseNode(node_code="f1", node_name="第一步",
                         sub_nodes=["f2"], node_slots={"a": "槽A"}),
                BaseNode(node_code="f2", node_name="第二步", is_end=True),
            ],
            stages={"clarify": "clarify_default"},
        )],
    )
    module = pattern_to_module(pattern)
    assert isinstance(module, FSMModule)
    assert [n.node_code for n in module.module_nodes] == ["f1", "f2"]
    assert module.module_nodes[0].sub_nodes == ["f2"]
    assert module.module_nodes[0].node_slots == {"a": "槽A"}
    assert module.module_nodes[1].is_end is True
    assert module.stages == {"clarify": "clarify_default"}


def test_result_is_deep_copy():
    pattern = _single_agent_pattern()
    module = pattern_to_module(pattern)
    src = pattern.module_map["research"]
    assert module is not src
    # 声明互不影响
    module.use_tools.append("extra")
    module.stages["x"] = "y"
    assert src.use_tools == ["search"]
    assert src.stages == {}


# ---------------------------------------------------------------------------
# plugins 折入
# ---------------------------------------------------------------------------

def test_pattern_plugins_fill_module_slots():
    pattern = _single_agent_pattern(
        plugins={"messages_builder": "default", "agent_hooks": "my_hooks"})
    module = pattern_to_module(pattern)
    assert module.plugins == {"messages_builder": "default",
                              "agent_hooks": "my_hooks"}
    assert module.messages_builder == "default"
    assert module.agent_hooks == "my_hooks"


def test_module_own_plugins_beat_pattern():
    pattern = _single_agent_pattern(
        plugins={"messages_builder": "pattern_mb", "agent_hooks": "pah"},
        module_kwargs={"messages_builder": "module_mb"})
    module = pattern_to_module(pattern)
    # 模块层压 pattern 层（与运行时解析链同序）
    assert module.plugins == {"messages_builder": "module_mb",
                              "agent_hooks": "pah"}


def test_executor_direct_field_rides_along():
    pattern = _single_agent_pattern(
        module_kwargs={"executor": "deep_research"})
    module = pattern_to_module(pattern)
    assert module.executor == "deep_research"


def test_pattern_level_executor_family_folds():
    pattern = _single_agent_pattern(plugins={"loop": "custom_loop"})
    module = pattern_to_module(pattern)
    assert module.plugins["loop"] == "custom_loop"


# ---------------------------------------------------------------------------
# 多模块 pattern：整组随行 + 属性分发
# ---------------------------------------------------------------------------

def test_multi_module_full_set_rides_along():
    pattern = _multi_module_pattern()
    result = pattern_to_modules(pattern)
    # 全部模块保留；头模块（源入口）居首并承接 pattern 身份
    assert len(result) == 2
    head, flow = result
    assert isinstance(head, RouteModule)
    assert head.module_code == "multi"
    assert head.module_name == "多模块"
    assert isinstance(flow, FSMModule)
    assert flow.module_code == "flow"
    # 组内拓扑随行：邻接边 / 节点跳转原样保留
    assert head.sub_modules == [{"target": "flow", "lend_knowledge": True,
                                 "lend_tools": []}]
    assert flow.sub_modules == [{"target": "multi", "lend_knowledge": False,
                                 "lend_tools": ["search"]}]
    assert head.module_nodes[0].jump_module == "flow"
    assert flow.module_nodes[0].jump_module == "multi"


def test_pattern_plugins_distributed_to_all_modules():
    pattern = _multi_module_pattern(
        plugins={"messages_builder": "default", "loop": "custom_loop"})
    result = pattern_to_modules(pattern)
    for module in result:
        assert module.plugins.get("messages_builder") == "default"
        assert module.plugins.get("loop") == "custom_loop"
    # 模块自己的声明压过分发值
    pattern = _multi_module_pattern(plugins={"agent_hooks": "pah"})
    head_src = pattern.module_map["root"]
    head_src.plugins["agent_hooks"] = "own_ah"  # 模块层已有声明
    result = pattern_to_modules(pattern)
    assert result[0].plugins["agent_hooks"] == "own_ah"
    assert result[1].plugins["agent_hooks"] == "pah"


def test_head_rename_rewrites_internal_references():
    """头模块改名（显式覆盖）→ 组内指向旧 code 的边与跳转一并改写。"""
    result = pattern_to_modules(_multi_module_pattern(),
                                module_code="gateway")
    head, flow = result
    assert head.module_code == "gateway"
    # flow 的边与节点跳转从 root 改写到 gateway
    assert flow.sub_modules[0]["target"] == "gateway"
    assert flow.module_nodes[0].jump_module == "gateway"
    # head 指向 flow 的引用不受影响
    assert head.sub_modules[0]["target"] == "flow"
    assert head.module_nodes[0].jump_module == "flow"


def test_converted_set_embeddable_in_host_pattern():
    """整组产物可直接嵌入宿主 pattern（构造期图校验通过）。"""
    result = pattern_to_modules(_multi_module_pattern())
    host = Pattern(
        code="host", name="宿主", description="d",
        entry_module_code=result[0].module_code, modules=result,
    )
    assert set(host.module_map) == {"multi", "flow"}


def test_pattern_to_module_returns_head_only():
    """单模块便捷入口 = 头模块；多模块场景应使用 pattern_to_modules。"""
    result = pattern_to_modules(_multi_module_pattern())
    head = pattern_to_module(_multi_module_pattern())
    assert head.module_code == result[0].module_code


# ---------------------------------------------------------------------------
# 声明式与异常路径
# ---------------------------------------------------------------------------

def test_result_serializes():
    module = pattern_to_module(_single_agent_pattern(
        plugins={"messages_builder": "default"}))
    d = module_to_dict(module)
    assert d["module_code"] == "deep_research"
    assert d["plugins"] == {"messages_builder": "default"}
    # 深度 round-trip：整个宿主 pattern 序列化稳定
    host = Pattern(code="host", name="宿主", description="d",
                   entry_module_code=module.module_code, modules=[module])
    loaded = pattern_from_dict(pattern_to_dict(host))
    assert loaded.module_map["deep_research"].base_prompt == "研究提示词"


def test_multi_module_result_serializes():
    result = pattern_to_modules(_multi_module_pattern(
        plugins={"messages_builder": "default"}))
    host = Pattern(code="host", name="宿主", description="d",
                   entry_module_code=result[0].module_code, modules=result)
    loaded = pattern_from_dict(pattern_to_dict(host))
    assert loaded.node_map["r_menu"].jump_module == "flow"
    assert loaded.module_map["flow"].sub_modules[0]["target"] == "multi"
    assert loaded.module_map["flow"].messages_builder == "default"


def test_unresolvable_entry_raises():
    pattern = Pattern(code="bad", name="b", description="d",
                      entry_module_code="ghost",
                      modules=[AgentModule(module_code="m")])
    with pytest.raises(ValueError, match="入口模块不可解析"):
        pattern_to_module(pattern)
    with pytest.raises(ValueError, match="入口模块不可解析"):
        pattern_to_modules(pattern)
