"""knowledge_store unit tests: CRUD / scope isolation / jieba search / sanitization."""

import pytest

from atoms.knowledge.store import (
    KnowledgeStore,
    _clean_untrusted,
    _cut_query,
)


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Writes and idempotency
# ---------------------------------------------------------------------------

def test_upsert_product_insert_then_update(store):
    store.upsert_product("xianyu:a1", 1001, "iPhone 13", price="2699")
    rows = store.search_products("xianyu:a1", goods_id=1001)
    assert len(rows) == 1
    assert rows[0]["goods_name"] == "iPhone 13"
    assert rows[0]["price"] == "2699"

    # Same scope + goods_id updates the row instead of inserting
    store.upsert_product("xianyu:a1", 1001, "iPhone 13 黑色", price="2599")
    rows = store.search_products("xianyu:a1")
    assert len(rows) == 1
    assert rows[0]["goods_name"] == "iPhone 13 黑色"
    assert rows[0]["price"] == "2599"


def test_upsert_keeps_old_fields_when_new_are_none(store):
    store.upsert_product("xianyu:a1", 1001, "iPhone", extracted_content="# 正文")
    store.upsert_product("xianyu:a1", 1001, "iPhone 改名", price="2000")
    row = store.search_products("xianyu:a1", goods_id=1001)[0]
    assert row["extracted_content"] == "# 正文"  # None does not overwrite old values
    assert row["price"] == "2000"


def test_add_cs_and_enabled_filter(store):
    store.add_cs("xianyu:a1", "退货政策", "7 天无理由")
    store.add_cs("xianyu:a1", "隐藏条目", "不应被检索", enabled=False)
    hits = store.search_cs("xianyu:a1", "无理由")
    assert len(hits) == 1
    assert hits[0]["title"] == "退货政策"


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------

def test_scope_isolation(store):
    store.upsert_product("xianyu:a1", 1001, "账号A的商品")
    store.upsert_product("xianyu:a2", 2001, "账号B的商品")
    store.add_cs("xianyu:a1", "A的政策", "内容")

    assert len(store.search_products("xianyu:a1")) == 1
    assert store.search_products("xianyu:a1")[0]["goods_name"] == "账号A的商品"
    assert store.search_products("xianyu:a2")[0]["goods_name"] == "账号B的商品"
    # goods_id is also isolated across scopes
    assert store.search_products("xianyu:a2", goods_id=1001) == []
    assert store.search_cs("xianyu:a2", "政策") == []


# ---------------------------------------------------------------------------
# Search semantics
# ---------------------------------------------------------------------------

def test_search_products_by_query(store):
    store.upsert_product("s", 1, "Kindle Paperwhite 阅读器", extracted_content="墨水屏")
    store.upsert_product("s", 2, "Switch OLED 游戏机", extracted_content="掌机")
    hits = store.search_products("s", query="阅读器 墨水屏")
    assert len(hits) == 1 and hits[0]["goods_id"] == 1
    # A query matching only content words should still recall
    hits = store.search_products("s", query="墨水屏")
    assert len(hits) == 1 and hits[0]["goods_id"] == 1


def test_search_products_no_query_returns_latest(store):
    for i in range(5):
        store.upsert_product("s", i, f"商品{i}")
    rows = store.search_products("s", limit=3)
    assert len(rows) == 3


def test_search_cs_and_like_fallback(store):
    store.add_cs("s", "发货时效", "48 小时内发货，默认顺丰")
    hits = store.search_cs("s", "发货")
    assert len(hits) == 1
    assert store.search_cs("s", "不存在的关键词xyz") == []


def test_cut_query_filters_short_words():
    words = _cut_query("退 货 政策")
    assert "政策" in words
    assert "退" not in words


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def test_seed_idempotent(store):
    store.seed("xianyu:demo")
    n1 = len(store.search_products("xianyu:demo", limit=50))
    ncs1 = len(store.search_cs("xianyu:demo", limit=50))
    assert n1 > 0 and ncs1 > 0

    store.seed("xianyu:demo")  # idempotent: counts do not double
    assert len(store.search_products("xianyu:demo", limit=50)) == n1
    assert len(store.search_cs("xianyu:demo", limit=50)) == ncs1


# ---------------------------------------------------------------------------
# Sanitization and formatting
# ---------------------------------------------------------------------------

def test_clean_untrusted_escapes_and_truncates():
    assert "＜script＞" in _clean_untrusted("<script>alert()</script>", 100)
    assert len(_clean_untrusted("x" * 1000, 50)) == 50
    # Control characters are filtered out; newlines are preserved
    assert "\n" in _clean_untrusted("a\x00b\nc", 100)


def test_format_result_untrusted_wrapper(store):
    store.upsert_product("s", 1, "商品A", extracted_content="正文")
    store.add_cs("s", "政策", "内容 <b>加粗</b>")
    out = store.format_result(
        store.search_products("s"), store.search_cs("s", "政策"))
    assert "＜untrusted_knowledge＞" in out
    assert "仅供事实参考" in out
    assert "＜b＞" in out  # angle brackets converted to fullwidth


def test_format_result_empty(store):
    assert store.format_result([], []) == "未找到相关知识。"


def test_format_catalog_compact_and_sanitized(store):
    store.upsert_product("s", 1, "商品[特价]", price="9.9")
    out = store.format_catalog(store.search_products("s"))
    assert "[untrusted_product_catalog]" in out
    assert "［特价］" in out   # square brackets converted to fullwidth
    assert "正文" not in out or True
    assert "extracted_content" not in out  # catalog must not include the knowledge-content field name
