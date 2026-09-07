"""Session persistence API integration tests -- launch/chat persist, audit endpoints, restart restore.

TestClient is used without ``with`` (startup not triggered); the store is injected
into a tmp DB explicitly by fixture; registry_guard clears/restores the global
session table to prevent pollution.
"""

import pytest
from fastapi.testclient import TestClient

from fake_provider import fake_llm_config, register_fake_provider
from nexus.engine.store import SessionStore


@pytest.fixture(scope="module")
def client():
    import host.main as main  # noqa: F401 -- importing it completes discover_builtin_tools/patterns
    return TestClient(main.app)


@pytest.fixture()
def store(tmp_path):
    """Inject a store backed by a tmp DB into main; restore and close afterwards."""
    import host.main as main

    s = SessionStore(str(tmp_path / "audit.db"))
    prev = main.store
    main.store = s
    yield s
    main.store = prev
    s.close()


@pytest.fixture()
def registry_guard():
    """Clear the global session registry and restore the snapshot after the test (same as the governance tests)."""
    import host.main as main

    with main.governor.lock:
        snap_sessions = dict(main.governor.sessions)
        snap_ts = dict(main.governor.last_active)
        main.governor.sessions.clear()
        main.governor.last_active.clear()
    yield
    with main.governor.lock:
        main.governor.sessions.clear()
        main.governor.sessions.update(snap_sessions)
        main.governor.last_active.clear()
        main.governor.last_active.update(snap_ts)


def launch(client, session_id, pattern_code="xianyu_agent"):
    resp = client.post(
        "/api/v1/launch",
        json={
            "request_id": f"req-{session_id}",
            "session_id": session_id,
            "pattern_code": pattern_code,
            "task_info": {"caller": "pytest"},
        },
    )
    assert resp.status_code == 200
    return resp.json()


def chat(client, session_id, query):
    resp = client.post(
        "/api/v1/chat",
        json={
            "request_id": f"req-chat-{session_id}",
            "session_id": session_id,
            "query": query,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _use_fake_llm(session_id):
    import host.main as main

    main.governor.sessions[session_id].cxt.metadata["llm_override"] = fake_llm_config()


def test_launch_chat_persisted(client, store, registry_guard):
    """launch -> chat: sessions row + incremental messages persisted (first row user, last row assistant)."""
    register_fake_provider()
    assert launch(client, "audit-1")["status"] is True
    _use_fake_llm("audit-1")

    body = chat(client, "audit-1", "你好")
    assert body["status"] is True, body["message"]

    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["audit-1"]
    assert rows[0]["pattern_code"] == "xianyu_agent"
    assert rows[0]["message_count"] >= 2

    msgs = store.get_messages("audit-1")
    assert msgs[0]["role"] == "user" and msgs[0]["stage"] == "chat"
    assert msgs[-1]["role"] == "assistant"


def test_chat_turn_incremental_append(client, store, registry_guard):
    """The second turn only appends second-turn messages; history stays continuous on the DB side."""
    register_fake_provider()
    launch(client, "audit-2")
    _use_fake_llm("audit-2")
    chat(client, "audit-2", "你好")
    first_count = len(store.get_messages("audit-2"))

    chat(client, "audit-2", "我想买车")
    second_count = len(store.get_messages("audit-2"))
    assert second_count > first_count
    msgs = store.get_messages("audit-2")
    assert msgs[first_count]["role"] == "user"  # a new round starts with a user message


def test_store_failure_does_not_block(client, store, registry_guard, monkeypatch):
    """DB write failure: only logged; the chat response is unaffected.

    Under per-message write-through semantics, a failed append_message is swallowed
    by the sink; a failed end-of-turn save_snapshot likewise does not block.
    """
    register_fake_provider()
    launch(client, "audit-degraded")
    _use_fake_llm("audit-degraded")

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "append_message", boom)
    monkeypatch.setattr(store, "save_snapshot", boom)
    body = chat(client, "audit-degraded", "你好")
    assert body["status"] is True, body["message"]


def test_list_sessions_endpoint(client, store, registry_guard):
    """GET /sessions: default list + pattern_code filter + pagination params."""
    register_fake_provider()
    launch(client, "api-a")
    launch(client, "api-a2")
    launch(client, "api-b", pattern_code="not_registered_is_rejected")

    resp = client.get("/api/v1/sessions")
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == "0"
    ids = [s["session_id"] for s in body["data"]["sessions"]]
    assert "api-a" in ids and "api-b" not in ids  # launch with an unregistered pattern was rejected

    resp = client.get("/api/v1/sessions", params={"pattern_code": "xianyu_agent", "limit": 1})
    sessions = resp.json()["data"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["pattern_code"] == "xianyu_agent"


def test_messages_endpoint_and_404(client, store, registry_guard):
    """GET /sessions/{id}/messages: full-trail messages; a missing session returns a 404 envelope."""
    register_fake_provider()
    launch(client, "api-msg")
    _use_fake_llm("api-msg")
    chat(client, "api-msg", "你好")

    resp = client.get("/api/v1/sessions/api-msg/messages")
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == "0"
    msgs = body["data"]["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[-1]["role"] == "assistant"
    assert all("stage" in m and "created_at" in m for m in msgs)

    resp = client.get("/api/v1/sessions/no-such/messages")
    body = resp.json()
    assert resp.status_code == 200  # the business code travels inside the envelope
    assert body["code"] == "404"
    assert body["status"] is False


def test_audit_endpoints_degraded_when_no_store(client, registry_guard):
    """With the store disabled, audit endpoints return a 500 envelope (degradation stays visible)."""
    import host.main as main

    prev = main.store
    main.store = None
    try:
        assert client.get("/api/v1/sessions").json()["code"] == "500"
        assert client.get("/api/v1/sessions/x/messages").json()["code"] == "500"
    finally:
        main.store = prev


def test_launch_persist_failure_degrades(client, store, registry_guard, monkeypatch):
    """launch persist failure: the launch response is unaffected and the session stays usable in memory."""
    import host.main as main

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "create_session", boom)
    body = launch(client, "audit-degraded2")
    assert body["status"] is True
    assert "audit-degraded2" in main.governor.sessions


def test_restart_recovery_restores_and_continues(client, store, registry_guard):
    """Simulate restart: clear memory -> _restore_sessions -> sessions restored and able to continue the dialogue, DB trail continuous."""
    import host.main as main

    register_fake_provider()
    launch(client, "rs-1")
    _use_fake_llm("rs-1")
    chat(client, "rs-1", "你好")
    count_before = len(store.get_messages("rs-1"))

    # Simulate restart: wipe in-memory state
    with main.governor.lock:
        main.governor.sessions.clear()
        main.governor.last_active.clear()

    restored = main._restore_sessions()
    assert restored >= 1
    session = main.governor.sessions["rs-1"]
    assert session.pattern is not None  # pattern re-resolved from the registry
    assert session.cxt.node_map and session.cxt.module_map  # pipeline maps re-injected
    assert len(session.cxt.history) >= 2  # history restored from the DB
    assert "rs-1" in main.governor.last_active  # last-active time converted and registered

    # Continue the dialogue after restore: new messages append after the restored history, keeping the DB trail continuous
    session.cxt.metadata["llm_override"] = fake_llm_config()
    body = chat(client, "rs-1", "我想买车")
    assert body["status"] is True, body["message"]

    msgs = store.get_messages("rs-1")
    assert len(msgs) == count_before + 2  # user + assistant
    assert msgs[count_before]["role"] == "user"


def test_mid_turn_failure_user_row_already_persisted(
        client, store, registry_guard, monkeypatch):
    """Write-through timing: the LLM crashes mid-turn, yet the user row is already persisted (not dependent on end of turn)."""
    register_fake_provider()
    launch(client, "crash-mid")
    _use_fake_llm("crash-mid")

    from fake_provider import FakeProvider

    def llm_boom(self, *args, **kwargs):
        raise RuntimeError("llm down mid-turn")

    monkeypatch.setattr(FakeProvider, "_chat_completion_impl", llm_boom)
    body = chat(client, "crash-mid", "你好")
    # The chat layer swallows the exception and turns it into error text (HTTP 200 + status True); the failure path is persisted too
    assert body["status"] is True
    assert "对话处理异常" in body["data"]["response"]

    msgs = store.get_messages("crash-mid")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]  # user persisted immediately via the sink; the error reply is written by end_turn


def test_restore_skips_unregistered_pattern(client, store, registry_guard):
    """Sessions with an unregistered pattern_code are skipped during restore (no raise, never loaded into memory)."""
    import host.main as main
    from nexus.engine.session import Session

    store.create_session(Session(session_id="ghost", pattern_code="no_such_pattern"))
    restored = main._restore_sessions()
    assert "ghost" not in main.governor.sessions
    assert restored == 0


def test_init_store_degrades_on_failure(monkeypatch):
    """Config/DB init failure -> store=None degradation, no exception raised."""
    import host.main as main

    prev = main.store

    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(main, "get_session_db_path", boom)
    main._init_store()
    assert main.store is None
    main.store = prev


def test_restore_failure_does_not_block(client, store, registry_guard, monkeypatch):
    """Restore exceptions do not block: a store-level error returns 0 and a per-session error skips that session; neither propagates outward."""
    import host.main as main
    from nexus.engine.session import Session

    # 1) store-level failure (e.g. a DB read error): no raise, returns 0
    def store_boom(ttl):
        raise RuntimeError("db read down")

    monkeypatch.setattr(store, "load_active_sessions", store_boom)
    assert main._restore_sessions() == 0

    # 2) single-session failure (e.g. any exception triggered by a corrupted row): skip that session, do not block the rest
    good = Session(session_id="rs-good", pattern_code="xianyu_agent")
    bad = Session(session_id="rs-bad", pattern_code="xianyu_agent")

    def fake_load(ttl):
        return [(good, main.time.time()), (bad, main.time.time())]

    monkeypatch.setattr(store, "load_active_sessions", fake_load)

    real_get = main.pattern_registry.get
    calls = []

    def get_or_raise(code):
        calls.append(code)
        if len(calls) == 2:
            raise RuntimeError("corrupted row")
        return real_get(code)

    monkeypatch.setattr(main.pattern_registry, "get", get_or_raise)

    restored = main._restore_sessions()
    assert restored == 1
    assert "rs-good" in main.governor.sessions
    assert "rs-bad" not in main.governor.sessions
