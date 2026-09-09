"""End-to-end integration test for the off-topic turn — FakeProvider + in-memory knowledge base.

The pattern is built inline; node codes/names follow the fake_provider script
conventions (route root / menu_sales / buy_ask_brand ...).
"""

import pytest

from fake_provider import fake_llm_config, register_fake_provider

from nexus.engine.chat import chat as chat_fn
from nexus.engine.session import Session
from nexus.model.module import FSMModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from atoms.stages.recaller import (
    KeywordRecallPath,
    MultiPathRecaller,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)
from atoms.stages.clarify import ClarifyRouteRule, ClarifyStage


@pytest.fixture(scope="module", autouse=True)
def _fake_provider():
    register_fake_provider()


KB_DOCS = [
    {"id": "fee_policy", "content": "除车价外仅收取上牌费与服务费，无其他收费",
     "metadata": {"keywords": ["收费", "服务费", "上牌费"]}},
]


@pytest.fixture()
def pattern():
    """Inline route + FSM pattern (ROUTE routing + car-buying FSM submodule)."""
    return Pattern(
        code="clarify_demo",
        name="澄清集成测试 pattern",
        description="路由 + 购车 FSM（内联测试 fixture）",
        entry_module_code="demo_root",
        modules=[
            RouteModule(
                module_code="demo_root",
                module_name="总路由",
                module_description="顶层路由",
                module_todo_description="意图分发",
                module_nodes=[
                    BaseNode(
                        node_code="route_root",
                        node_name="路由根节点",
                        node_description="总入口",
                        node_todo_description="意图分类",
                        sub_nodes=["menu_sales"],
                    ),
                    BaseNode(
                        node_code="menu_sales",
                        node_name="购车咨询",
                        node_description="购车入口",
                        node_todo_description="跳转到购车子模块",
                        sub_nodes=[],
                        jump_module="demo_buy",
                    ),
                ],
            ),
            FSMModule(
                module_code="demo_buy",
                module_name="购车流程",
                module_description="品牌 → 预算 → 城市 → 确认",
                module_todo_description="收集购车信息",
                module_nodes=[
                    BaseNode(
                        node_code="buy_ask_brand",
                        node_name="询问品牌",
                        node_description="收集品牌",
                        node_todo_description="抽取 brand 槽位",
                        node_slots={"brand": "品牌"},
                        sub_nodes=["buy_ask_budget"],
                    ),
                    BaseNode(
                        node_code="buy_ask_budget",
                        node_name="询问预算",
                        node_description="收集预算",
                        node_todo_description="抽取 budget 槽位",
                        node_slots={"budget": "预算"},
                        sub_nodes=["buy_ask_city"],
                    ),
                    BaseNode(
                        node_code="buy_ask_city",
                        node_name="询问城市",
                        node_description="收集城市",
                        node_todo_description="抽取 city 槽位",
                        node_slots={"city": "城市"},
                        sub_nodes=["buy_confirm"],
                    ),
                    BaseNode(
                        node_code="buy_confirm",
                        node_name="确认购车信息",
                        node_description="最终确认",
                        node_todo_description="结束流程",
                        sub_nodes=[],
                        is_end=True,
                    ),
                ],
            ),
        ],
    )


def test_off_topic_turn_routes_kb_and_keeps_node(pattern):
    """Off-topic turn: kb answer + bring back on topic; node unchanged, slots unpolluted; the next turn recovers."""
    session = Session(session_id="it", pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.metadata["llm_override"] = fake_llm_config()

    # Enable clarify on the car-buying FSM module (test injection, leaves the
    # fixture definition untouched): register a KB-backed ClarifyStage under
    # a unique code and declare that code in the module's stages
    from stage_stubs import register_stage_stub
    from nexus.registry.plugins import registry as plugin_registry
    kb_clarify_code = "clarify_kb_it"
    if not plugin_registry.has("stage", kb_clarify_code):
        plugin_registry.register("stage", kb_clarify_code, lambda: ClarifyStage(
            recaller=MultiPathRecaller(
                recall_paths=[KeywordRecallPath(name="kb", documents=KB_DOCS)],
                filters=[ScoreThresholdFilter(threshold=0.1)],
                fusion=WeightedScoreFusion(),
            ),
            rule=ClarifyRouteRule(),
        ))
    buy = pattern.module_map["demo_buy"]
    buy.stages = {"clarify": kb_clarify_code}

    sessions = {"it": session}

    # Round 1: route silently dispatches; the buy FSM first node buy_ask_brand
    # digests the sentence in the same turn, brand slot = the whole query,
    # node advances to buy_ask_budget
    r1 = chat_fn("我想买车", "it", sessions)
    assert session.cxt.current_module_code == "demo_buy"
    assert session.cxt.current_node_code == "buy_ask_budget"
    assert session.cxt.filled_slots.get("brand") == "我想买车"
    assert "询问品牌" in r1  # FSMNLG generates the reply with the pre-transition node

    # Round 2: off-topic (asks about fees when budget should be asked)
    r3 = chat_fn("还要收别的钱吗", "it", sessions)
    clarify_info = session.cxt.metadata["clarify"]
    assert clarify_info["triggered"] is True
    assert clarify_info["mode"] == "kb"
    assert "上牌费与服务费" in r3
    assert "预算" in r3
    assert session.cxt.current_node_code == "buy_ask_budget"
    assert "topic" not in session.cxt.filled_slots
    assert session.cxt.filled_slots.get("brand") == "我想买车"

    # Round 3: back to normal (answers the budget)
    r4 = chat_fn("20万左右", "it", sessions)
    assert session.cxt.metadata["clarify"]["triggered"] is False
    assert session.cxt.current_node_code == "buy_ask_city"
    assert session.cxt.filled_slots["budget"] == "20万左右"
