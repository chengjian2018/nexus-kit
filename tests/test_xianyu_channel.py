"""Xianyu channel adapter tests -- unit layer (fake engine ops injected) + main.app integration layer.

The unit layer builds its own FastAPI app with fake launch/get/run injected, covering
session_id derivation, auto launch, stale message swallowing, token checks and error
response contracts; the integration layer runs main.app end to end (session governance
+ store persistence) with main.chat stubbed to stay offline.
"""

import os
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.engine.store import SessionStore
from nexus.channels.base import EngineOps
from nexus.channels.webhooks import build_channel_router
from apps.xianyu_agent.channel import XianyuChannel


# ============================================================================
# Unit layer: router test rig with fake engine ops injected
# ============================================================================

class ChannelHarness:
    """Fake engine ops + a standalone FastAPI app, recording calls for assertions.

    pattern/token are injected via environment variables (the generic handler reads
    them per request); the post() helper sets them for the request and restores
    them afterwards.
    """

    def __init__(self, pattern_code="demo_pattern", token=None):
        self.pattern_code = pattern_code
        self.token = token
        self._env = {}
        if pattern_code is not None:
            self._env["XIANYU_CHANNEL_PATTERN"] = pattern_code
        if token is not None:
            self._env["XIANYU_CHANNEL_TOKEN"] = token
        self.sessions = {}
        self.launch_calls = []
        self.run_calls = []
        self.launch_error = None  # (code, message): simulates launch failure
        self.run_error = None

        def launch_session(pattern_code, session_id, task_info, request_id, exist_ok=False):
            self.launch_calls.append(
                {
                    "pattern_code": pattern_code,
                    "session_id": session_id,
                    "task_info": task_info,
                    "exist_ok": exist_ok,
                }
            )
            if self.launch_error is not None:
                return None, self.launch_error[0], self.launch_error[1]
            if session_id in self.sessions:
                return self.sessions[session_id], "0", "已存在"
            sess = SimpleNamespace(session_id=session_id)
            self.sessions[session_id] = sess
            return sess, "0", "ok"

        def get_session(session_id):
            return self.sessions.get(session_id)

        def run_chat_turn(session, query):
            self.run_calls.append((session.session_id, query))
            if self.run_error is not None:
                return None, self.run_error
            return f"echo:{query}", None

        app = FastAPI()
        app.include_router(build_channel_router(
            XianyuChannel(),
            EngineOps(
                get_session=get_session,
                launch_session=launch_session,
                run_chat_turn=run_chat_turn,
            ),
        ))
        self.client = TestClient(app)

    def post(self, path, json=None, params=None):
        """POST with env injection: sets env vars for the request, restores them afterwards."""
        saved = {k: os.environ.get(k) for k in self._env}
        # Defense: when token is None, clear any token the outer environment may have exported, to avoid polluting the 403 check
        token_saved = os.environ.pop("XIANYU_CHANNEL_TOKEN", None)
        try:
            os.environ.update(self._env)
            return self.client.post(path, json=json, params=params)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if token_saved is not None:
                os.environ["XIANYU_CHANNEL_TOKEN"] = token_saved


def inbound(**overrides):
    """Standard inbound payload; fields can be overridden."""
    payload = {
        "account_id": "acc1",
        "message": "你好",
        "chat_id": "chat1",
        "item_id": "item1",
        "send_user_id": "buyer1",
        "send_user_name": "买家小张",
    }
    payload.update(overrides)
    return payload


def test_first_message_auto_launches():
    """First message auto-launches: session_id derivation, task_info extraction, exist_ok semantics."""
    h = ChannelHarness()
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "echo:你好"
    assert body["session_id"] == "xianyu:acc1:chat1"

    assert len(h.launch_calls) == 1
    call = h.launch_calls[0]
    assert call["session_id"] == "xianyu:acc1:chat1"
    assert call["pattern_code"] == "demo_pattern"
    assert call["exist_ok"] is True
    assert call["task_info"] == {
        "channel": "xianyu",
        "account_id": "acc1",
        "item_id": "item1",
        "buyer_user_id": "buyer1",
        "buyer_user_name": "买家小张",
    }


def test_second_message_reuses_session():
    """A second message in the same session reuses the existing session, no new launch."""
    h = ChannelHarness()
    h.post("/api/v1/channel/xianyu", json=inbound())
    h.post("/api/v1/channel/xianyu", json=inbound(message="多少钱"))

    assert len(h.launch_calls) == 1
    assert h.run_calls == [
        ("xianyu:acc1:chat1", "你好"),
        ("xianyu:acc1:chat1", "多少钱"),
    ]


def test_unknown_session_without_pattern_503():
    """Session missing and pattern unset: 503, engine untouched."""
    h = ChannelHarness(pattern_code=None)
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 503
    assert h.launch_calls == [] and h.run_calls == []


def test_launch_failure_maps_500():
    """Auto launch failure (e.g. unregistered pattern) maps to 500."""
    h = ChannelHarness()
    h.launch_error = ("404", "pattern_code 'x' 未注册")
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 500
    assert "未注册" in resp.json()["detail"]


def test_run_error_maps_500():
    """Engine turn exception maps to 500, without a reply key (the peer will not send anything)."""
    h = ChannelHarness()
    h.run_error = RuntimeError("LLM 超时")
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 500
    assert "LLM 超时" in resp.json()["detail"]


def test_stale_message_swallowed():
    """Stale message (reconnect replay) swallowed: 200 + empty reply, no launch, no dialogue."""
    h = ChannelHarness()
    stale_ms = str(int((time.time() - 600) * 1000))
    resp = h.post(
        "/api/v1/channel/xianyu", json=inbound(msg_time=stale_ms)
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == ""
    assert h.launch_calls == [] and h.run_calls == []


def test_unparseable_msg_time_passes_through():
    """Unparseable msg_time is not filtered; dialogue proceeds normally."""
    h = ChannelHarness()
    resp = h.post(
        "/api/v1/channel/xianyu", json=inbound(msg_time="不是时间")
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:你好"


def test_token_rejects_wrong_and_accepts_right():
    """With a token configured: wrong token 403, right token passes."""
    h = ChannelHarness(token="s3cret")
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 403

    resp = h.post(
        "/api/v1/channel/xianyu", json=inbound(), params={"token": "s3cret"}
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:你好"


def test_missing_required_field_422():
    """Missing required field: 422 (not 200, so the peer sends nothing)."""
    h = ChannelHarness()
    payload = inbound()
    del payload["message"]
    resp = h.post("/api/v1/channel/xianyu", json=payload)
    assert resp.status_code == 422


def test_success_body_has_no_fallback_keys():
    """The success body must not carry data/content/message keys: when reply is empty the peer
    falls back to those three keys in order; leaking them would send debug info to the buyer."""
    h = ChannelHarness()
    resp = h.post("/api/v1/channel/xianyu", json=inbound())
    assert set(resp.json().keys()) <= {"reply", "session_id"}


# ============================================================================
# Integration layer: main.app end to end (session governance + store persistence; main.chat stubbed to stay offline)
# ============================================================================

@pytest.fixture(scope="module")
def client():
    import host.main as main  # noqa: F401 -- importing completes discovery + channel wiring
    return TestClient(main.app)


@pytest.fixture()
def store(tmp_path):
    """Inject a tmp-DB store into main; restore and close afterwards."""
    import host.main as main

    s = SessionStore(str(tmp_path / "channel.db"))
    prev = main.store
    main.store = s
    yield s
    main.store = prev
    s.close()


@pytest.fixture()
def registry_guard():
    """Clear the global session registry; restore the snapshot after the test (same as the persistence tests)."""
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


@pytest.fixture()
def fake_chat(monkeypatch):
    """Stub main.chat with a fixed reply that still writes history like the real behavior, keeping the tests offline;
    returns the (session_id, query) call log."""
    import host.main as main

    calls = []

    def _chat(query, session_id, all_sessions, store=None):
        calls.append((session_id, query))
        session = all_sessions[session_id]
        session.cxt.add_message("user", query, stage="chat")
        reply = f"auto:{query}"
        session.cxt.add_message("assistant", reply, stage="chat")
        return reply

    monkeypatch.setattr(main, "chat", _chat)
    return calls


def test_channel_end_to_end(client, store, registry_guard, fake_chat, monkeypatch):
    """First message: auto launch (real governance + persistence) -> engine dialogue -> reply contract."""
    monkeypatch.setenv("XIANYU_CHANNEL_PATTERN", "xianyu_agent")
    resp = client.post("/api/v1/channel/xianyu", json=inbound())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "auto:你好"
    assert body["session_id"] == "xianyu:acc1:chat1"

    import host.main as main

    session = main.governor.sessions["xianyu:acc1:chat1"]
    assert session.pattern_code == "xianyu_agent"
    assert session.cxt.metadata["task_info"]["item_id"] == "item1"

    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["xianyu:acc1:chat1"]
    assert rows[0]["pattern_code"] == "xianyu_agent"

    msgs = store.get_messages("xianyu:acc1:chat1")
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "你好"


def test_channel_second_turn_appends(
    client, store, registry_guard, fake_chat, monkeypatch
):
    """Second message reuses the session: persisted messages append, no duplicate session row."""
    monkeypatch.setenv("XIANYU_CHANNEL_PATTERN", "xianyu_agent")
    client.post("/api/v1/channel/xianyu", json=inbound())
    first_count = len(store.get_messages("xianyu:acc1:chat1"))

    client.post("/api/v1/channel/xianyu", json=inbound(message="能便宜点吗"))
    msgs = store.get_messages("xianyu:acc1:chat1")
    assert len(msgs) > first_count
    assert len(store.list_sessions()) == 1
    assert fake_chat[-1] == ("xianyu:acc1:chat1", "能便宜点吗")


def test_channel_no_pattern_503(client, registry_guard, fake_chat, monkeypatch):
    """XIANYU_CHANNEL_PATTERN unset and session missing: 503, no dialogue."""
    monkeypatch.delenv("XIANYU_CHANNEL_PATTERN", raising=False)
    resp = client.post("/api/v1/channel/xianyu", json=inbound(chat_id="chat-new"))
    assert resp.status_code == 503
    assert fake_chat == []
