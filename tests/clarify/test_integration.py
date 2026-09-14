"""End-to-end integration test for the off-topic turn — FakeProvider + in-memory knowledge base.

Current form: the pattern is a single FSM Pattern; node codes/names follow the
fake_provider script conventions (unified stage: u_ask_brand / u_ask_budget /
u_confirm). The clarify declaration lives on the node (u_ask_budget) with the
pattern skeleton carrying the clarify slot.
"""

import pytest

from fake_provider import fake_llm_config, register_fake_provider

from async_utils import arun
from nexus.engine.chat import chat as chat_fn
from nexus.engine.session import Session
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
    """Inline car-buying FSM pattern (brand → budget → confirm) with the
    unified stage + a KB-backed clarify slot declared on the budget node."""
    kb_clarify_code = "clarify_kb_it"
    from nexus.registry.plugins import registry as plugin_registry
    if not plugin_registry.has("stage", kb_clarify_code):
        plugin_registry.register("stage", kb_clarify_code, lambda: ClarifyStage(
            recaller=MultiPathRecaller(
                recall_paths=[KeywordRecallPath(name="kb", documents=KB_DOCS)],
                filters=[ScoreThresholdFilter(threshold=0.1)],
                fusion=WeightedScoreFusion(),
            ),
            rule=ClarifyRouteRule(),
        ))

    return Pattern(
        code="clarify_demo",
        name="澄清集成测试 pattern",
        description="购车 FSM（内联测试 fixture）",
        pattern_type="fsm",
        stages=[
            {"nlu": "fsm_unified"},
            {"clarify": None},
            {"nlg": "nlg_pass_through"},
        ],
        nodes=[
            BaseNode(
                code="u_ask_brand",
                name="询问品牌",
                description="收集品牌",
                task_description="抽取 brand 槽位",
                slots={"brand": "品牌"},
                sub_nodes=["u_ask_budget"],
            ),
            # the clarify declaration lives on the node where the off-topic
            # turn can happen (the node-level clarify declaration)
            BaseNode(
                code="u_ask_budget",
                name="询问预算",
                description="收集预算",
                task_description="抽取 budget 槽位",
                slots={"budget": "预算"},
                sub_nodes=["u_confirm"],
                stages={"clarify": kb_clarify_code},
            ),
            BaseNode(
                code="u_confirm",
                name="确认购车信息",
                description="最终确认",
                task_description="结束流程",
                is_end=True,
            ),
        ],
    )


def test_off_topic_turn_routes_kb_and_keeps_node(pattern):
    """Off-topic turn: kb answer + bring back on topic; node unchanged, slots unpolluted; the next turn recovers."""
    session = Session(session_id="it", pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.metadata["llm_override"] = fake_llm_config()

    sessions = {"it": session}

    # Round 1: the FSM first node u_ask_brand digests the sentence, brand
    # slot = the whole query, node advances to u_ask_budget
    r1 = arun(chat_fn("我想买车", "it", sessions))
    assert session.cxt.current_node_code == "u_ask_budget"
    assert session.cxt.filled_slots.get("brand") == "我想买车"
    assert "询问预算" in r1  # the unified reply follows the next node's style

    # Round 2: off-topic (asks about fees when budget should be asked)
    r3 = arun(chat_fn("还要收别的钱吗", "it", sessions))
    clarify_info = session.cxt.metadata["clarify"]
    assert clarify_info["triggered"] is True
    assert clarify_info["mode"] == "kb"
    assert "上牌费与服务费" in r3
    assert "预算" in r3
    assert session.cxt.current_node_code == "u_ask_budget"
    assert "topic" not in session.cxt.filled_slots
    assert session.cxt.filled_slots.get("brand") == "我想买车"

    # Round 3: back to normal (answers the budget)
    r4 = arun(chat_fn("20万左右", "it", sessions))
    assert session.cxt.metadata["clarify"]["triggered"] is False
    assert session.cxt.current_node_code == "u_confirm"
    assert session.cxt.filled_slots["budget"] == "20万左右"
