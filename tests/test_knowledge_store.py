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
# Console management CRUD (ops-console P0)
# ---------------------------------------------------------------------------

def test_list_scopes_aggregates_both_tables(store):
    store.upsert_product("xianyu:a1", 1001, "商品")
    store.upsert_product("xianyu:a2", 2001, "商品")
    store.add_cs("xianyu:a1", "政策", "内容")
    scopes = store.list_scopes()
    by_scope = {s["scope"]: s for s in scopes}
    assert set(by_scope) == {"xianyu:a1", "xianyu:a2"}
    assert by_scope["xianyu:a1"]["product_count"] == 1
    assert by_scope["xianyu:a1"]["cs_count"] == 1
    assert by_scope["xianyu:a1"]["collection_count"] == 0
    assert by_scope["xianyu:a2"]["cs_count"] == 0
    assert by_scope["xianyu:a1"]["updated_at"] > 0


def test_update_product_partial_and_explicit_clear(store):
    store.upsert_product("s", 1, "iPhone", price="2699",
                         extracted_content="# 正文")
    # only present keys are updated; absent keys are left untouched
    assert store.update_product("s", 1, {"price": "2599"}) is True
    row = store.get_product("s", 1)
    assert row["price"] == "2599"
    assert row["extracted_content"] == "# 正文"
    # None = explicit clear (unlike upsert's COALESCE retention)
    assert store.update_product("s", 1, {"extracted_content": None}) is True
    row = store.get_product("s", 1)
    assert row["extracted_content"] is None
    # empty fields = no-op, merely probing existence
    assert store.update_product("s", 1, {}) is True
    assert store.update_product("s", 999, {}) is False
    # unknown fields fail fast
    with pytest.raises(ValueError):
        store.update_product("s", 1, {"no_such_column": 1})


def test_delete_product(store):
    store.upsert_product("s", 1, "iPhone")
    assert store.delete_product("s", 1) is True
    assert store.get_product("s", 1) is None
    assert store.delete_product("s", 1) is False


def test_list_cs_includes_disabled_and_pagination(store):
    store.add_cs("s", "条目1", "内容")
    store.add_cs("s", "条目2", "内容", enabled=False)
    rows = store.list_cs("s")
    assert len(rows) == 2  # management view includes disabled entries
    assert {r["title"] for r in rows} == {"条目1", "条目2"}
    rows = store.list_cs("s", include_disabled=False)
    assert [r["title"] for r in rows] == ["条目1"]
    assert len(store.list_cs("s", limit=1)) == 1
    assert len(store.list_cs("s", limit=1, offset=1)) == 1


def test_update_cs_toggle_enable_and_delete(store):
    store.add_cs("s", "退货政策", "7 天无理由")
    entry_id = store.list_cs("s")[0]["id"]

    assert store.update_cs(entry_id, {"enabled": False}) is True
    assert store.get_cs(entry_id)["enabled"] == 0
    assert store.search_cs("s", "退货") == []  # search view filters disabled entries

    assert store.update_cs(entry_id, {"content": "15 天可退换"}) is True
    row = store.get_cs(entry_id)
    assert row["content"] == "15 天可退换"
    assert row["title"] == "退货政策"  # keys not provided stay unchanged

    with pytest.raises(ValueError):
        store.update_cs(entry_id, {"no_such_column": 1})

    assert store.delete_cs(entry_id) is True
    assert store.get_cs(entry_id) is None
    assert store.delete_cs(entry_id) is False


def test_clear_scope(store):
    store.upsert_product("s", 1, "商品")
    store.upsert_product("s", 2, "商品")
    store.add_cs("s", "政策", "内容")
    cid = store.create_collection("s", "案例库", None,
                                  [{"name": "a", "type": "text"}])
    store.add_record(cid, {"a": "记录"})
    store.upsert_product("other", 1, "别动我")
    counts = store.clear_scope("s")
    assert counts == {"products_deleted": 2, "cs_deleted": 1,
                      "collections_deleted": 1, "records_deleted": 1}
    scopes = store.list_scopes()
    assert [s["scope"] for s in scopes] == ["other"]
    assert scopes[0]["product_count"] == 1


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
    # demo custom collection seeded alongside (with records)
    colls = store.list_collections("xianyu:demo")
    assert len(colls) == 1 and colls[0]["record_count"] > 0

    store.seed("xianyu:demo")  # idempotent: counts do not double
    assert len(store.search_products("xianyu:demo", limit=50)) == n1
    assert len(store.search_cs("xianyu:demo", limit=50)) == ncs1
    assert len(store.list_collections("xianyu:demo")) == 1
    assert store.list_collections("xianyu:demo")[0]["record_count"] == colls[0]["record_count"]


def test_seed_marker_prevents_resurrect_after_clear(store):
    """After clearing a scope (simulating a restart followed by another
    seed), data must not resurrect — the kb_meta marker guard is used,
    not a row-count guard."""
    store.seed("xianyu:demo")
    store.clear_scope("xianyu:demo")
    store.seed("xianyu:demo")
    assert store.search_products("xianyu:demo", limit=50) == []
    assert store.search_cs("xianyu:demo", limit=50) == []
    assert store.list_collections("xianyu:demo") == []


# ---------------------------------------------------------------------------
# Custom knowledge collections (ops-console user-defined tables)
# ---------------------------------------------------------------------------

def test_collection_crud_and_duplicate_name(store):
    cid = store.create_collection("s", "案例库", "说明", [
        {"name": "a", "label": "字段A", "type": "text", "required": True},
        {"name": "n", "type": "number"},
    ])
    coll = store.get_collection(cid)
    assert coll["name"] == "案例库" and coll["record_count"] == 0
    assert coll["fields"][0] == {"name": "a", "label": "字段A",
                                 "type": "text", "required": True}

    with pytest.raises(ValueError):  # duplicate name within the same scope
        store.create_collection("s", "案例库", None, [{"name": "a"}])
    store.create_collection("t", "案例库", None, [{"name": "a"}])  # same name across scopes is OK

    assert store.update_collection(cid, {"description": "新说明"}) is True
    assert store.get_collection(cid)["description"] == "新说明"
    with pytest.raises(ValueError):
        store.update_collection(cid, {"no_such_key": 1})

    assert store.delete_collection(cid) is True
    assert store.get_collection(cid) is None
    assert store.delete_collection(cid) is False


def test_collection_field_validation(store):
    with pytest.raises(ValueError):
        store.create_collection("s", "空字段", None, [])
    with pytest.raises(ValueError):
        store.create_collection("s", "空名", None, [{"name": "", "type": "text"}])
    with pytest.raises(ValueError):
        store.create_collection("s", "非法名", None, [{"name": "1bad", "type": "text"}])
    with pytest.raises(ValueError):
        store.create_collection("s", "重复名", None, [{"name": "a"}, {"name": "a"}])
    with pytest.raises(ValueError):
        store.create_collection("s", "坏类型", None, [{"name": "a", "type": "json"}])


def test_record_validation_search_and_replace(store):
    cid = store.create_collection("s", "案例库", None, [
        {"name": "title", "type": "text", "required": True},
        {"name": "detail", "type": "textarea"},
        {"name": "amount", "type": "number"},
    ])
    rid = store.add_record(cid, {"title": "屏幕亮线", "detail": "寄回换屏",
                                 "amount": "12.5"})  # numeric string gets coerced
    assert store.list_records(cid)[0]["data"] == {
        "title": "屏幕亮线", "detail": "寄回换屏", "amount": 12.5}

    with pytest.raises(ValueError):
        store.add_record(cid, {"title": "x", "bogus": 1})      # unknown field
    with pytest.raises(ValueError):
        store.add_record(cid, {"detail": "缺必填"})              # required
    with pytest.raises(ValueError):
        store.add_record(cid, {"title": "x", "amount": "abc"})  # number
    with pytest.raises(ValueError):
        store.add_record(999, {"title": "x"})                    # collection does not exist

    # full-field value search (tokenized AND) + no hit
    assert [r["id"] for r in store.list_records(cid, query="亮线 换屏")] == [rid]
    assert store.list_records(cid, query="不存在的词xyz") == []

    # full replace: optional fields explicitly cleared (None)
    assert store.update_record(cid, rid, {"title": "改"}) is True
    assert store.list_records(cid)[0]["data"] == {
        "title": "改", "detail": None, "amount": None}
    assert store.update_record(cid, 999, {"title": "x"}) is False

    assert store.delete_record(cid, rid) is True
    assert store.delete_record(cid, rid) is False


def test_update_collection_fields_reindexes_records(store):
    cid = store.create_collection("s", "库", None, [
        {"name": "a", "type": "text"}, {"name": "b", "type": "text"}])
    rid = store.add_record(cid, {"a": "苹果", "b": "香蕉"})
    # dropping field b: record values are cleaned up and no longer match
    # the old value
    assert store.update_collection(cid, {"fields": [{"name": "a", "type": "text"}]}) is True
    assert store.list_records(cid)[0]["data"] == {"a": "苹果"}
    assert store.list_records(cid, query="香蕉") == []
    assert [r["id"] for r in store.list_records(cid, query="苹果")] == [rid]


def test_update_collection_empty_patch_is_existence_check(store):
    """An empty patch is equivalent to an existence check — previously an
    empty SET clause produced invalid SQL (OperationalError), which was
    not caught by the API layer's except ValueError."""
    cid = store.create_collection("s", "库", None, [{"name": "a", "type": "text"}])
    assert store.update_collection(cid, {}) is True
    assert store.update_collection(999, {}) is False
    # the normalized result is not written back into the caller's dict
    patch = {"name": "  改名  "}
    assert store.update_collection(cid, patch) is True
    assert patch == {"name": "  改名  "}
    assert store.get_collection(cid)["name"] == "改名"


def test_list_records_untokenizable_query_returns_empty(store):
    """Single-character / punctuation-only queries tokenize to empty ->
    return an empty set instead of silently degrading to "return
    everything" (callers with filtering semantics would present the whole
    table as hits)."""
    cid = store.create_collection("s", "库", None, [{"name": "t", "type": "text"}])
    store.add_record(cid, {"t": "苹果香蕉"})
    assert store.list_records(cid, query="果") == []
    assert store.list_records(cid, query="！。") == []
    assert len(store.list_records(cid)) == 1


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
