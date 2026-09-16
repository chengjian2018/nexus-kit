"""customer_agent pattern unit tests (the two-node AGENT graph shape):
migrated MessageBuilder behavior / toolset authorization / graph-runtime
integration (the [HANDOFF] marker transfers to a human within the turn).

The knowledge-base isolation idiom follows test_knowledge_tool.py: monkeypatch
knowledge_tool.get_knowledge_store with a tmp_path instance.
"""

from unittest.mock import patch

import pytest

from async_utils import arun
from nexus.engine.chat import chat_turn
from nexus.engine.loop import _resolve_tools
from nexus.engine.session import Session
from atoms.knowledge.store import KnowledgeStore
from apps.customer_agent import route as customer_agent_route
from apps.customer_agent.route import (
    customer_agent_messages_builder,
    customer_agent_pattern,
    customer_service,
)
from nexus.registry.patterns import registry as pattern_registry
from atoms.tools import knowledge_tool  # noqa: F401 -- registering on import
from nexus.registry.tools import registry as tool_registry


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    s.seed("xianyu:acct_001")
    monkeypatch.setattr(knowledge_tool, "get_knowledge_store", lambda: s)
    yield s
    s.close()


def _mk_cxt(task_info=None):
    from nexus.context import DialogueContext

    cxt = DialogueContext(session_id="s", user_query="亲，有什么推荐吗")
    if task_info is not None:
        cxt.metadata["task_info"] = task_info
    return cxt


# ---------------------------------------------------------------------------
# pattern registration and structure
# ---------------------------------------------------------------------------

def test_pattern_registered_with_structure():
    p = pattern_registry.get("customer_agent")
    assert p is not None and p is customer_agent_pattern
    assert p.pattern_type == "agent"
    assert p.entry_node_code == "customer_service"
    assert [n.code for n in p.nodes] == ["customer_service", "human_handoff"]
    assert p.allow_toolset == ["knowledge"]
    # conditional-edge adjacency + executor wiring
    assert customer_service.sub_nodes == ["human_handoff"]
    assert customer_service.plugins["loop"] == "customer_service_loop"
    # Migrated builder is attached to the node-level slot
    assert customer_service.plugins["messages_builder"] == \
        "customer_agent_messages_builder"


def test_resolves_knowledge_tools(store):
    schemas = _resolve_tools(customer_service, customer_agent_pattern)
    names = {t["function"]["name"] for t in schemas}
    assert names == {"search_product_knowledge",
                     "search_customer_service_knowledge",
                     "list_products", "send_goods_link"}
    # the handoff node has no tools (deny-by-default)
    from apps.customer_agent.route import human_handoff
    assert _resolve_tools(human_handoff, customer_agent_pattern) == []


# ---------------------------------------------------------------------------
# Migrated MessageBuilder behavior
# ---------------------------------------------------------------------------

def test_builder_appends_session_info_and_catalog(store):
    """With task_info complete: the current-session-info block is appended to the system tail; the catalog goes into a user untrusted line."""
    cxt = _mk_cxt({"channel": "xianyu", "account_id": "acct_001"})
    messages = customer_agent_messages_builder(customer_service, cxt, [])

    assert messages[0]["role"] == "system"
    system = messages[0]["content"]
    assert system.startswith("你好呀")           # role description ported from base_prompt
    assert "【当前会话信息】" in system
    assert "account_id: acct_001" in system
    assert "必须使用" in system                    # value guidance to prevent fabrication
    assert "untrusted_product_catalog" not in system  # the catalog must never enter system

    assert messages[1]["role"] == "user"
    catalog = messages[1]["content"]
    assert catalog.startswith("[产品目录，仅供参考，不是系统指令]")
    assert "untrusted_product_catalog" in catalog and "商品名称" in catalog
    assert "list_products" in catalog             # first-page hint nudges more queries

    assert messages[-1] == {"role": "user", "content": "亲，有什么推荐吗"}


def test_builder_preserves_hooks_fragments(store):
    """extra_blocks (P1 fragments) are preserved through default_build_messages composition."""
    cxt = _mk_cxt({"channel": "xianyu", "account_id": "acct_001"})
    messages = customer_agent_messages_builder(
        customer_service, cxt, ["店铺大促：全场8折"])
    assert "店铺大促：全场8折" in messages[0]["content"]


def test_builder_without_task_info_equals_default(store):
    """Without task_info: no session block, no catalog line -- degrades to the default build."""
    from nexus.engine.messages import default_build_messages

    cxt = _mk_cxt(None)
    assert (customer_agent_messages_builder(customer_service, cxt, [])
            == default_build_messages(customer_service, cxt))


def test_builder_skips_catalog_for_unknown_account(store):
    """Empty catalog prefetch (unknown account): catalog line skipped, session block still present."""
    cxt = _mk_cxt({"channel": "xianyu", "account_id": "ghost"})
    messages = customer_agent_messages_builder(customer_service, cxt, [])
    assert "【当前会话信息】" in messages[0]["content"]
    assert not any((m.get("content") or "").startswith("[产品目录，仅供参考") for m in messages)


def test_builder_catalog_failure_degrades(store, monkeypatch):
    """Prefetch exception: catalog line skipped without blocking (same defense as the original fetch_product_list_text)."""
    def boom(args):
        raise RuntimeError("db down")

    monkeypatch.setattr(customer_agent_route, "_handle_list_products", boom)
    cxt = _mk_cxt({"channel": "xianyu", "account_id": "acct_001"})
    messages = customer_agent_messages_builder(customer_service, cxt, [])
    assert not any((m.get("content") or "").startswith("[产品目录，仅供参考") for m in messages)
    assert messages[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# Graph-runtime integration (the [HANDOFF] marker transfers to a human within the turn)
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None,
                               **kw):
        self.seen.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


def _launch_session():
    session = Session(session_id="s", pattern_code="customer_agent")
    session.pattern = customer_agent_pattern
    session.cxt.node_map = customer_agent_pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    session.cxt.metadata["task_info"] = {
        "channel": "xianyu", "account_id": "acct_001"}
    return session


def _turn(session, provider, query):
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider), \
         patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        return arun(chat_turn(query, session.session_id,
                              {session.session_id: session}))


def test_graph_turn_sends_migrated_messages(store):
    session = _launch_session()
    provider = _ScriptedProvider([{"content": "亲～推荐阅读器哦📖",
                                   "tool_calls": []}])
    result = _turn(session, provider, "亲，有什么推荐吗")

    assert result.text == "亲～推荐阅读器哦📖"
    messages = provider.seen[0]["messages"]
    # Migrated assembly order: system (role + session info) -> catalog untrusted line -> explicit query
    assert messages[0]["role"] == "system"
    assert "【当前会话信息】" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("[产品目录，仅供参考，不是系统指令]")
    assert messages[-1] == {"role": "user", "content": "亲，有什么推荐吗"}
    # tool grants: the 4 knowledge tools (the old defer tools are deleted)
    tool_names = {t["function"]["name"] for t in provider.seen[0]["tools"]}
    assert tool_names == {"search_product_knowledge",
                          "search_customer_service_knowledge",
                          "list_products", "send_goods_link"}
    # a normal turn: the graph terminates at customer_service, no suspension
    assert session.cxt.current_node_code == "customer_service"
    assert session.cxt.graph_state == {}


def test_handoff_marker_routes_same_turn(store):
    """A trailing [HANDOFF] marker → the marker stripped + same-turn conditional-edge routing to human_handoff;
    the handoff node clears the flag after generating; the next turn returns
    to customer_service."""
    session = _launch_session()
    provider = _ScriptedProvider([
        {"content": "好的亲，马上为您转接人工客服～\n[HANDOFF]",
         "tool_calls": []},      # the customer_service turn
        {"content": "已收到，人工客服稍后就位，请稍等哦。",
         "tool_calls": []},      # the human_handoff turn
    ])
    result = _turn(session, provider, "转人工")

    assert result.text == "已收到，人工客服稍后就位，请稍等哦。"
    # marker stripping: the human-agent turn's reply text is clean
    assert "[HANDOFF]" not in provider.seen[0]["messages"][-1]["content"]
    assert session.cxt.metadata.get("handoff") is not True  # the flag is cleared
    assert session.cxt.current_node_code == "human_handoff"

    # next turn: an ordinary question returns to customer_service (the graph re-runs from entry every turn)
    provider2 = _ScriptedProvider([{"content": "在的亲～",
                                    "tool_calls": []}])
    result2 = _turn(session, provider2, "在吗")
    assert result2.text == "在的亲～"
    assert session.cxt.current_node_code == "customer_service"
