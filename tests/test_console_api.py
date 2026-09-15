"""Ops-console API tests (ui.api P0 surface) — a standalone FastAPI app mounts the router, the knowledge base is stubbed onto a tmp DB, and pattern views use the real registry (offline, no LLM)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.api as console
from atoms.knowledge.store import KnowledgeStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = KnowledgeStore(str(tmp_path / "console-kb.db"))
    monkeypatch.setattr(console, "get_knowledge_store", lambda: store)
    app = FastAPI()
    app.include_router(console.router)
    yield TestClient(app)
    store.close()


def _ok_data(resp):
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0" and body["status"] is True
    return body["data"]


# ---------------------------------------------------------------------------
# Pattern read-only views
# ---------------------------------------------------------------------------

def test_patterns_list_covers_registered(client):
    patterns = _ok_data(client.get("/api/v1/console/patterns"))["patterns"]
    codes = {p["code"] for p in patterns}
    assert {"xianyu_agent", "customer_agent", "install_booking_agent",
            "repair_booking_agent", "deep_research"} <= codes
    install = next(p for p in patterns if p["code"] == "install_booking_agent")
    assert install["pattern_type"] == "fsm"
    assert install["node_count"] >= 14
    assert install["managed"] == "code"


def test_pattern_detail_yaml_mermaid_tree(client):
    data = _ok_data(client.get("/api/v1/console/patterns/xianyu_agent"))
    assert "code: xianyu_agent" in data["yaml"]
    assert data["mermaid"].lstrip().startswith("flowchart")
    tree = data["tree"]
    assert tree["code"] == "xianyu_agent"
    assert tree["pattern_type"] == "agent"
    assert any(n["code"] == "xy_route_root" for n in tree["nodes"])


def test_pattern_detail_404(client):
    resp = client.get("/api/v1/console/patterns/no_such_pattern")
    assert resp.status_code == 404
    assert resp.json()["status"] is False


# ---------------------------------------------------------------------------
# Catalog (data source for the reference-code dropdowns)
# ---------------------------------------------------------------------------

def test_catalog_stage_and_executor(client):
    stages = _ok_data(client.get("/api/v1/console/catalog/stage"))["codes"]
    assert "fsm_unified" in stages and "nlg_pass_through" in stages
    executors = _ok_data(client.get("/api/v1/console/catalog/executor"))["codes"]
    assert {"default_loop", "default_fsm"} <= set(executors)
    assert "default_route" not in executors  # ROUTE executor 已删除
    assert "xianyu_router" in executors     # app 自有节点执行器可见


def test_catalog_unknown_kind_404(client):
    resp = client.get("/api/v1/console/catalog/nope")
    assert resp.status_code == 404


def test_catalog_tools_lists_knowledge_toolset(client):
    toolsets = _ok_data(client.get("/api/v1/console/catalog/tools"))["toolsets"]
    assert "knowledge" in toolsets
    assert "search_product_knowledge" in toolsets["knowledge"]["tools"]


# ---------------------------------------------------------------------------
# Knowledge base CRUD / scopes / search test
# ---------------------------------------------------------------------------

def test_product_crud_roundtrip(client):
    # create (upsert)
    _ok_data(client.post("/api/v1/console/knowledge/products", json={
        "scope": "xianyu:t1", "goods_id": 1, "goods_name": "iPhone 13",
        "price": "2699", "extracted_content": "# 95新 国行"}))
    rows = _ok_data(client.get("/api/v1/console/knowledge/products",
                               params={"scope": "xianyu:t1"}))["rows"]
    assert len(rows) == 1 and rows[0]["goods_name"] == "iPhone 13"

    # patch: absent fields untouched, explicit null clears
    _ok_data(client.put("/api/v1/console/knowledge/products",
                        params={"scope": "xianyu:t1", "goods_id": 1},
                        json={"price": "2599"}))
    _ok_data(client.put("/api/v1/console/knowledge/products",
                        params={"scope": "xianyu:t1", "goods_id": 1},
                        json={"extracted_content": None}))
    rows = _ok_data(client.get("/api/v1/console/knowledge/products",
                               params={"scope": "xianyu:t1"}))["rows"]
    assert rows[0]["price"] == "2599"
    assert rows[0]["extracted_content"] is None

    # delete
    _ok_data(client.delete("/api/v1/console/knowledge/products",
                           params={"scope": "xianyu:t1", "goods_id": 1}))
    resp = client.delete("/api/v1/console/knowledge/products",
                         params={"scope": "xianyu:t1", "goods_id": 1})
    assert resp.status_code == 404


def test_cs_entries_crud_and_toggle(client):
    entry_id = _ok_data(client.post("/api/v1/console/knowledge/cs-entries", json={
        "scope": "xianyu:t1", "title": "退货政策",
        "content": "7 天无理由退货", "tags": "售后"}))["id"]

    rows = _ok_data(client.get("/api/v1/console/knowledge/cs-entries",
                               params={"scope": "xianyu:t1"}))["rows"]
    assert len(rows) == 1

    # after disabling: still visible to the admin listing, invisible to retrieval (verified by search-test)
    _ok_data(client.put(f"/api/v1/console/knowledge/cs-entries/{entry_id}",
                        json={"enabled": False}))
    rows = _ok_data(client.get("/api/v1/console/knowledge/cs-entries",
                               params={"scope": "xianyu:t1"}))["rows"]
    assert rows[0]["enabled"] == 0
    hits = _ok_data(client.post("/api/v1/console/knowledge/search-test", json={
        "scope": "xianyu:t1", "query": "退货"}))
    assert hits["cs_entries"] == []

    # explicitly nulling the title is rejected
    resp = client.put(f"/api/v1/console/knowledge/cs-entries/{entry_id}",
                      json={"title": None})
    assert resp.status_code == 400

    _ok_data(client.delete(f"/api/v1/console/knowledge/cs-entries/{entry_id}"))
    resp = client.delete(f"/api/v1/console/knowledge/cs-entries/{entry_id}")
    assert resp.status_code == 404


def test_scopes_listing_and_clear(client):
    client.post("/api/v1/console/knowledge/products", json={
        "scope": "xianyu:a", "goods_id": 1, "goods_name": "商品"})
    client.post("/api/v1/console/knowledge/cs-entries", json={
        "scope": "xianyu:a", "title": "政策", "content": "内容"})
    _ok_data(client.post("/api/v1/console/knowledge/collections", json={
        "scope": "xianyu:a", "name": "案例库",
        "fields": [{"name": "case_no", "type": "text", "required": True}]}))
    scopes = _ok_data(client.get("/api/v1/console/knowledge/scopes"))["scopes"]
    by_scope = {s["scope"]: s for s in scopes}
    assert by_scope["xianyu:a"]["product_count"] == 1
    assert by_scope["xianyu:a"]["cs_count"] == 1
    assert by_scope["xianyu:a"]["collection_count"] == 1

    data = _ok_data(client.post("/api/v1/console/knowledge/clear-scope",
                                json={"scope": "xianyu:a"}))
    assert data == {"products_deleted": 1, "cs_deleted": 1,
                    "collections_deleted": 1, "records_deleted": 0}


def test_custom_collections_and_records_crud(client):
    base = "/api/v1/console/knowledge/collections"
    cid = _ok_data(client.post(base, json={
        "scope": "xianyu:c1", "name": "售后案例库", "description": "案例",
        "fields": [
            {"name": "case_no", "label": "案例编号", "type": "text", "required": True},
            {"name": "refund", "label": "退款", "type": "number"},
        ]}))["id"]

    # 同 scope 重名 -> 400（store 层校验）
    resp = client.post(base, json={
        "scope": "xianyu:c1", "name": "售后案例库",
        "fields": [{"name": "a"}]})
    assert resp.status_code == 400
    # 坏字段名 -> 422（pydantic pattern）
    resp = client.post(base, json={
        "scope": "xianyu:c1", "name": "库2", "fields": [{"name": "1bad"}]})
    assert resp.status_code == 422

    colls = _ok_data(client.get(base, params={"scope": "xianyu:c1"}))["collections"]
    assert [c["name"] for c in colls] == ["售后案例库"]
    assert colls[0]["fields"][0]["label"] == "案例编号"

    # 记录：新增（数字字符串 coercion）/ 列表 / 检索
    rid = _ok_data(client.post(f"{base}/{cid}/records",
                               json={"data": {"case_no": "A-1", "refund": "99"}}))["id"]
    rows = _ok_data(client.get(f"{base}/{cid}/records"))["rows"]
    assert rows[0]["data"] == {"case_no": "A-1", "refund": 99}

    resp = client.post(f"{base}/{cid}/records",
                       json={"data": {"case_no": "A-2", "bogus": 1}})
    assert resp.status_code == 400  # 未知字段
    resp = client.post(f"{base}/{cid}/records", json={"data": {"refund": 1}})
    assert resp.status_code == 400  # 缺必填
    resp = client.post(f"{base}/{cid}/records",
                       json={"data": {"case_no": "A-3", "refund": "abc"}})
    assert resp.status_code == 400  # number 非法

    hits = _ok_data(client.get(f"{base}/{cid}/records",
                               params={"query": "A-1"}))["rows"]
    assert [r["id"] for r in hits] == [rid]

    # 全量替换记录（可选字段显式清空）
    _ok_data(client.put(f"{base}/{cid}/records/{rid}",
                        json={"data": {"case_no": "A-1x", "refund": None}}))
    rows = _ok_data(client.get(f"{base}/{cid}/records"))["rows"]
    assert rows[0]["data"] == {"case_no": "A-1x", "refund": None}

    # 改字段定义：被删字段的值从记录里清理
    _ok_data(client.put(f"{base}/{cid}", json={
        "fields": [{"name": "case_no", "type": "text", "required": True}]}))
    rows = _ok_data(client.get(f"{base}/{cid}/records"))["rows"]
    assert rows[0]["data"] == {"case_no": "A-1x"}

    # 404 路径
    assert client.get(f"{base}/999").status_code == 404
    assert client.delete(f"{base}/999").status_code == 404
    assert client.get(f"{base}/999/records").status_code == 404
    assert client.delete(f"{base}/{cid}/records/999").status_code == 404

    # 删库（级联删记录）
    _ok_data(client.delete(f"{base}/{cid}"))
    assert client.get(f"{base}/{cid}").status_code == 404
    assert client.get(f"{base}/{cid}/records").status_code == 404


def test_collections_bad_scope_rejected(client):
    resp = client.get("/api/v1/console/knowledge/collections",
                      params={"scope": "no-colon"})
    assert resp.status_code == 400
    body = resp.json()
    assert "{channel}:{account_id}" in body["message"]


def test_search_test_tokens_and_match_explanation(client):
    client.post("/api/v1/console/knowledge/products", json={
        "scope": "xianyu:t2", "goods_id": 1, "goods_name": "Kindle 阅读器",
        "extracted_content": "墨水屏"})
    client.post("/api/v1/console/knowledge/cs-entries", json={
        "scope": "xianyu:t2", "title": "发货时效", "content": "48 小时内发货"})
    data = _ok_data(client.post("/api/v1/console/knowledge/search-test", json={
        "scope": "xianyu:t2", "query": "阅读器 墨水屏"}))
    # jieba search mode splits sub-words (e.g. 墨水屏 -> 墨水/水屏) — assertions use containment
    assert "阅读器" in data["tokens"]
    assert any("墨水" in t for t in data["tokens"])
    assert len(data["products"]) == 1
    assert "阅读器" in data["products"][0]["_match"]["goods_name"]
    assert data["products"][0]["_match"]["extracted_content"]  # hit via the 墨水* sub-word

    goods_mode = _ok_data(client.post("/api/v1/console/knowledge/search-test",
                                      json={"scope": "xianyu:t2", "query": "",
                                            "goods_id": 1}))
    assert len(goods_mode["products"]) == 1 and goods_mode["tokens"] == []


def test_bad_scope_rejected(client):
    resp = client.get("/api/v1/console/knowledge/products",
                      params={"scope": "no-colon"})
    assert resp.status_code == 400
    body = resp.json()
    assert "{channel}:{account_id}" in body["message"]


# ---------------------------------------------------------------------------
# RAG retrieval config endpoints (knowledge base and config file both stubbed; registry restored after use)
# ---------------------------------------------------------------------------

@pytest.fixture()
def rag_env(client, tmp_path, monkeypatch):
    """Standalone config file + stubbed knowledge base + builtin assembly restored at teardown."""
    import atoms.stages.rag_config as rc
    from atoms.knowledge.store import KnowledgeStore

    monkeypatch.setenv("NEXUS_RAG_CONFIG", str(tmp_path / "rag.yaml"))
    store = KnowledgeStore(str(tmp_path / "rag-kb.db"))
    store.add_cs("s:t", "退货政策", "自签收起 7 天内支持无理由退货", tags="售后,退货")
    monkeypatch.setattr(rc, "get_knowledge_store", lambda: store)
    yield store
    store.close()
    rc.reset_rag_config()


def test_rag_config_get_default_then_file(client, rag_env):
    data = _ok_data(client.get("/api/v1/console/rag/config"))
    assert data["source"] == "builtin"
    assert data["config"]["recall_paths"] == []
    assert data["supported"]["recall_path_types"] == ["kb_cs", "kb_products"]


def test_rag_config_put_validate_apply_reset(client, rag_env):
    # valid config: saved + applied
    resp = client.put("/api/v1/console/rag/config", json={"config": {
        "recall_paths": [{"type": "kb_cs", "scope": "s:t", "top_k": 5}],
        "rule": {"t_high": 0.8},
    }})
    assert resp.status_code == 200
    assert resp.json()["data"]["applied_codes"] == [
        "rag_clarify", "clarify_default", "builtin:clarify"]

    data = _ok_data(client.get("/api/v1/console/rag/config"))
    assert data["source"] == "file"
    assert data["config"]["rule"]["t_high"] == 0.8
    assert data["config"]["fusion"] == {"type": "weighted"}  # defaults filled in on persist

    # invalid config: collect-all-errors 400
    resp = client.put("/api/v1/console/rag/config", json={"config": {
        "recall_paths": [{"type": "es"}], "rule": {"t_low": 0.9, "t_high": 0.1},
    }})
    assert resp.status_code == 400
    msg = resp.json()["message"]
    assert "共 2 项" in msg and "v1 未开放" in msg and "t_low 必须" in msg

    # reset: delete the file + restore the builtin config
    data = _ok_data(client.post("/api/v1/console/rag/reset"))
    assert data["file_deleted"] is True
    data = _ok_data(client.get("/api/v1/console/rag/config"))
    assert data["source"] == "builtin"


def test_rag_test_run_with_inline_config(client, rag_env):
    resp = client.post("/api/v1/console/rag/test-run", json={
        "config": {"recall_paths": [{"type": "kb_cs", "scope": "s:t"}]},
        "query": "退货政策 怎么算", "topic": "退货",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["mode"] == "kb" and data["top_score"] >= 0.6
    assert data["per_path"][0]["name"] == "kb_cs"

    resp = client.post("/api/v1/console/rag/test-run", json={
        "query": "量子涨落", "config": {"recall_paths": []}})
    assert resp.json()["data"]["mode"] == "fallback"


def test_rag_test_run_bad_config_400(client, rag_env):
    resp = client.post("/api/v1/console/rag/test-run", json={
        "query": "x", "config": {"fusion": {"type": "bogus"}}})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# host.main full-chain mounting (API surface exercised through the real app; no startup events, no DB/MCP dependencies)
# ---------------------------------------------------------------------------

def test_host_app_mounts_console():
    import host.main as main

    client = TestClient(main.app)
    page = client.get("/console/")
    assert page.status_code == 200
    assert "nexus-console" in page.text
    body = client.get("/api/v1/console/patterns").json()
    assert body["code"] == "0" and body["status"] is True
