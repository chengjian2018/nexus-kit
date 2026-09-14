"""Unified stage (single call + structured output) offline tests — FSM-only
form (the ROUTE family is gone; the pattern is one FSM Pattern whose
skeleton wires the unified stage + pass-through NLG; the clarify declaration
lives on the node).

Uses the scripted FakeProvider to simulate LLM output (no real API access), covering:
1. Pattern structure + stage wiring (skeleton / node.stages / registry codes)
2. End-to-end flow: exactly 1 LLM call per turn, correct jumps and slots
3. Prompt assembly: candidate nodes carry answer styles, next_node allowed-values list
4. Code-level hard guard against invalid next_node (keeps the current node, reply preserved)
5. Parse-failure retry success / exhausted-retry fallback without crashing
6. PassThroughNLG keeps the already-generated reply
7. Opening broadcast (zero LLM, pure concatenation)
8. Dual-track clarify combination: off-topic turn emits a clarify signal → kb
   answer + bring back on topic (2 calls on the clarify turn, still 1 on a
   normal turn); a clarify signal from a node without the clarify declaration
   is rejected by the allowed-values hard guard
"""

import logging

import pytest

from async_utils import arun
from fake_provider import (
    FakeProvider,
    fake_llm_config,
    register_fake_provider,
)

logging.basicConfig(level=logging.WARNING)


# ============================================================================
# Fixtures & helpers
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    """Register the scripted provider, reused throughout the tests."""
    register_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """Return the inline-built unified stage FSM pattern (node codes/names stay
    consistent with the fake_provider script conventions)."""
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    return Pattern(
        code="unified_demo",
        name="统一阶段测试 pattern",
        description="FSM 统一阶段（内联测试 fixture）",
        pattern_type="fsm",
        stages=[{"nlu": "fsm_unified"}, {"clarify": None},
                {"nlg": "nlg_pass_through"}],
        nodes=[
            BaseNode(
                code="u_ask_brand",
                name="询问品牌",
                description="收集用户心仪的汽车品牌",
                task_description="理解用户提到的汽车品牌并抽取 brand 槽位",
                sub_nodes=["u_ask_budget"],
                slots={"brand": "汽车品牌，如比亚迪、特斯拉"},
                answer_examples=["好的，您对{brand}感兴趣呀！方便说下预算吗？"],
            ),
            BaseNode(
                code="u_ask_budget",
                name="询问预算",
                description="收集用户的购车预算区间",
                task_description="理解用户提到的预算并抽取 budget 槽位",
                sub_nodes=["u_confirm"],
                slots={"budget": "预算区间，如20万左右"},
                answer_examples=["预算{budget}很清晰！下面帮您确认一下信息。"],
            ),
            BaseNode(
                code="u_confirm",
                name="确认购车信息",
                description="向用户确认已收集的品牌与预算信息",
                task_description="确认信息无误；流程到此结束",
                is_end=True,
                answer_examples=["为您确认：品牌{brand}，预算{budget}。"],
            ),
        ],
    )


@pytest.fixture()
def sessions():
    """A session container isolated per test."""
    return {}


def launch(pattern, sessions, session_id="s1"):
    """Simulate main.py's launch flow: register the session and inject the pipeline context."""
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {}
    session.cxt.metadata["llm_override"] = fake_llm_config()
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    """Call nexus.engine.chat to process one dialogue turn."""
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id, all_sessions=sessions))


def chat_once(pattern, sessions, query, expect_calls=1):
    """Process one dialogue turn and assert exactly expect_calls LLM calls are consumed.

    A normal turn is 1 call (the core benefit of the unified stage).
    """
    before = FakeProvider.call_count
    reply = chat(sessions, "s1", query)
    assert FakeProvider.call_count - before == expect_calls, (
        f"本轮应恰好 {expect_calls} 次 LLM 调用，"
        f"实际 {FakeProvider.call_count - before} 次"
    )
    return reply


# ============================================================================
# Structure and wiring tests
# ============================================================================

def test_pattern_structure_and_stage_wiring(pattern):
    """The FSM pattern wires the unified stage via the skeleton; codes resolve
    through the plugin registry."""
    from atoms.stages.unified import FSMUnifiedNLU, PassThroughNLG

    assert pattern.code == "unified_demo"
    assert pattern.pattern_type == "fsm"
    assert pattern.entry_node_code == "u_ask_brand"
    # skeleton shape: unified nlu → clarify slot (opt-in, None) → pass-through nlg
    assert [list(e.keys())[0] for e in pattern.stages] == [
        "nlu", "clarify", "nlg"]
    assert [list(e.values())[0] for e in pattern.stages] == [
        "fsm_unified", None, "nlg_pass_through"]

    from nexus.registry.plugins import registry as plugin_registry
    assert isinstance(plugin_registry.resolve("stage", "fsm_unified"),
                      FSMUnifiedNLU)
    assert isinstance(plugin_registry.resolve("stage", "nlg_pass_through"),
                      PassThroughNLG)

    # FSM node chain and end node
    assert pattern.node_map["u_ask_brand"].sub_nodes == ["u_ask_budget"]
    assert pattern.node_map["u_ask_budget"].sub_nodes == ["u_confirm"]
    assert pattern.node_map["u_confirm"].is_end is True


def test_prompt_embeds_candidates_and_valid_values(pattern):
    """The unified stage prompt carries candidate-node answer styles and the next_node allowed values."""
    from nexus.context import DialogueContext
    from atoms.stages.unified import FSMUnifiedNLU

    ctx = DialogueContext(session_id="s-prompt", user_query="比亚迪")
    ctx.node_map = pattern.node_map
    ctx.current_node_code = "u_ask_brand"

    prompt = FSMUnifiedNLU().prompt_build(ctx)

    # Full candidate-node info: code + name + slot definition + answer style
    assert "u_ask_budget" in prompt
    assert "询问预算" in prompt
    assert "预算区间" in prompt
    assert "回答范式" in prompt
    # Current node's answer style (used when keeping the current node)
    assert "汽车品牌" in prompt
    # next_node allowed-values list (JSON array containing the empty string and candidate codes)
    assert '"u_ask_budget"' in prompt
    assert '""' in prompt


# ============================================================================
# End-to-end flow tests (single call per turn)
# ============================================================================

def test_full_flow_single_call_per_turn(pattern, sessions):
    """Full car-buying flow: brand → budget → confirm, exactly 1 call per turn."""
    session = launch(pattern, sessions)

    # Turn 1: the FSM first node u_ask_brand digests the sentence in one call
    # (brand slot = the whole query, jump to u_ask_budget)
    reply = chat_once(pattern, sessions, "我想买车，看看有什么车型")
    assert "询问预算" in reply, f"回复应来自统一阶段直出，实际: {reply!r}"
    assert session.cxt.current_node_code == "u_ask_budget"
    assert session.cxt.filled_slots["brand"] == "我想买车，看看有什么车型"

    # Turn 2: u_ask_budget digests it, producing the budget slot + confirmation reply + jump in one call
    reply = chat_once(pattern, sessions, "比亚迪")
    assert "确认购车信息" in reply
    assert session.cxt.current_node_code == "u_confirm"
    assert session.cxt.filled_slots["budget"] == "比亚迪"

    # Turn 3: end node; empty next_node keeps it in place (budget keeps the value extracted in turn 2)
    reply = chat_once(pattern, sessions, "预算20万左右")
    assert "确认购车信息" in reply
    assert session.cxt.current_node_code == "u_confirm"
    assert session.cxt.filled_slots["budget"] == "比亚迪"

    # Slots carry through the whole flow; unified-stage observation metadata written
    assert session.cxt.filled_slots == {
        "brand": "我想买车，看看有什么车型",
        "budget": "比亚迪",
    }
    assert session.cxt.metadata["unified"]["triggered"] is True
    assert "reply" in session.cxt.metadata["unified"]


# ============================================================================
# Hard guard and fallback tests
# ============================================================================

def test_invalid_next_node_guarded(pattern, sessions):
    """The model outputs an invalid next_node: the code-level guard keeps the current node, reply preserved."""
    session = launch(pattern, sessions)

    reply = chat_once(pattern, sessions, "跳到不存在节点")

    assert session.cxt.current_node_code == "u_ask_brand"
    assert session.cxt.nlu_result["next_node"] == ""
    # Reply preserved (still returned to the user); observation metadata records the invalid value
    assert "非法节点" in reply
    assert session.cxt.metadata["unified"]["invalid_next_node"] == "not_exist_node"


def test_parse_failure_retry_recovers(pattern, sessions):
    """First output is non-JSON → retry corrects it → normal jump (2 calls in total)."""
    session = launch(pattern, sessions)
    before = FakeProvider.call_count

    reply = chat(sessions, "s1", "解析失败重试 买车")

    # Failure + retry = 2 calls on the single unified stage
    assert FakeProvider.call_count - before == 2
    assert session.cxt.current_node_code == "u_ask_budget"
    assert "询问预算" in reply


def test_parse_failure_exhausted_falls_back(pattern, sessions):
    """Parsing still fails after retry → fallback reply + keep the current node, no exception raised."""
    from atoms.stages.unified import FSMUnifiedNLU

    session = launch(pattern, sessions)
    before = FakeProvider.call_count

    reply = chat(sessions, "s1", "永远解析失败")

    assert FakeProvider.call_count - before == 2
    assert reply == FSMUnifiedNLU.fallback_reply
    assert session.cxt.metadata["unified"]["parse_failed"] is True
    assert session.cxt.nlu_result == {"next_node": "", "slots": {}}
    assert session.cxt.current_node_code == "u_ask_brand"


def test_pass_through_nlg_keeps_existing_result():
    """PassThroughNLG: keeps an existing generated reply as-is; when missing, sets it empty, warns, and does not crash."""
    from nexus.context import DialogueContext
    from atoms.stages.unified import PassThroughNLG

    stage = PassThroughNLG()

    ctx = DialogueContext(session_id="s-1", user_query="q")
    ctx.nlg_result = {"content": "已生成的回复"}
    ctx = arun(stage.execute(ctx))
    assert ctx.nlg_result == {"content": "已生成的回复"}

    ctx_empty = DialogueContext(session_id="s-2", user_query="q")
    ctx_empty = arun(stage.execute(ctx_empty))
    assert ctx_empty.nlg_result == {"content": ""}


# ============================================================================
# Opening broadcast tests (zero LLM, pure concatenation)
# ============================================================================

def test_opening_broadcast_default_fallback():
    """No template passed: default copy concatenated line by line with the task_info key-value pairs."""
    from nexus.context import DialogueContext
    from atoms.stages.unified import OpeningBroadcastNLG

    ctx = DialogueContext(session_id="s-ob-1", user_query="q")
    ctx.metadata["task_info"] = {"product_name": "闲置iPhone", "price": "3000"}
    ctx = arun(OpeningBroadcastNLG().execute(ctx))

    assert ctx.nlg_result["content"] == (
        "您好，很高兴为您服务！\nproduct_name: 闲置iPhone\nprice: 3000"
    )


def test_opening_broadcast_template_fields():
    """Template passed: task_info fields embedded via str.format."""
    from nexus.context import DialogueContext
    from atoms.stages.unified import OpeningBroadcastNLG

    ctx = DialogueContext(session_id="s-ob-2", user_query="q")
    ctx.task_basic_info = {"product_name": "闲置iPhone"}
    stage = OpeningBroadcastNLG(template="您好，我是{product_name}的智能助手")
    ctx = arun(stage.execute(ctx))

    assert ctx.nlg_result["content"] == "您好，我是闲置iPhone的智能助手"


def test_opening_broadcast_template_missing_field_falls_back():
    """Template references a task_info field that is missing: warn and fall back to the default concatenation, no exception raised."""
    from nexus.context import DialogueContext
    from atoms.stages.unified import OpeningBroadcastNLG

    ctx = DialogueContext(session_id="s-ob-3", user_query="q")
    ctx.task_basic_info = {"product_name": "闲置iPhone"}
    stage = OpeningBroadcastNLG(template="您好，我是{seller_name}的助手")
    ctx = arun(stage.execute(ctx))

    assert ctx.nlg_result["content"] == (
        "您好，很高兴为您服务！\nproduct_name: 闲置iPhone"
    )


# ============================================================================
# Dual-track clarify combination tests (clarify declared on the node)
# ============================================================================

def test_clarify_next_node_rejected_when_disabled(pattern, sessions):
    """A node without the clarify declaration outputs a clarify signal: the
    allowed-values hard guard falls back to keeping the current node.

    The model's reply is an acknowledgment-style promise ("let me confirm for
    you"), but the node has no clarify stage installed to honor it, so the
    reply is replaced with the fallback copy as well (avoiding an empty
    promise).
    """
    from atoms.stages.unified import FSMUnifiedNLU

    session = launch(pattern, sessions)

    reply = chat_once(pattern, sessions, "硬造澄清意图")

    assert session.cxt.current_node_code == "u_ask_brand"
    assert session.cxt.nlu_result["next_node"] == ""
    assert session.cxt.metadata["unified"]["invalid_next_node"] == "clarify"
    assert reply == FSMUnifiedNLU.fallback_reply
    assert "clarify" not in session.cxt.metadata


def test_unified_with_clarify_off_topic_turn(pattern, sessions):
    """Unified stage + dual-track clarify: off-topic turn gets a kb answer + bring back on topic;
    node unchanged, slots unpolluted.

    The pipeline is [FSMUnifiedNLU, ClarifyStage, PassThroughNLG]:
    a clarify turn = unified call + clarify generation, 2 LLM calls in total (on par with
    two-stage + clarify); a normal turn is still 1 call.
    """
    from atoms.stages.clarify import ClarifyRouteRule, ClarifyStage
    from atoms.stages.recaller import (
        KeywordRecallPath,
        MultiPathRecaller,
        ScoreThresholdFilter,
        WeightedScoreFusion,
    )

    kb_docs = [
        {
            "id": "fee_policy",
            "content": "除车价外仅收取上牌费与服务费，无其他收费",
            "metadata": {"keywords": ["收费", "服务费", "上牌费"]},
        },
    ]

    # Test injection: declare the clarify slot on the u_ask_budget node with a
    # KB-backed stage registered under a unique code (restored afterwards, so
    # other cases in this file stay unpolluted)
    from nexus.registry.plugins import registry as plugin_registry
    budget = pattern.node_map["u_ask_budget"]
    saved_stages = dict(budget.stages or {})
    clarify_code = "clarify_kb_unified"
    if not plugin_registry.has("stage", clarify_code):
        plugin_registry.register("stage", clarify_code, lambda: ClarifyStage(
            recaller=MultiPathRecaller(
                recall_paths=[KeywordRecallPath(name="kb", documents=kb_docs)],
                filters=[ScoreThresholdFilter(threshold=0.1)],
                fusion=WeightedScoreFusion(),
            ),
            rule=ClarifyRouteRule(),
        ))
    budget.stages = {**saved_stages, "clarify": clarify_code}

    try:
        session = launch(pattern, sessions)

        # Turn 1: u_ask_brand digests the sentence in one call, advancing to u_ask_budget
        chat_once(pattern, sessions, "我想买车")
        assert session.cxt.current_node_code == "u_ask_budget"
        assert session.cxt.filled_slots.get("brand") == "我想买车"

        # Turn 2: off-topic (asking about fees when the budget should be asked) → the unified stage
        # emits a clarify signal; ClarifyStage overwrites the reply with a kb answer + bring back
        # on topic (unified + clarify generation = 2 calls)
        before = FakeProvider.call_count
        reply = chat(sessions, "s1", "还要收别的钱吗")
        assert FakeProvider.call_count - before == 2

        clarify_info = session.cxt.metadata["clarify"]
        assert clarify_info["triggered"] is True
        assert clarify_info["mode"] == "kb"
        assert "上牌费与服务费" in reply
        assert "预算" in reply
        assert session.cxt.current_node_code == "u_ask_budget"
        assert "topic" not in session.cxt.filled_slots
        assert session.cxt.filled_slots.get("brand") == "我想买车"

        # Turn 3: back to normal (answers the budget) → clarify metadata reset, flow keeps advancing
        reply = chat_once(pattern, sessions, "20万左右")
        assert session.cxt.metadata["clarify"]["triggered"] is False
        assert session.cxt.current_node_code == "u_confirm"
        assert session.cxt.filled_slots["budget"] == "20万左右"
    finally:
        budget.stages = saved_stages
