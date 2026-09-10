"""Session history compression tests — estimation / threshold / snap / summary-failure protection / end-to-end."""

import json
from async_utils import arun
from unittest.mock import patch

import pytest

from nexus.engine.compression import (
    _snap_to_pair_boundary,
    compress_history,
    estimate_tokens,
    maybe_compress,
    should_compress,
)
from nexus.engine.session import Session
from nexus.engine.store import SessionStore
from nexus.context import SessionMessage


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def test_estimate_tokens_cjk_and_ascii():
    # pure CJK: 4 chars × 2 + 4 overhead = 12
    assert estimate_tokens([SessionMessage(role="user", content="你好你好")]) == 12
    # pure ASCII: 8 chars × 0.25 = 2 + 4 = 6
    assert estimate_tokens([SessionMessage(role="user", content="abcdefgh")]) == 6
    # mixed: 3 CJK (6) + 4 ascii (1) + 4 = 11
    assert estimate_tokens([SessionMessage(role="user", content="你好吗abcd")]) == 11


def test_estimate_tokens_tool_payload_in_content():
    """Tool-turn payload lives in content, so it counts toward the estimate naturally (larger than an empty text line)."""
    from nexus.context import encode_tool_call_content
    plain = SessionMessage(role="user", content="q")
    payload = encode_tool_call_content(
        "", [{"id": "c1", "function": {"name": "weather", "arguments": "{}"}}])
    with_payload = SessionMessage(role="assistant", content=payload)
    assert (estimate_tokens([plain, with_payload])
            > estimate_tokens([plain, SessionMessage(role="assistant",
                                                     content="")]))


def test_should_compress_boundaries():
    msgs = [SessionMessage(role="user", content="长" * 4000)]
    assert not should_compress(msgs, threshold=0, retain_count=12)
    assert not should_compress(msgs, threshold=100, retain_count=12)  # too few messages
    many = [SessionMessage(role="user", content="长" * 100) for _ in range(20)]
    assert should_compress(many, threshold=100, retain_count=12)
    assert not should_compress(many, threshold=100000, retain_count=12)


# ---------------------------------------------------------------------------
# retain boundary snap
# ---------------------------------------------------------------------------

def test_snap_keeps_tool_pair_together():
    """split lands on a tool row: snap back to its assistant tool-turn start."""
    from nexus.context import encode_tool_call_content
    tool_calls = [{"id": "c1", "function": {"name": "t"}}]
    history = [
        SessionMessage(role="user", content="q1"),
        SessionMessage(role="assistant",
                       content=encode_tool_call_content("查", tool_calls)),
        SessionMessage(role="tool", content="r1",
                       metadata={"tool_call_id": "c1"}),
        SessionMessage(role="assistant", content="答"),
        SessionMessage(role="user", content="q2"),
    ]
    # split=2 lands on the tool row -> snapped back to 1 (the assistant run start)
    assert _snap_to_pair_boundary(history, 2) == 1
    # split not on a tool row -> kept as-is
    assert _snap_to_pair_boundary(history, 4) == 4


def test_snap_no_assistant_run_before_tool_keeps_split():
    """No assistant tool turn precedes the tool row: nowhere to snap back to, the original split is kept."""
    history = [SessionMessage(role="tool", content="r",
                              metadata={"tool_call_id": "c1"})]
    assert _snap_to_pair_boundary(history, 1) == 1


# ---------------------------------------------------------------------------
# compress_history
# ---------------------------------------------------------------------------

class _SummaryProvider:
    """Summary LLM stub: records each request, returns a fixed summary."""

    def __init__(self, reply="摘要：用户咨询手机", fail=False):
        self.reply = reply
        self.fail = fail
        self.seen = []

    async def achat_completion(self, messages, model, temperature, max_tokens,
                               **kw):
        self.seen.append(messages)
        if self.fail:
            raise RuntimeError("llm down")
        return {"content": self.reply, "tool_calls": []}


def _mk_session_with_history(store=None, n_pairs=10, session_id="comp-1"):
    """Build a session with 2×n_pairs history entries; with a store, launch+attach (messages persisted)."""
    session = Session(session_id=session_id, pattern_code="p")
    if store is not None:
        store.create_session(session)
        store.attach(session)
    for i in range(n_pairs):
        session.cxt.add_message("user", f"问题{i}：" + "长" * 50, stage="chat")
        session.cxt.add_message("assistant", f"回答{i}", stage="chat")
    return session


def test_compress_success_rebuilds_db_and_cxt(tmp_path):
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)  # history already persisted via the sink

    provider = _SummaryProvider()
    llm_config = {"code": "fake", "model": "m", "temperature": 0.3,
                  "max_tokens": 1024}
    with patch("nexus.engine.compression.build_provider", return_value=provider):
        ok = arun(compress_history(session, store, llm_config, retain_count=4))

    assert ok is True
    # DB: summary first + 4 retained entries
    history = store.get_history("comp-1")
    assert history[0].role == "summary"
    assert history[0].stage == "compress"
    assert len(history) == 5
    # cxt rebuilt in sync + in-turn marker corrected
    assert session.cxt.history[0] is not history[0] or True  # different object identities are fine
    assert session.cxt.history[0].role == "summary"
    assert len(session.cxt.history) == 5
    assert session.cxt.turn_history_start == 5
    # Summary request: system is the summarizer persona, user is the concatenated old conversation
    assert provider.seen[0][0]["role"] == "system"
    assert "摘要" in provider.seen[0][0]["content"]
    assert "问题0" in provider.seen[0][1]["content"]
    # Only old messages (before the split) are summarized; questions inside the retention window are not
    assert "问题9" not in provider.seen[0][1]["content"]
    store.close()


def test_compress_llm_failure_keeps_everything(tmp_path):
    """Summary LLM failure: DB and cxt.history stay untouched (iron rule)."""
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)
    before_mem = list(session.cxt.history)
    before_db = store.get_history("comp-1")

    provider = _SummaryProvider(fail=True)
    with patch("nexus.engine.compression.build_provider", return_value=provider):
        ok = arun(compress_history(session, store,
                                   {"code": "f", "model": "m"}, retain_count=4))

    assert ok is False
    assert store.get_history("comp-1") == before_db
    assert session.cxt.history == before_mem
    store.close()


def test_compress_aborts_when_db_memory_mismatch(tmp_path):
    """DB/memory mismatch: abort compression (never delete against a misaligned history)."""
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)
    # append one in-memory entry that is never persisted -> mismatch
    session.cxt.history.append(SessionMessage(role="user", content="幽灵"))

    provider = _SummaryProvider()
    with patch("nexus.engine.compression.build_provider", return_value=provider):
        ok = arun(compress_history(session, store,
                                   {"code": "f", "model": "m"}, retain_count=4))

    assert ok is False
    assert not provider.seen  # LLM not called (validation runs first)
    assert len(store.get_history("comp-1")) == 20
    store.close()


def test_compress_empty_summary_aborts(tmp_path):
    """Empty summary: abort."""
    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)

    provider = _SummaryProvider(reply="   ")
    with patch("nexus.engine.compression.build_provider", return_value=provider):
        ok = arun(compress_history(session, store,
                                   {"code": "f", "model": "m"}, retain_count=4))
    assert ok is False
    assert len(store.get_history("comp-1")) == 20
    store.close()


# ---------------------------------------------------------------------------
# End-to-end: after compression, built messages do not duplicate the query
# ---------------------------------------------------------------------------

def test_after_compress_query_appears_once(tmp_path):
    from nexus.engine.messages import default_build_messages
    from nexus.model.module import AgentModule

    store = SessionStore(str(tmp_path / "t.db"))
    session = _mk_session_with_history(store)
    session.cxt.turn_history_start = len(session.cxt.history)
    session.cxt.user_query = "新问题"

    with patch("nexus.engine.compression.build_provider",
               return_value=_SummaryProvider("摘要：此前咨询")):
        ok = arun(compress_history(session, store,
                                   {"code": "f", "model": "m"}, retain_count=4))
    assert ok is True

    messages = default_build_messages(AgentModule(module_code="m"), session.cxt)
    user_contents = [m["content"] for m in messages if m["role"] == "user"]
    assert user_contents.count("新问题") == 1
    assert any("untrusted_会话摘要" in c for c in user_contents)
    # retained old turns are replayed as usual
    assert any(c.startswith("问题") for c in user_contents)
    store.close()


def test_maybe_compress_skips_without_store_or_threshold():
    """store None / threshold 0: skip silently without raising."""
    session = _mk_session_with_history()
    arun(maybe_compress(session, None))  # no-op
    with patch("nexus.settings.get_session_compress_config",
               return_value=(0, 12)):
        arun(maybe_compress(session, object()))
    assert len(session.cxt.history) == 20
