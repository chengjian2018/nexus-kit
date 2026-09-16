"""knowledge_tool unit tests: registration / ACL / dispatch paths / scope derivation / ownership validation.

Tools read the global get_knowledge_store() (defaults to data/knowledge.db);
tests monkeypatch in a tmp_path instance to avoid polluting real data files.
"""

import json

import pytest

from atoms.tools import knowledge_tool  # noqa: F401 -- registers on import
from atoms.knowledge.store import KnowledgeStore
from nexus.registry.tools import registry


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    s.seed("xianyu:acct_001")
    monkeypatch.setattr(knowledge_tool, "get_knowledge_store", lambda: s)
    yield s
    s.close()


def _dispatch(name: str, **args) -> str:
    from async_utils import arun
    return arun(registry.dispatch(name, args))


# ---------------------------------------------------------------------------
# Registration and pattern ACL (must hold in both directions)
# ---------------------------------------------------------------------------

def test_tools_registered():
    for name in ("search_product_knowledge",
                 "search_customer_service_knowledge",
                 "list_products", "send_goods_link"):
        assert registry.get_entry(name) is not None, name
        assert registry.get_toolset_for_tool(name) == "knowledge"


def test_toolset_authorization_grant_and_deny():
    """Authorization = pattern.allow_toolset ∩ node.use_tools (both
    deny-by-default)."""
    from nexus.engine.loop import _resolve_tools
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    names = ("search_product_knowledge",
             "search_customer_service_knowledge",
             "list_products", "send_goods_link")

    grant = Pattern(code="customer_agent", name="c", description="d",
                    allow_toolset=["knowledge"],
                    nodes=[BaseNode(code="main", use_tools=list(names))])
    resolved = {t["function"]["name"]
                for t in _resolve_tools(grant.node_map["main"], grant)}
    assert set(names) <= resolved

    # deny-by-default: the pattern never granted the toolset → listing it on the node does nothing
    deny = Pattern(code="xianyu_agent", name="x", description="d",
                   allow_toolset=["mcp-websearch"],
                   nodes=[BaseNode(code="root", use_tools=list(names))])
    assert _resolve_tools(deny.node_map["root"], deny) == []


# ---------------------------------------------------------------------------
# dispatch normal paths
# ---------------------------------------------------------------------------

def test_search_product_by_goods_id(store):
    out = _dispatch("search_product_knowledge",
                    account_id="acct_001", goods_id=1001)
    assert "iPhone 13" in out
    assert "＜untrusted_knowledge＞" in out


def test_search_product_by_query(store):
    out = _dispatch("search_product_knowledge",
                    account_id="acct_001", query="阅读器 墨水屏")
    assert "Kindle" in out


def test_search_cs(store):
    out = _dispatch("search_customer_service_knowledge",
                    account_id="acct_001", query="退货")
    assert "7 天" in out or "7天" in out
    assert "【客服知识】" in out


def test_list_products_catalog(store):
    out = _dispatch("list_products", account_id="acct_001", limit=2)
    assert "[untrusted_product_catalog]" in out
    assert "商品ID:" in out
    # catalog must not include knowledge content
    assert "电池健康" not in out


def test_send_goods_link_happy_path(store):
    out = _dispatch("send_goods_link", account_id="acct_001", goods_id=1002)
    payload = json.loads(out)
    assert "goofish.com/item?id=1002" in payload["goods_card"]
    assert "AirPods" in payload["goods_card"]


# ---------------------------------------------------------------------------
# dispatch error/edge paths
# ---------------------------------------------------------------------------

def test_missing_account_id(store):
    for name in ("search_product_knowledge", "search_customer_service_knowledge",
                 "list_products", "send_goods_link"):
        out = _dispatch(name)
        assert "error" in json.loads(out), name


def test_search_requires_goods_or_query(store):
    out = _dispatch("search_product_knowledge", account_id="acct_001")
    assert "error" in json.loads(out)


def test_send_goods_link_rejects_unknown_goods(store):
    """Ownership validation: a goods_id outside the current scope is rejected (prevents LLM fabrication)."""
    out = _dispatch("send_goods_link", account_id="acct_001", goods_id=999999)
    payload = json.loads(out)
    assert "error" in payload
    assert "不属于当前账号" in payload["error"]


def test_scope_derived_from_account_id(store):
    """Account isolation: acct_002 has no seed data; searches return empty and never leak acct_001's data."""
    out = _dispatch("search_product_knowledge",
                    account_id="acct_002", goods_id=1001)
    assert out == "未找到相关知识。"
    catalog = _dispatch("list_products", account_id="acct_002")
    assert "未找到商品" in catalog


def test_empty_result_is_not_error(store):
    out = _dispatch("search_customer_service_knowledge",
                    account_id="acct_001", query="不存在的词xyz")
    assert "error" not in out
    assert out == "未找到相关知识。"
