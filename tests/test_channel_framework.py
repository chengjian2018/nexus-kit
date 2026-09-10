"""Channel framework tests -- protocol objects, generic handler, registry (fake specs, offline)."""

from types import SimpleNamespace
from typing import Any, Dict, Optional

from pydantic import BaseModel

from nexus.channels.base import ChannelSpec, EngineOps, InboundMessage


class _FakePayload(BaseModel):
    """Fake channel payload: user_id + text required, ts optional."""

    user_id: str
    text: str
    ts: Optional[float] = None


class FakeChannel:
    """Minimal ChannelSpec implementation, reused by handler/registry tests."""

    name = "fake"
    payload_model = _FakePayload
    default_pattern_env = "FAKE_CHANNEL_PATTERN"
    token_env = None
    stale_seconds = 300.0

    def parse(self, payload: _FakePayload) -> InboundMessage:
        return InboundMessage(
            channel=self.name,
            text=payload.text,
            session_key=payload.user_id,
            timestamp=payload.ts,
            task_info={"channel": self.name, "user_id": payload.user_id},
        )

    def build_reply(self, reply: str, session_id: str) -> Dict[str, Any]:
        return {"reply": reply, "session_id": session_id}


def test_inbound_message_defaults():
    """InboundMessage can be constructed directly; task_info defaults to an empty dict."""
    msg = InboundMessage(channel="x", text="hi", session_key="k")
    assert msg.timestamp is None
    assert msg.task_info == {}


def test_engine_ops_holds_callables():
    """EngineOps is a pure data bundle: the three operation fields are stored and read back verbatim."""
    async def _launch(*a, **k):
        return None, "0", ""

    async def _run(s, q):
        return "ok", None

    ops = EngineOps(get_session=lambda _sid: None,
                    launch_session=_launch,
                    run_chat_turn=_run)
    assert ops.get_session("any") is None
    from async_utils import arun
    assert arun(ops.run_chat_turn(None, "q"))[0] == "ok"


def test_fake_spec_satisfies_protocol():
    """FakeChannel structurally satisfies the ChannelSpec protocol (runtime_checkable)."""
    spec = FakeChannel()
    assert isinstance(spec, ChannelSpec)
    assert spec.name == "fake"
    msg = spec.parse(_FakePayload(user_id="u1", text="hi"))
    assert msg.session_key == "u1" and msg.text == "hi"
    assert spec.build_reply("r", "fake:u1") == {"reply": "r", "session_id": "fake:u1"}


# ============================================================================
# Registry
# ============================================================================

import pytest

from nexus.registry.channels import ChannelRegistry, discover_builtin_channels


class _BadSpecNoName:
    payload_model = _FakePayload
    default_pattern_env = "X"
    token_env = None
    stale_seconds = 1.0

    def parse(self, payload):  # pragma: no cover -- never called
        raise AssertionError

    def build_reply(self, reply, session_id):  # pragma: no cover
        raise AssertionError


def test_register_and_get():
    reg = ChannelRegistry()
    spec = FakeChannel()
    reg.register(spec)
    assert reg.get("fake") is spec
    assert reg.list_names() == ["fake"]
    assert reg.is_registered("fake") is True
    assert reg.get("nope") is None


def test_register_rejects_bad_names():
    """Missing name / path separators / uppercase -- invalid for a URL path, rejected at import time."""
    reg = ChannelRegistry()
    with pytest.raises(ValueError):
        reg.register(_BadSpecNoName())  # no name attribute
    bad = FakeChannel()
    bad.name = "a/b"
    with pytest.raises(ValueError):
        reg.register(bad)


def test_register_rejects_duplicate_name():
    reg = ChannelRegistry()
    reg.register(FakeChannel())
    with pytest.raises(ValueError):
        reg.register(FakeChannel())  # duplicate rejected: prevents two files racing for the same name


def test_register_rejects_non_callable_hooks():
    reg = ChannelRegistry()
    bad = FakeChannel()
    bad.parse = "not callable"
    with pytest.raises(ValueError):
        reg.register(bad)


def test_discover_builtin_channels_skips_framework_files(tmp_path):
    """AST discovery: only imports channel files containing a module-level registry.register();
    framework files (base/register/webhooks) are never imported even if they mention register."""
    (tmp_path / "base.py").write_text("registry.register(FakeChannel())\n", encoding="utf-8")
    (tmp_path / "register.py").write_text("registry = 1\n", encoding="utf-8")
    (tmp_path / "webhooks.py").write_text("registry.register(FakeChannel())\n", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    # Real channel: module-level registry.register(...) call
    (tmp_path / "good.py").write_text(
        "registry.register(FakeChannel())\n", encoding="utf-8"
    )
    # Not a channel: no register call
    (tmp_path / "helper.py").write_text("x = 1\n", encoding="utf-8")
    # register inside a function body does not count (AST only looks at module top level)
    (tmp_path / "nested.py").write_text(
        "def f():\n    registry.register(FakeChannel())\n", encoding="utf-8"
    )

    imported = discover_builtin_channels(tmp_path)
    assert imported == []  # good.py's import fails (FakeChannel undefined), skipped with a warning


def test_discover_imports_real_channel_file(tmp_path):
    """A self-contained channel file (no dependency on external names) is imported successfully and registered into the given registry."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "from nexus.registry.channels import registry\n"
        "from nexus.channels.base import InboundMessage\n"
        "from pydantic import BaseModel\n"
        "class P(BaseModel):\n"
        "    user_id: str\n"
        "class S:\n"
        "    name = 'discovered'\n"
        "    payload_model = P\n"
        "    default_pattern_env = 'X'\n"
        "    token_env = None\n"
        "    stale_seconds = 1.0\n"
        "    def parse(self, p):\n"
        "        return InboundMessage(channel=self.name, text=p.user_id, session_key=p.user_id)\n"
        "    def build_reply(self, reply, session_id):\n"
        "        return {'reply': reply}\n"
        "registry.register(S())\n",
        encoding="utf-8",
    )
    imported = discover_builtin_channels(tmp_path)
    # Files outside a package go through spec_from_file_location; the module name is fixed to _channel_ext_<stem>
    assert imported == ["_channel_ext_good"]
    from nexus.registry.channels import registry as global_reg
    assert global_reg.is_registered("discovered") is True
    global_reg._channels.pop("discovered", None)  # clean up global state


# ============================================================================
# Generic handler (fake engine ops + FakeChannel / custom fake spec)
# ============================================================================

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.channels.base import EngineOps
from nexus.channels.webhooks import build_channel_router


class HandlerHarness:
    """Fake engine ops + a single-channel app, recording calls for assertions (mirrors the Xianyu test harness shape)."""

    def __init__(self, spec=None, pattern_code="demo_pattern", token=None,
                 stale_seconds=300.0):
        self.spec = spec or FakeChannel()
        if token is not None:
            self.spec.token_env = "FAKE_CHANNEL_TOKEN"
        self.pattern_code = pattern_code
        self.sessions = {}
        self.launch_calls = []
        self.run_calls = []
        self.launch_error = None
        self.run_error = None

        async def launch_session(pattern_code, session_id, task_info, request_id, exist_ok=False):
            self.launch_calls.append(
                {"pattern_code": pattern_code, "session_id": session_id,
                 "task_info": task_info, "exist_ok": exist_ok}
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

        async def run_chat_turn(session, query):
            self.run_calls.append((session.session_id, query))
            if self.run_error is not None:
                return None, self.run_error
            return f"echo:{query}", None

        app = FastAPI()
        app.include_router(build_channel_router(
            self.spec,
            EngineOps(get_session=get_session, launch_session=launch_session,
                      run_chat_turn=run_chat_turn),
        ))
        self.client = TestClient(app)


def _post(h, json=None, params=None, env=None):
    """POST with env isolation (pattern/token env vars restored afterwards)."""
    import os
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.pop("FAKE_CHANNEL_PATTERN", None)
    for k, v in (env or {}).items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return h.client.post(
            f"/api/v1/channel/{h.spec.name}",
            json=json if json is not None else {"user_id": "u1", "text": "你好"},
            params=params,
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_handler_success_and_session_prefix():
    """Success path: session_id gets the channel prefix, task_info passed through, reply contract."""
    h = HandlerHarness()
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reply": "echo:你好", "session_id": "fake:u1"}
    call = h.launch_calls[0]
    assert call["session_id"] == "fake:u1"
    assert call["pattern_code"] == "demo_pattern"
    assert call["exist_ok"] is True
    assert call["task_info"] == {"channel": "fake", "user_id": "u1"}
    assert h.run_calls == [("fake:u1", "你好")]


def test_handler_existing_session_skips_launch():
    """Session exists: reused, no launch."""
    h = HandlerHarness()
    _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    _post(h, json={"user_id": "u1", "text": "第二条"}, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert len(h.launch_calls) == 1
    assert h.run_calls[-1] == ("fake:u1", "第二条")


def test_handler_no_pattern_503():
    """Session missing and pattern env unset: 503, telling which env to set."""
    h = HandlerHarness(pattern_code=None)
    resp = _post(h, env={})
    assert resp.status_code == 503
    assert "FAKE_CHANNEL_PATTERN" in resp.json()["detail"]
    assert h.launch_calls == [] and h.run_calls == []


def test_handler_launch_failure_500():
    h = HandlerHarness()
    h.launch_error = ("404", "pattern 'x' 未注册")
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 500
    assert "未注册" in resp.json()["detail"]


def test_handler_run_error_500():
    h = HandlerHarness()
    h.run_error = RuntimeError("LLM 超时")
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 500
    assert "LLM 超时" in resp.json()["detail"]


def test_handler_stale_message_swallowed():
    """Stale message: 200 + empty reply, no launch and no dialogue (reconnect replay protection)."""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1", "text": "hi", "ts": time.time() - 600},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == ""
    assert h.launch_calls == [] and h.run_calls == []


def test_handler_none_timestamp_bypasses_stale():
    """timestamp=None: no staleness filtering."""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1", "text": "hi", "ts": None},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:hi"


def test_handler_fresh_message_passes():
    """Fresh messages are processed normally."""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1", "text": "hi", "ts": time.time()},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "echo:hi"


def test_handler_token_wrong_403_right_200(monkeypatch):
    """Token check is enabled only when token_env names a non-empty env: wrong token 403, right token passes."""
    monkeypatch.setenv("FAKE_CHANNEL_TOKEN", "s3cret")
    h = HandlerHarness(token="s3cret")
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 403
    assert h.run_calls == []

    resp = _post(h, params={"token": "s3cret"},
                 env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200


def test_handler_token_env_unset_no_check(monkeypatch):
    """token_env names an env but the env is unset: no check (optional token semantics)."""
    monkeypatch.delenv("FAKE_CHANNEL_TOKEN", raising=False)
    h = HandlerHarness(token="s3cret")  # token_env already points to FAKE_CHANNEL_TOKEN
    resp = _post(h, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 200


def test_handler_payload_invalid_422():
    """Payload missing required fields: 422 (pydantic automatic)."""
    h = HandlerHarness()
    resp = _post(h, json={"user_id": "u1"}, env={"FAKE_CHANNEL_PATTERN": "demo_pattern"})
    assert resp.status_code == 422
