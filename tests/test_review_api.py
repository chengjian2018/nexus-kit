"""Session-review console endpoints (ui.api §Session review) — a standalone
FastAPI app mounts the console router with ``_session_deps`` monkeypatched to
a tmp SessionStore + fake turn registry (offline: no host.main import, no LLM).
"""

import json
import sqlite3

import pytest
from async_utils import arun
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.api as console
from nexus.context import encode_tool_call_content
from nexus.engine.session import Session
from nexus.engine.store import SessionStore


class _FakeTurnRegistry:
    """Only the surface the review endpoints touch."""

    def __init__(self, running=()):
        self._running = set(running)

    def has_running(self, session_id):
        return session_id in self._running


def make_session(session_id, pattern_code="xianyu_agent"):
    session = Session(session_id=session_id, pattern_code=pattern_code)
    session.task_info = {"caller": "pytest"}
    session.cxt.metadata["request_id"] = f"req-{session_id}"
    return session


def _trace_row(kind, payload=None, turn_id="req-t1"):
    return {
        "session_id": "s-tl",
        "turn_id": turn_id,
        "kind": kind,
        "payload": payload or {"node_code": "n1", "data": {"step": 1}},
    }


@pytest.fixture()
def review_env(tmp_path, monkeypatch):
    db = str(tmp_path / "review.db")
    store = arun(SessionStore.create(db))
    monkeypatch.setattr(
        console, "_session_deps",
        lambda: (store, _FakeTurnRegistry({"s-live"})))
    app = FastAPI()
    app.include_router(console.router)
    yield TestClient(app), store, db
    arun(store.close())


def _ok_data(resp):
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0" and body["status"] is True
    return body["data"]


# ---------------------------------------------------------------------------
# GET /sessions — list
# ---------------------------------------------------------------------------

def test_sessions_list_filters_and_running_flag(review_env):
    client, store, _ = review_env
    for sid, pattern in (("alpha-1", "xianyu_agent"),
                         ("alpha-2", "other"),
                         ("beta-1", "xianyu_agent"),
                         ("s-live", "xianyu_agent")):
        session = make_session(sid, pattern_code=pattern)
        arun(store.create_session(session))
        store.attach(session)
        arun(session.cxt.add_message("user", f"q-{sid}", stage="chat"))

    data = _ok_data(client.get("/api/v1/console/sessions"))
    assert data["has_more"] is False
    assert {s["session_id"] for s in data["sessions"]} == {
        "alpha-1", "alpha-2", "beta-1", "s-live"}
    by_id = {s["session_id"]: s for s in data["sessions"]}
    assert by_id["s-live"]["turn_running"] is True
    assert by_id["alpha-1"]["turn_running"] is False
    assert by_id["alpha-1"]["message_count"] == 1

    # q fuzzy filtering (substring)
    data = _ok_data(client.get("/api/v1/console/sessions", params={"q": "alpha"}))
    assert {s["session_id"] for s in data["sessions"]} == {"alpha-1", "alpha-2"}

    # % / _ in q are treated literally (LIKE wildcards disabled)
    data = _ok_data(client.get("/api/v1/console/sessions", params={"q": "alpha%1"}))
    assert data["sessions"] == []

    # pattern_code filtering + has_more
    data = _ok_data(client.get("/api/v1/console/sessions",
                               params={"pattern_code": "other"}))
    assert [s["session_id"] for s in data["sessions"]] == ["alpha-2"]

    data = _ok_data(client.get("/api/v1/console/sessions",
                               params={"q": "alpha-", "limit": 1}))
    assert data["has_more"] is True
    assert len(data["sessions"]) == 1


def test_sessions_list_store_disabled(monkeypatch):
    monkeypatch.setattr(console, "_session_deps", lambda: (None, None))
    app = FastAPI()
    app.include_router(console.router)
    client = TestClient(app)
    resp = client.get("/api/v1/console/sessions")
    assert resp.status_code == 500
    assert resp.json()["code"] == "500"


# ---------------------------------------------------------------------------
# GET /sessions/{id} — detail
# ---------------------------------------------------------------------------

def test_session_detail_decodes_json_columns(review_env):
    client, store, _ = review_env
    session = make_session("s-det")
    session.cxt.graph_state = {"__paused_node__": "n1"}
    session.cxt.filled_slots = {"brand": "特斯拉"}
    arun(store.create_session(session))

    data = _ok_data(client.get("/api/v1/console/sessions/s-det"))
    s = data["session"]
    assert s["graph_state"] == {"__paused_node__": "n1"}
    assert s["filled_slots"] == {"brand": "特斯拉"}
    assert s["task_info"] == {"caller": "pytest"}
    assert s["request_id"] == "req-s-det"
    assert s["message_count"] == 0
    assert s["turn_running"] is False


def test_session_detail_bad_json_degrades_not_500(review_env):
    client, store, db = review_env
    arun(store.create_session(make_session("s-bad")))
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE sessions SET graph_state = '{broken' "
                     "WHERE session_id = 's-bad'")
    conn.close()

    data = _ok_data(client.get("/api/v1/console/sessions/s-bad"))
    assert data["session"]["graph_state"] == {}


def test_session_detail_404(review_env):
    client, _, _ = review_env
    resp = client.get("/api/v1/console/sessions/nope")
    assert resp.status_code == 404
    assert resp.json()["status"] is False


# ---------------------------------------------------------------------------
# GET /sessions/{id}/timeline — merged timeline
# ---------------------------------------------------------------------------

def _seed_tool_turn(store):
    """One dialogue turn: user → assistant(tool round) → tool → assistant final,
    plus node_start / tool_call / tool_result / graph_done trace events."""
    session = make_session("s-tl")
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "帮我查下库存", stage="chat"))
    arun(session.cxt.add_message(
        "assistant",
        encode_tool_call_content("查库存", [{
            "id": "call-1", "type": "function",
            "function": {"name": "inventory_query",
                         "arguments": "{\"sku\": \"A1\"}"},
        }]),
        stage="agent"))
    arun(session.cxt.add_message(
        "tool", '{"stock": 3}', stage="agent",
        metadata={"tool_call_id": "call-1", "tool_name": "inventory_query"}))
    arun(session.cxt.add_message("assistant", "库存还剩 3 件", stage="nlg"))
    arun(store.append_trace(session, _trace_row(
        kind="node_start", payload={"node_code": "af_root", "step": 0})))
    arun(store.append_trace(session, _trace_row(
        kind="tool_call",
        payload={"call_id": "call-1", "tool_name": "inventory_query",
                 "args": {"sku": "A1"}, "round_idx": 1})))
    arun(store.append_trace(session, _trace_row(
        kind="tool_result",
        payload={"call_id": "call-1", "tool_name": "inventory_query",
                 "result": {"stock": 3}, "round_idx": 1})))
    arun(store.append_trace(session, _trace_row(
        kind="graph_done", payload={"reason": "is_end", "step": 1})))


def test_timeline_merges_and_orders(review_env):
    client, store, _ = review_env
    _seed_tool_turn(store)

    data = _ok_data(client.get("/api/v1/console/sessions/s-tl/timeline"))
    items = data["items"]
    assert sum(1 for i in items if i["item_type"] == "message") == 4
    assert sum(1 for i in items if i["item_type"] == "trace") == 4
    # created_at is the primary order (ascending)
    created = [i["created_at"] for i in items]
    assert created == sorted(created)

    # the tool-round assistant message decodes into text + tool_calls
    tool_round = next(i for i in items if i.get("tool_calls"))
    assert tool_round["content"] == "查库存"
    assert tool_round["tool_calls"][0]["function"]["name"] == "inventory_query"

    # turns aggregation (the trace side keys on turn_id)
    assert data["turns"] == [{"turn_id": "req-t1", "count": 4}]
    assert data["trace_truncated"] is False
    assert data["session"]["session_id"] == "s-tl"
    assert data["session"]["message_count"] == 4


def test_timeline_flags_synthetic_and_rewritten(review_env):
    client, store, _ = review_env
    session = make_session("s-flag")
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "改写后的输入", stage="chat",
                                 metadata={"rewritten": True,
                                           "original_call": "原始输入"}))
    arun(session.cxt.add_message(
        "tool", "拦截回填", stage="agent",
        metadata={"tool_call_id": "c9", "tool_name": "t", "synthetic": True}))
    arun(session.cxt.add_message("assistant", "正常", stage="nlg"))

    data = _ok_data(client.get("/api/v1/console/sessions/s-flag/timeline"))
    flags = {i["content"]: i["flags"] for i in data["items"]
             if i["item_type"] == "message"}
    assert flags["改写后的输入"] == {"rewritten": True}
    assert flags["拦截回填"] == {"synthetic": True}
    assert flags["正常"] == {}


def test_timeline_trace_truncated_flag(review_env, monkeypatch):
    client, store, _ = review_env
    monkeypatch.setattr(console, "_TRACE_LIMIT", 3)
    session = make_session("s-many")
    arun(store.create_session(session))
    store.attach(session)
    for i in range(4):
        arun(store.append_trace(session, _trace_row(
            kind=f"ev{i}", payload={"i": i})))

    data = _ok_data(client.get("/api/v1/console/sessions/s-many/timeline"))
    assert data["trace_truncated"] is True
    assert sum(1 for i in data["items"] if i["item_type"] == "trace") == 3


def test_timeline_404_and_disabled(review_env, monkeypatch):
    client, _, _ = review_env
    resp = client.get("/api/v1/console/sessions/nope/timeline")
    assert resp.status_code == 404
    assert resp.json()["status"] is False

    monkeypatch.setattr(console, "_session_deps", lambda: (None, None))
    app = FastAPI()
    app.include_router(console.router)
    disabled = TestClient(app)
    resp = disabled.get("/api/v1/console/sessions/x/timeline")
    assert resp.status_code == 500
    assert resp.json()["code"] == "500"
