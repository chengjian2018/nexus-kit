"""xianyu_agent（plan-⑧ AGENT 图形态）离线测试——复刻闲鱼自动回复的多轮
对话管理。

LLM 输出由脚本化 FakeProvider 模拟（无真实 API）。图结构：路由根节点
（xianyu_router 执行器：本地意图规则 + LLM 兜底 + 议价计数）条件边分发到
四个菜单节点；议价拒绝节点挂规则执行器（answer_examples 直出，零 LLM），
其余菜单节点挂生成执行器。每条买家消息从入口重跑全图（原"轮末回根"天然
成立）。覆盖：
1. 图结构与 AST 自动发现注册（节点邻接、执行器接线、prompt 资产在 config）
2. 本地意图关键词表（price/tech/default，复刻 detect_intent）
3. 意图路由：三轮独立检测、命中对应菜单节点
4. 议价轮次控制：第 max_bargain_rounds 刀起固定拒绝话术 + 零 LLM
5. 议价参数注入：bargain_count/max_* 经 slots 进 filled_slots 供 NLG
6. 自定义议价配置：metadata.bargain_settings 覆盖默认
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
        f"xianyu_agent 未被自动发现，已发现: {imported}"
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
    from async_utils import arun
    from nexus.engine.chat import chat as chat_fn

    return arun(chat_fn(query=query, session_id=session_id,
                        all_sessions=sessions))


class _PromptSpy:
    """Install a plain-function hook on FakeProvider to capture every prompt
    it receives (a plain function rebinds to the calling instance, so the
    class-level call_count machinery keeps working)."""

    def __init__(self):
        self.prompts = []
        self.original = FakeProvider._achat_completion_impl
        spy = self

        async def _impl(provider, messages, model, temperature, max_tokens,
                        stream=False, **kwargs):
            spy.prompts.append(messages[0]["content"])
            type(provider).call_count += 1
            from fake_provider import scripted_response
            return {"content": scripted_response(messages[0]["content"])}

        self._impl = _impl

    def __enter__(self):
        FakeProvider._achat_completion_impl = self._impl
        return self

    def __exit__(self, *exc):
        FakeProvider._achat_completion_impl = self.original


# ============================================================================
# Structure tests
# ============================================================================

def test_pattern_auto_discovered_and_structure(pattern):
    """Pattern is AST-auto-discoverable; two-layer graph structure is correct."""
    assert pattern.code == "xianyu_agent"
    assert pattern.pattern_type == "agent"
    assert pattern.entry_node_code == "xy_route_root"

    assert [n.code for n in pattern.nodes] == [
        "xy_route_root", "xy_menu_price", "xy_menu_price_refuse",
        "xy_menu_tech", "xy_menu_default",
    ]
    # 条件边邻接：根节点路由到四个菜单节点；菜单节点无后继（图自然终止）
    root = pattern.node_map["xy_route_root"]
    assert set(root.sub_nodes) == {"xy_menu_price", "xy_menu_price_refuse",
                                   "xy_menu_tech", "xy_menu_default"}
    for code in ("xy_menu_price", "xy_menu_price_refuse",
                 "xy_menu_tech", "xy_menu_default"):
        assert pattern.node_map[code].sub_nodes == []

    # 执行器接线：根 = 路由执行器；拒绝节点 = 规则执行器；其余 = 生成执行器
    assert root.plugins["loop"] == "xianyu_router"
    assert pattern.node_map["xy_menu_price_refuse"].plugins["loop"] == \
        "xianyu_rule_reply"
    for code in ("xy_menu_price", "xy_menu_tech", "xy_menu_default"):
        assert pattern.node_map[code].plugins["loop"] == "xianyu_reply"

    # 执行器 code 经插件中心可解析
    from nexus.registry.plugins import registry as plugin_registry
    for code in ("xianyu_router", "xianyu_reply", "xianyu_rule_reply"):
        assert plugin_registry.has("executor", code), code

    # 意图级 NLG 模板在节点 config（拒绝节点以 answer_examples 承载话术）
    assert pattern.node_map["xy_menu_price"].get_prompt("base_nlg_prompt")
    assert pattern.node_map["xy_menu_tech"].get_prompt("base_nlg_prompt")
    assert pattern.node_map["xy_menu_default"].get_prompt("base_nlg_prompt")
    assert pattern.node_map["xy_menu_price_refuse"].answer_examples == [REFUSE_TEXT]

    # AGENT 图不跑 stages（无骨架）
    assert pattern.stages == []


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
    """All three intents route to their menu nodes; each turn re-routes from
    the entry (independent detection — the full-graph rerun semantic)."""
    session = launch(pattern, sessions)

    reply = chat(sessions, "s1", "能便宜点吗")
    assert session.cxt.nlu_result["next_node"] == "xy_menu_price"
    assert session.cxt.nlu_result["intent"] == "price"
    assert session.cxt.current_node_code == "xy_menu_price"  # 图位置镜像=命中的菜单节点
    assert reply  # 生成执行器产出回复

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
            # Turns 3/4: count >= max (3) -> rule executor, zero LLM
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
    """The bargain reply prompt contains four elements: product info / history / bargain settings / buyer message."""
    session = launch(pattern, sessions)

    with _PromptSpy() as spy:
        chat(sessions, "s1", "能便宜点吗")

    prompt = spy.prompts[-1]  # 最后一次调用是生成执行器的买家回复
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

    with _PromptSpy() as spy:
        chat(sessions, "s1", "这个怎么用")   # tech（本地命中，1 次生成调用）
        chat(sessions, "s1", "今天发货吗")   # default（本地未命中 → LLM 分类兜底 + 生成）

    assert "技术专家" in spy.prompts[0]
    assert "电商卖家" in spy.prompts[-1]  # 末次调用是 default 模板生成
    # 中间那次是意图分类兜底提示词
    assert any("通用意图分类器" in p for p in spy.prompts)


# ============================================================================
# Time augmentation end-to-end (router inline rewrite → prompts consume it)
# ============================================================================

def test_time_augmented_query_flows_into_prompt(pattern, sessions):
    """A buyer message carrying relative time is augmented by the router's
    inline TimeAugQueryRewriter and lands in the reply prompt."""
    import time as _time

    session = launch(pattern, sessions)
    session.cxt.metadata["time_base"] = _time.mktime(
        _time.strptime("2026-09-03 10:00:00", "%Y-%m-%d %H:%M:%S"))

    with _PromptSpy() as spy:
        chat(sessions, "s1", "明天下午3点前能发货吗")

    # The rewrite lands in ctx and the reply prompt (resolved absolute time)
    assert session.cxt.rewritten_queries[0] != "明天下午3点前能发货吗"
    assert "2026-09-04" in session.cxt.rewritten_queries[0]
    assert session.cxt.rewritten_queries[0] in spy.prompts[-1]
