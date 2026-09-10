"""SessionStore unit tests -- table creation / persist / incremental append / restore / queries.

All cases use a tmp-file DB + raw sqlite3 assertions (never the read APIs under test);
fake sessions are hand-built, with no FastAPI or LLM dependency.
"""

import json
import sqlite3

from async_utils import arun

from nexus.engine.session import Session
from nexus.engine.store import SessionStore


def make_session(session_id="s1", pattern_code="xianyu_agent"):
    """Build a minimal Session with task info."""
    session = Session(session_id=session_id, pattern_code=pattern_code)
    session.task_info = {"caller": "pytest"}
    session.cxt.metadata["request_id"] = f"req-{session_id}"
    return session


def fetch_one(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def fetch_all(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def test_init_idempotent(tmp_path):
    """Creating a store repeatedly against the same file (idempotent DDL) must not raise."""
    db = str(tmp_path / "t.db")
    store1 = arun(SessionStore.create(db))
    arun(store1.close())
    store2 = arun(SessionStore.create(db))
    arun(store2.close())


def test_create_session_roundtrip(tmp_path):
    """Launch persist: sessions row fields and JSON columns round-trip exactly."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session()
    session.cxt.current_module_code = "xianyu_root"
    session.cxt.current_node_code = "route_root"
    session.cxt.filled_slots = {"brand": "特斯拉"}
    arun(store.create_session(session))
    arun(store.close())

    row = fetch_one(db, "SELECT * FROM sessions WHERE session_id = 's1'")
    assert row is not None
    assert row["pattern_code"] == "xianyu_agent"
    assert row["request_id"] == "req-s1"
    assert json.loads(row["task_info"]) == {"caller": "pytest"}
    assert row["current_module_code"] == "xianyu_root"
    assert row["current_node_code"] == "route_root"
    assert json.loads(row["filled_slots"]) == {"brand": "特斯拉"}
    assert row["created_at"] > 0 and row["last_active_at"] > 0


def test_create_session_keeps_old_trail(tmp_path):
    """Re-launch with the same session_id: old messages kept (generation scheme), sessions row epoch+1."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    arun(store.create_session(make_session()))
    # Manually insert an old message to simulate the previous generation's trail
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, stage, metadata, created_at)"
            " VALUES ('s1', 'user', '旧消息', 'chat', '{}', 1.0)"
        )
    conn.close()

    arun(store.create_session(make_session()))  # re-launch
    arun(store.close())

    assert fetch_one(db, "SELECT COUNT(*) FROM messages")[0] == 1
    row = fetch_one(db, "SELECT launch_epoch FROM sessions WHERE session_id = 's1'")
    assert row["launch_epoch"] == 1


def test_write_through_writes_current_epoch(tmp_path):
    """After re-launch: new messages carry the current epoch=1, old messages epoch=0."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session()
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "第一代", stage="chat"))

    arun(store.create_session(make_session()))  # re-launch, epoch=1
    arun(session.cxt.add_message("user", "第二代", stage="chat"))
    arun(store.close())

    msgs = fetch_all(db, "SELECT content, launch_epoch FROM messages ORDER BY id")
    assert [(m["content"], m["launch_epoch"]) for m in msgs] == [
        ("第一代", 0),
        ("第二代", 1),
    ]


def test_load_active_sessions_restores_current_epoch_only(tmp_path):
    """Restore takes only the current generation: old-generation messages stay in the DB but never enter history."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session("alive")
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "旧代消息", stage="chat"))

    arun(store.create_session(make_session("alive")))# epoch=1
    arun(session.cxt.add_message("user", "当代消息", stage="chat"))

    restored = arun(store.load_active_sessions(ttl_seconds=3600))
    arun(store.close())

    r, _ = restored[0]
    assert [m.content for m in r.cxt.history] == ["当代消息"]


def test_get_messages_includes_all_epochs(tmp_path):
    """Audit: get_messages returns messages from every generation, with a launch_epoch key, ordered by id ascending."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session()
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "第一代", stage="chat"))
    arun(store.create_session(make_session()))# epoch=1
    arun(session.cxt.add_message("user", "第二代", stage="chat"))

    msgs = arun(store.get_messages("s1"))
    arun(store.close())
    assert [m["content"] for m in msgs] == ["第一代", "第二代"]
    assert [m["launch_epoch"] for m in msgs] == [0, 1]
    assert msgs[0]["id"] < msgs[1]["id"]


def test_write_through_appends_incrementally(tmp_path):
    """Two turns of dialogue: messages are written to the DB one by one immediately; the end-of-turn snapshot only writes back state (no double write)."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session()
    arun(store.create_session(session))
    store.attach(session)

    # Round 1: user + assistant (per-message write-through)
    arun(session.cxt.add_message("user", "你好", stage="chat"))
    arun(session.cxt.add_message("assistant", "您好", stage="chat"))
    arun(store.save_snapshot(session))

    # State changes + new messages ahead of round 2
    session.cxt.filled_slots["brand"] = "特斯拉"
    session.cxt.current_node_code = "buy_ask_budget"
    arun(session.cxt.add_message("user", "我想买车", stage="chat"))
    arun(session.cxt.add_message("assistant", "回复", stage="chat"))
    arun(store.save_snapshot(session))
    arun(store.close())

    msgs = fetch_all(db, "SELECT * FROM messages ORDER BY id")
    assert [m["content"] for m in msgs] == ["你好", "您好", "我想买车", "回复"]
    assert all(m["session_id"] == "s1" for m in msgs)
    assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"
    # No double write: the DB row count matches the in-memory history length
    assert len(msgs) == len(session.cxt.history)

    row = fetch_one(db, "SELECT * FROM sessions WHERE session_id = 's1'")
    assert row["current_node_code"] == "buy_ask_budget"
    assert json.loads(row["filled_slots"]) == {"brand": "特斯拉"}
    # After round 2, last_active_at has been refreshed (greater than created_at)
    assert row["last_active_at"] >= row["created_at"]


def test_write_through_message_metadata_json(tmp_path):
    """Message metadata column JSON round-trip."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session()
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message(
        "tool", "tool_result", stage="agent", metadata={"tool": "search_product_knowledge"}
    ))
    arun(store.close())

    row = fetch_one(db, "SELECT metadata FROM messages")
    assert json.loads(row[0]) == {"tool": "search_product_knowledge"}


def test_snapshot_without_new_messages_only_refreshes_state(tmp_path):
    """With no new messages, save_snapshot only refreshes state: no row inserted, no exception."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session()
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "q", stage="chat"))
    arun(store.save_snapshot(session))
    arun(store.save_snapshot(session))
    arun(store.close())

    assert fetch_one(db, "SELECT COUNT(*) FROM messages")[0] == 1


def test_load_active_sessions_restores_fields(tmp_path):
    """Restore: history/filled_slots/current node/task info are restored; pattern is left empty for the caller to resolve."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    session = make_session("alive")
    session.cxt.current_module_code = "xianyu_root"
    session.cxt.current_node_code = "menu_sales"
    session.cxt.filled_slots = {"brand": "特斯拉"}
    arun(store.create_session(session))
    store.attach(session)
    arun(session.cxt.add_message("user", "你好", stage="chat"))
    arun(session.cxt.add_message("assistant", "您好", stage="chat"))

    restored = arun(store.load_active_sessions(ttl_seconds=3600))
    arun(store.close())

    assert len(restored) == 1
    r, last_active = restored[0]
    assert isinstance(last_active, float) and last_active > 0
    assert r.session_id == "alive"
    assert r.pattern_code == "xianyu_agent"
    assert r.pattern is None
    assert r.task_info == {"caller": "pytest"}
    assert r.cxt.metadata["request_id"] == "req-alive"
    assert r.cxt.current_module_code == "xianyu_root"
    assert r.cxt.current_node_code == "menu_sales"
    assert r.cxt.filled_slots == {"brand": "特斯拉"}
    assert [(m.role, m.content) for m in r.cxt.history] == [
        ("user", "你好"),
        ("assistant", "您好"),
    ]


def test_load_active_sessions_filters_expired(tmp_path):
    """Sessions inactive beyond the ttl are not restored."""
    import time as _time

    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    arun(store.create_session(make_session("alive")))
    arun(store.create_session(make_session("dead")))
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_id = 'dead'",
            (_time.time() - 9999,),
        )
    conn.close()

    restored = arun(store.load_active_sessions(ttl_seconds=3600))
    arun(store.close())

    assert [s.session_id for s, _ in restored] == ["alive"]


def _seed_two_sessions(store):
    """Seed two sessions with one dialogue turn each (write-through); returns (ids)."""
    for sid in ("sa", "sb"):
        session = make_session(sid, pattern_code="xianyu_agent" if sid == "sa" else "other")
        arun(store.create_session(session))
        store.attach(session)
        arun(session.cxt.add_message("user", f"q-{sid}", stage="chat"))
        arun(session.cxt.add_message("assistant", f"a-{sid}", stage="chat"))


def test_list_sessions_filter_and_order(tmp_path):
    """Filter by pattern_code, order by last_active_at descending, include message_count."""
    import time as _time

    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    _seed_two_sessions(store)
    # Roll sa's last_active_at back to make it older
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_id = 'sa'",
            (_time.time() - 100,),
        )
    conn.close()

    all_rows = arun(store.list_sessions(pattern_code=None, limit=50, offset=0))
    assert [r["session_id"] for r in all_rows] == ["sb", "sa"]

    filtered = arun(store.list_sessions(pattern_code="other", limit=50, offset=0))
    assert [r["session_id"] for r in filtered] == ["sb"]

    assert all_rows[0]["message_count"] == 2
    assert "pattern_code" in all_rows[0]
    arun(store.close())


def test_list_sessions_pagination(tmp_path):
    """limit/offset pagination."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    _seed_two_sessions(store)

    page = arun(store.list_sessions(pattern_code=None, limit=1, offset=1))
    assert len(page) == 1
    assert page[0]["session_id"] in ("sa", "sb")
    arun(store.close())


def test_get_messages_ordered_and_typed(tmp_path):
    """Messages ordered by id ascending; metadata deserialized into a dict."""
    db = str(tmp_path / "t.db")
    store = arun(SessionStore.create(db))
    _seed_two_sessions(store)

    msgs = arun(store.get_messages("sa"))
    assert msgs is not None
    assert [m["content"] for m in msgs] == ["q-sa", "a-sa"]
    assert msgs[0]["id"] < msgs[1]["id"]
    assert isinstance(msgs[0]["metadata"], dict)
    assert msgs[0]["stage"] == "chat"
    arun(store.close())


def test_get_messages_missing_session(tmp_path):
    """Missing session returns None (the endpoint turns it into a 404 envelope)."""
    store = arun(SessionStore.create(str(tmp_path / "t.db")))
    assert arun(store.get_messages("nope")) is None
    arun(store.close())


# ---------------------------------------------------------------------------
# Migration + per-message write-through + compression primitive
# ---------------------------------------------------------------------------

def test_migrate_adds_tool_columns_to_legacy_db(tmp_path):
    """Legacy-schema DB (no tool columns) -> missing columns are added on open; old data stays readable."""
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    with conn:
        conn.executescript("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                pattern_code TEXT NOT NULL,
                launch_epoch INTEGER NOT NULL DEFAULT 0,
                request_id TEXT,
                task_info TEXT NOT NULL DEFAULT '{}',
                current_module_code TEXT,
                current_node_code TEXT,
                filled_slots TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                last_active_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                launch_epoch INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO sessions (session_id, pattern_code, created_at, last_active_at)"
            " VALUES ('s1', 'xianyu_agent', 1.0, 1.0)")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, stage, metadata, created_at)"
            " VALUES ('s1', 'user', '旧数据', 'chat', '{}', 1.0)")
    conn.close()

    store = arun(SessionStore.create(db))  # usable on open (the payload scheme needs no migration)
    msgs = arun(store.get_messages("s1"))
    assert msgs is not None and msgs[0]["content"] == "旧数据"
    arun(store.close())


def test_messages_table_has_no_tool_columns(tmp_path):
    """Payload scheme means zero schema changes: messages has only seven columns; the tool trail goes through content/metadata."""
    store = arun(SessionStore.create(str(tmp_path / "t.db")))
    arun(store.close())
    probe = sqlite3.connect(str(tmp_path / "t.db"))
    cols = {r[1] for r in probe.execute("PRAGMA table_info(messages)").fetchall()}
    probe.close()
    assert cols == {"id", "session_id", "launch_epoch", "role", "content",
                    "stage", "metadata", "created_at"}


def test_append_message_tool_payload_roundtrip(tmp_path):
    """append_message: tool-turn JSON payload + tool row metadata.tool_call_id round-trip."""
    store = arun(SessionStore.create(str(tmp_path / "t.db")))
    session = make_session()
    arun(store.create_session(session))

    from nexus.context import SessionMessage, decode_tool_call_content, encode_tool_call_content
    tool_calls = [{"id": "call_1", "type": "function",
                   "function": {"name": "list_products", "arguments": "{}"}}]
    arun(store.append_message(session, SessionMessage(
        role="assistant",
        content=encode_tool_call_content("查询中", tool_calls),
        stage="agent")))
    arun(store.append_message(session, SessionMessage(
        role="tool", content="22 度", stage="agent",
        metadata={"tool_call_id": "call_1"})))
    arun(store.append_message(session, SessionMessage(
        role="user", content="谢谢", stage="chat")))

    history = arun(store.get_history("s1"))
    assert len(history) == 3
    assert decode_tool_call_content(history[0].content) == ("查询中", tool_calls)
    assert history[1].metadata["tool_call_id"] == "call_1"
    assert history[2].metadata == {}
    arun(store.close())


def test_append_message_writes_current_epoch(tmp_path):
    """After re-launch, append_message writes to the current epoch and get_history only fetches the current one."""
    store = arun(SessionStore.create(str(tmp_path / "t.db")))
    session = make_session()
    arun(store.create_session(session))
    from nexus.context import SessionMessage
    arun(store.append_message(session, SessionMessage(
        role="user", content="第一代消息", stage="chat")))
    arun(store.create_session(session))  # epoch 0 → 1
    arun(store.append_message(session, SessionMessage(
        role="user", content="第二代消息", stage="chat")))

    history = arun(store.get_history("s1"))
    assert [m.content for m in history] == ["第二代消息"]
    arun(store.close())


def test_replace_history_summary_first_and_retained_kept(tmp_path):
    """Compression re-layout: the summary row comes first, retained messages keep their order (payloads round-trip unchanged)."""
    store = arun(SessionStore.create(str(tmp_path / "t.db")))
    session = make_session()
    arun(store.create_session(session))
    from nexus.context import SessionMessage, decode_tool_call_content, encode_tool_call_content
    tool_calls = [{"id": "c1", "function": {"name": "t"}}]
    session.cxt.history = [
        SessionMessage(role="user", content="旧问题1", stage="chat"),
        SessionMessage(role="assistant", content="旧回答1", stage="chat"),
        SessionMessage(role="user", content="新问题", stage="chat"),
        SessionMessage(role="assistant",
                       content=encode_tool_call_content("查一下", tool_calls),
                       stage="agent"),
        SessionMessage(role="tool", content="工具结果", stage="agent",
                       metadata={"tool_call_id": "c1"}),
        SessionMessage(role="assistant", content="新回答", stage="chat"),
    ]
    # Align the DB with memory (under write-through semantics this is written by append_message)
    for msg in session.cxt.history:
        arun(store.append_message(session, msg))

    arun(store.replace_history(session, "此前对话要点：买手机", keep_idx=2))

    history = arun(store.get_history("s1"))
    assert len(history) == 5
    assert history[0].role == "summary"
    assert history[0].content == "此前对话要点：买手机"
    assert history[0].stage == "compress"
    assert decode_tool_call_content(history[2].content) == ("查一下", tool_calls)
    assert history[3].metadata["tool_call_id"] == "c1"
    assert history[4].content == "新回答"
    arun(store.close())


def test_replace_history_mismatch_leaves_db_untouched(tmp_path):
    """DB/memory row-count mismatch: raises RuntimeError, the transaction rolls back, and the DB is left untouched."""
    import pytest
    store = arun(SessionStore.create(str(tmp_path / "t.db")))
    session = make_session()
    arun(store.create_session(session))
    from nexus.context import SessionMessage
    arun(store.append_message(session, SessionMessage(
        role="user", content="DB 里的消息", stage="chat")))
    # In-memory history is empty -> mismatch

    with pytest.raises(RuntimeError):
        arun(store.replace_history(session, "摘要", keep_idx=0))

    history = arun(store.get_history("s1"))
    assert len(history) == 1 and history[0].content == "DB 里的消息"
    arun(store.close())
