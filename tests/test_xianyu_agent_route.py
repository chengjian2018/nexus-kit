"""xianyu_agent (ROUTE mode) offline tests -- replicating xianyu-auto-reply agent dialogue management.

LLM output is simulated via the scripted FakeProvider (no real API access). Covers:
1. Pattern structure and AST auto-discovery registration
2. Local intent detection keyword tables (price/tech/default, replicating detect_intent)
3. Intent routing: bargain/tech/default -> corresponding menu nodes, back to root at turn end
4. Bargain round count control: from the max_bargain_rounds-th haggle on, fixed refusal script and zero LLM
5. Bargain param injection: bargain_count/max_* enter filled_slots via slots for NLG
6. Custom bargain settings: metadata.bargain_settings overrides the defaults
"""

import logging

import pytest

from fake_provider import (
    FakeProvider,
    fake_llm_config,
    register_fake_provider,
)

logging.basicConfig(level=logging.WARNING)

REFUSE_TEXT = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="session", autouse=True)
def _fake_provider():
    """Register the scripted provider, reused for the whole test run."""
    register_fake_provider()


@pytest.fixture(scope="module")
def pattern():
    """Discover builtin patterns and return xianyu_agent."""
    from nexus.registry.patterns import discover_builtin_patterns, registry

    imported = discover_builtin_patterns()
    assert "apps.xianyu_agent.route" in imported, (
        f"xianyu_agent_route 未被自动发现，已发现: {imported}"
    )
    return registry.get("xianyu_agent")


@pytest.fixture()
def sessions():
    """A fresh session container per test."""
    return {}


def launch(pattern, sessions, session_id="s1", bargain_settings=None):
    """Simulate main.py's launch flow: register the session and inject pipeline context."""
    from nexus.engine.session import Session

    session = Session(session_id=session_id, pattern_code=pattern.code)
    session.pattern = pattern
    session.task_info = {}
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["task_info"] = {
        "channel": "xianyu", "account_id": "acc1", "item_id": "item1",
    }
    if bargain_settings is not None:
        session.cxt.metadata["bargain_settings"] = bargain_settings
    session.cxt.metadata["llm_override"] = fake_llm_config()
    sessions[session_id] = session
    return session


def chat(sessions, session_id, query):
    """Run one dialogue turn via nexus.engine.chat."""
    from nexus.engine.chat import chat as chat_fn

    return chat_fn(query=query, session_id=session_id, all_sessions=sessions)


# ============================================================================
# Structure tests
# ============================================================================

def test_pattern_auto_discovered_and_structure(pattern):
    """Pattern is AST-auto-discoverable; module/node structure and ROUTE semantics are correct."""
    from nexus.model.module import ModuleType

    assert pattern.code == "xianyu_agent"
    assert pattern.entry_module_code == "xianyu_root"
    assert set(pattern.module_map) == {"xianyu_root"}

    root = pattern.module_map["xianyu_root"]
    assert root.type == ModuleType.ROUTE

    # Route module node order: root must be module_nodes[0] (first node)
    assert [n.node_code for n in root.module_nodes] == [
        "xy_route_root", "xy_menu_price", "xy_menu_price_refuse",
        "xy_menu_tech", "xy_menu_default",
    ]

    # All intent menu nodes have no jump_module: stay in the route module, return to root every turn
    for node in root.module_nodes[1:]:
        assert not getattr(node, "jump_module", None)

    # Intent menu nodes carry intent-level NLG templates (except the refusal node: fixed script)
    assert pattern.node_map["xy_menu_price"].base_nlg_prompt
    assert pattern.node_map["xy_menu_tech"].base_nlg_prompt
    assert pattern.node_map["xy_menu_default"].base_nlg_prompt


def test_generate_wired_at_module_level(pattern):
    """XianyuIntentNLU / FixedNLG are wired into the module-level generate dict (nlu/nlg slots)."""
    from apps.xianyu_agent.route import FixedNLG, XianyuIntentNLU

    root = pattern.module_map["xianyu_root"]
    stages = root.stages
    assert isinstance(stages, dict)
    assert stages["nlu"] == "xianyu_intent_nlu"
    assert stages["nlg"] == "xianyu_fixed_nlg"
    # 声明的 code 可解析到实现类（插件中心 kind="stage"）
    from nexus.registry.plugins import registry as plugin_registry
    import atoms.stages  # noqa: F401
    import apps.xianyu_agent.route  # noqa: F401 -- registers app-local codes
    assert isinstance(plugin_registry.resolve("stage", "xianyu_intent_nlu"),
                      XianyuIntentNLU)
    assert isinstance(plugin_registry.resolve("stage", "xianyu_fixed_nlg"),
                      FixedNLG)


def test_query_slot_wired_with_time_aug(pattern):
    """The pattern-level query slot is wired with TimeAugQueryRewriter (time-augmented rewrite)."""
    from atoms.stages.query import TimeAugQueryRewriter

    # plan-②: the query slot is declared in the skeleton by string code
    skeleton_values = {slot: code for e in pattern.stages
                       for slot, code in e.items()}
    assert skeleton_values.get("query") == "time_aug_query"
    from nexus.registry.plugins import registry as plugin_registry
    assert isinstance(plugin_registry.resolve("stage", "time_aug_query"),
                      TimeAugQueryRewriter)


# ============================================================================
# Intent detection tests (replicating the detect_intent keyword table)
# ============================================================================

@pytest.mark.parametrize("query,intent", [
    ("能便宜点吗", "price"),
    ("多少钱", "price"),
    ("可以刀一点吗", "price"),
    ("包个邮吧", "price"),
    ("最低什么价", "price"),
    ("这个怎么用", "tech"),
    ("有什么功能", "tech"),
    ("参数发一下", "tech"),
    ("在吗", "default"),
    ("今天发货吗", "default"),
    ("HELLO 在吗", "default"),  # matched after lower(); non-keyword still default
])
def test_detect_intent_keywords(query, intent):
    """Local keyword intent detection matches the original implementation's keyword tables."""
    from apps.xianyu_agent.route import detect_intent

    assert detect_intent(query) == intent


# ============================================================================
# Intent routing tests
# ============================================================================

def test_intent_routing_each_turn(pattern, sessions):
    """All three intents route to their menu nodes; back to root at turn end (independent detection each turn)."""
    session = launch(pattern, sessions)

    chat(sessions, "s1", "能便宜点吗")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_price"
    assert session.cxt.nlu_result["intent"] == "price"
    assert session.cxt.current_node_code == "xy_route_root"

    chat(sessions, "s1", "这个怎么用")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_tech"

    chat(sessions, "s1", "在吗")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_default"


def test_intent_metadata_written_for_counting(pattern, sessions):
    """Each turn's user message gets intent written back into metadata, for bargain count lookback."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "多少钱")
    user_msgs = [m for m in session.cxt.history if m.role == "user"]
    assert user_msgs[-1].metadata.get("intent") == "price"

    chat(sessions, "s1", "怎么下载驱动")
    user_msgs = [m for m in session.cxt.history if m.role == "user"]
    assert user_msgs[-1].metadata.get("intent") == "tech"


# ============================================================================
# Bargain round count control tests
# ============================================================================

def test_bargain_refuse_at_threshold_zero_llm(pattern, sessions):
    """From the max_bargain_rounds-th haggle on: fixed refusal script + zero LLM calls."""
    session = launch(pattern, sessions)
    queries = ["能便宜点吗", "还能再少点", "最低多少钱", "再刀50"]

    llm_calls = []
    for i, q in enumerate(queries, 1):
        before = FakeProvider.call_count
        reply = chat(sessions, "s1", q)
        llm_calls.append(FakeProvider.call_count - before)

        if i < 3:
            # First two turns: normal bargain node, a single LLM generation
            assert session.cxt.nlu_result["next_node"] == "xy_menu_price"
            assert llm_calls[-1] == 1
        else:
            # Turns 3/4: count >= max (3) -> fixed refusal, zero LLM
            assert session.cxt.nlu_result["next_node"] == "xy_menu_price_refuse"
            assert reply == REFUSE_TEXT
            assert llm_calls[-1] == 0

    assert session.cxt.nlu_result["slots"]["bargain_count"] == 4


def test_bargain_count_persists_across_interleaved_intents(pattern, sessions):
    """Bargain count persists across turns: interleaved non-bargain messages do not reset it."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "能便宜点吗")     # price #1
    chat(sessions, "s1", "这个怎么用")
    chat(sessions, "s1", "还能再少点")     # price #2
    chat(sessions, "s1", "在吗")
    reply = chat(sessions, "s1", "最低多少钱")  # price #3 -> refusal

    assert session.cxt.nlu_result["slots"]["bargain_count"] == 3
    assert reply == REFUSE_TEXT


def test_custom_bargain_settings(pattern, sessions):
    """metadata.bargain_settings overrides the default bargain settings (max=1 -> refused on the first haggle)."""
    session = launch(pattern, sessions,
                     bargain_settings={"max_bargain_rounds": 1})
    reply = chat(sessions, "s1", "能便宜点吗")

    assert session.cxt.nlu_result["next_node"] == "xy_menu_price_refuse"
    assert reply == REFUSE_TEXT


def test_bargain_params_injected_into_slots(pattern, sessions):
    """Bargain params (count/max_*) merge into filled_slots via slots, for NLG template injection."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "能便宜点吗")

    assert session.cxt.filled_slots["bargain_count"] == 1
    assert session.cxt.filled_slots["max_bargain_rounds"] == 3
    assert session.cxt.filled_slots["max_discount_percent"] == 10
    assert session.cxt.filled_slots["max_discount_amount"] == 100


def test_non_price_intent_no_bargain_params(pattern, sessions):
    """Non-bargain intents get bargain_count=0; params are still injected (templates can reference them uniformly)."""
    session = launch(pattern, sessions)
    chat(sessions, "s1", "这个怎么用")

    assert session.cxt.nlu_result["intent"] == "tech"
    assert session.cxt.filled_slots["bargain_count"] == 0


# ============================================================================
# Prompt assembly tests
# ============================================================================

def test_price_prompt_contains_bargain_context(pattern, sessions):
    """The bargain NLG prompt contains four elements: product info / history / bargain settings / buyer message."""
    session = launch(pattern, sessions)

    captured = {}
    from atoms.stages.nlg import BaseNLG
    original = BaseNLG._call_llm

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    BaseNLG._call_llm = spy
    try:
        chat(sessions, "s1", "能便宜点吗")
    finally:
        BaseNLG._call_llm = original

    prompt = captured["prompt"]
    assert "议价" in prompt
    assert "商品信息" in prompt
    assert "item_id: item1" in prompt          # task_info product info injected
    assert "对话历史" in prompt
    assert "议价设置" in prompt
    assert "bargain_count" in prompt           # bargain params injected
    assert "能便宜点吗" in prompt


def test_intent_specific_prompt_selected(pattern, sessions):
    """Tech intent uses the tech template (with the "tech expert" persona); default intent uses the default template."""
    session = launch(pattern, sessions)

    from atoms.stages.nlg import BaseNLG
    original = BaseNLG._call_llm
    captured = []

    def spy(self, prompt, llm_config=None):
        captured.append(prompt)
        return original(self, prompt, llm_config)

    BaseNLG._call_llm = spy
    try:
        chat(sessions, "s1", "这个怎么用")   # tech
        chat(sessions, "s1", "今天发货吗")   # default
    finally:
        BaseNLG._call_llm = original

    assert "技术专家" in captured[0]
    assert "电商卖家" in captured[1]


# ============================================================================
# Time augmentation rewrite end-to-end tests (query slot -> NLU/NLG consume the augmented message)
# ============================================================================

def test_time_augmented_query_flows_into_prompt(pattern, sessions):
    """A buyer message carrying relative time is augmented by TimeAugQueryRewriter and lands in the NLG prompt.

    Injects a fixed time_base (2026-09-03 10:00:00, Thursday); the query asking to
    ship before "3pm tomorrow" gets an augmented annotation with the resolved absolute
    time (jionlp range parsing, including the next day 2026-09-04).
    The default intent goes through the LLM fallback: FakeProvider returns non-label
    text, falls back to the default menu -> FixedNLG, a single LLM call.
    """
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-03 10:00:00", "%Y-%m-%d %H:%M:%S"))

    from atoms.stages.nlg import BaseNLG
    original = BaseNLG._call_llm
    captured = {}

    def spy(self, prompt, llm_config=None):
        captured["prompt"] = prompt
        return original(self, prompt, llm_config)

    BaseNLG._call_llm = spy
    try:
        chat(sessions, "s1", "明天下午3点前能发货吗")
    finally:
        BaseNLG._call_llm = original

    # The rewrite lands in ctx and the NLG prompt (the augmented annotation contains the resolved absolute time)
    assert session.cxt.rewritten_queries[0] != "明天下午3点前能发货吗"
    assert "2026-09-04" in session.cxt.rewritten_queries[0]
    assert session.cxt.rewritten_queries[0] in captured["prompt"]
