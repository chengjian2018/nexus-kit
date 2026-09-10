"""SQLite session persistence — source of truth for messages (per-message
write-through) + state snapshots + audit.

Governance (TTL/eviction/existence checks) happens in main.py's in-memory
state. Messages are persisted one by one, as they are added, through the
message_sink wired up by ``attach`` (``append_message``; the DB is the source
of truth — a mid-turn crash loses no messages); end of turn ``save_snapshot``
only writes back the sessions state snapshot; startup restores via
``load_active_sessions``; compression reorders rows via ``replace_history``.
Single connection + lock serialization (FastAPI sync endpoints run on a
thread pool; write traffic is tiny).
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nexus.engine.session import Session
from nexus.context import SessionMessage

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    pattern_code        TEXT NOT NULL,
    launch_epoch        INTEGER NOT NULL DEFAULT 0,
    request_id          TEXT,
    task_info           TEXT NOT NULL DEFAULT '{}',
    current_module_code TEXT,
    current_node_code   TEXT,
    filled_slots        TEXT NOT NULL DEFAULT '{}',
    created_at          REAL NOT NULL,
    last_active_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active_at);
CREATE INDEX IF NOT EXISTS idx_sessions_pattern    ON sessions(pattern_code);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    launch_epoch INTEGER NOT NULL DEFAULT 0,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT '',
    metadata   TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class SessionStore:
    """Session audit store: sessions (state snapshots) + messages (row-level message log)."""

    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Launch persistence
    # ------------------------------------------------------------------

    def create_session(self, session: Session) -> None:
        """Persist a new session (called at launch).

        Re-launching the same session_id starts a new generation
        (launch_epoch + 1): the old generation's message audit rows stay in
        place and the sessions row is upserted (created_at reset).
        """
        now = time.time()
        request_id = (session.cxt.metadata or {}).get("request_id")
        task_info = json.dumps(session.task_info or {}, ensure_ascii=False)
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT launch_epoch FROM sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            epoch = (row["launch_epoch"] + 1) if row is not None else 0
            self._conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, pattern_code, launch_epoch, request_id, task_info,
                    current_module_code, current_node_code, filled_slots,
                    created_at, last_active_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.pattern_code,
                    epoch,
                    request_id,
                    task_info,
                    session.cxt.current_module_code,
                    session.cxt.current_node_code,
                    filled_slots,
                    now,
                    now,
                ),
            )

    # ------------------------------------------------------------------
    # End-of-turn persistence
    # ------------------------------------------------------------------

    def attach(self, session: Session) -> None:
        """Wire up per-message write-through: every add_message on the session
        is persisted immediately.

        Sink write failures are swallowed on the ``DialogueContext.add_message``
        side (logged, never blocking the dialogue); attach also runs at launch
        even when create_session fails — no messages are lost once the DB
        recovers mid-way.
        """
        session.cxt.message_sink = (
            lambda msg: self.append_message(session, msg))

    def save_snapshot(self, session: Session) -> None:
        """Write back the end-of-turn sessions state snapshot
        (module/node/slots/last-active time).

        Message appending has moved to ``append_message`` (per-message
        write-through once attached); this method no longer touches the
        messages table — one end-of-turn transaction to write back state.
        """
        now = time.time()
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE sessions
                   SET current_module_code = ?, current_node_code = ?,
                       filled_slots = ?, last_active_at = ?
                   WHERE session_id = ?""",
                (
                    session.cxt.current_module_code,
                    session.cxt.current_node_code,
                    filled_slots,
                    now,
                    session.session_id,
                ),
            )

    # ------------------------------------------------------------------
    # Per-message write-through (DB is the source of truth) + compression primitives
    # ------------------------------------------------------------------

    def _current_epoch(self, session_id: str) -> int:
        """Return the session's current-generation epoch (0 if no row; requires self._lock held)."""
        row = self._conn.execute(
            "SELECT launch_epoch FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["launch_epoch"] if row is not None else 0

    def append_message(self, session: Session, msg: SessionMessage) -> None:
        """Persist a single message immediately (write side of message_sink,
        triggered per add_message).

        The epoch is looked up at write time (same idiom as save_turn): an
        in-flight turn of a session evicted from memory and re-launched lands
        in the new generation — same kind of deviation as the existing batch
        write, not introduced here.
        """
        with self._lock, self._conn:
            epoch = self._current_epoch(session.session_id)
            self._conn.execute(
                """INSERT INTO messages
                   (session_id, launch_epoch, role, content, stage, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    epoch,
                    msg.role,
                    msg.content,
                    msg.stage,
                    json.dumps(msg.metadata or {}, ensure_ascii=False),
                    time.time(),
                ),
            )

    def get_history(self, session_id: str) -> List[SessionMessage]:
        """Rebuild all messages of the current epoch in ascending id order
        (used for the DB/memory alignment check before compression).

        Tool-call traces round-trip verbatim inside the content/metadata
        payloads (no dedicated columns needed).
        """
        with self._lock:
            epoch = self._current_epoch(session_id)
            rows = self._conn.execute(
                """SELECT role, content, stage, metadata
                   FROM messages WHERE session_id = ? AND launch_epoch = ?
                   ORDER BY id""",
                (session_id, epoch),
            ).fetchall()
            return [
                SessionMessage(
                    role=r["role"],
                    content=r["content"],
                    stage=r["stage"],
                    metadata=json.loads(r["metadata"] or "{}"),
                )
                for r in rows
            ]

    def replace_history(
        self, session: Session, summary_text: str, keep_idx: int
    ) -> None:
        """Compression rewrite: delete all rows of the current epoch → insert
        the summary row → re-insert ``history[keep_idx:]``.

        Single transaction; first checks the DB row count against
        ``len(cxt.history)`` and raises ``RuntimeError`` on mismatch (the
        transaction rolls back, DB untouched); the caller catches it and
        abandons compression — out-of-sync history is never deleted.
        Retained rows get renumbered ids (AUTOINCREMENT cannot insert before
        existing rows); the summary naturally sorts first.
        """
        now = time.time()
        with self._lock, self._conn:
            epoch = self._current_epoch(session.session_id)
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages"
                " WHERE session_id = ? AND launch_epoch = ?",
                (session.session_id, epoch),
            ).fetchone()["n"]
            if count != len(session.cxt.history):
                raise RuntimeError(
                    f"DB/内存消息数不齐，放弃压缩: session={session.session_id}"
                    f" db={count} mem={len(session.cxt.history)}"
                )
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND launch_epoch = ?",
                (session.session_id, epoch),
            )
            rows = [(
                session.session_id, epoch, "summary", summary_text, "compress",
                "{}", now,
            )]
            for msg in session.cxt.history[keep_idx:]:
                rows.append((
                    session.session_id, epoch, msg.role, msg.content, msg.stage,
                    json.dumps(msg.metadata or {}, ensure_ascii=False),
                    now,
                ))
            self._conn.executemany(
                """INSERT INTO messages
                   (session_id, launch_epoch, role, content, stage, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    # ------------------------------------------------------------------
    # Restart restore
    # ------------------------------------------------------------------

    def load_active_sessions(self, ttl_seconds: float) -> List[Tuple[Session, float]]:
        """Load sessions whose ``last_active_at`` has not expired (for startup
        restore).

        Returns:
            List of ``(Session, wall-clock last_active_at)``. Session.pattern
            is None and node_map/module_map are empty — resolved and injected
            by the caller from the registries.
        """
        cutoff = time.time() - ttl_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE last_active_at >= ?"
                " AND launch_epoch = (SELECT MAX(launch_epoch) FROM sessions s2"
                "                      WHERE s2.session_id = sessions.session_id)"
                " ORDER BY last_active_at DESC",
                (cutoff,),
            ).fetchall()
            restored: List[Tuple[Session, float]] = []
            for row in rows:
                msgs = self._conn.execute(
                    "SELECT role, content, stage, metadata FROM messages"
                    " WHERE session_id = ? AND launch_epoch = ? ORDER BY id",
                    (row["session_id"], row["launch_epoch"]),
                ).fetchall()
                session = Session(
                    session_id=row["session_id"],
                    pattern_code=row["pattern_code"],
                )
                session.task_info = json.loads(row["task_info"] or "{}")
                session.cxt.metadata["task_info"] = session.task_info
                session.cxt.metadata["request_id"] = row["request_id"]
                session.cxt.current_module_code = row["current_module_code"]
                session.cxt.current_node_code = row["current_node_code"]
                session.cxt.filled_slots = json.loads(row["filled_slots"] or "{}")
                session.cxt.history = [
                    SessionMessage(
                        role=m["role"],
                        content=m["content"],
                        stage=m["stage"],
                        metadata=json.loads(m["metadata"] or "{}"),
                    )
                    for m in msgs
                ]
                restored.append((session, row["last_active_at"]))
            return restored

    # ------------------------------------------------------------------
    # Audit queries (read-only)
    # ------------------------------------------------------------------

    def list_sessions(
        self,
        pattern_code: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Session list (descending by last_active_at), with message counts."""
        sql = """
            SELECT s.session_id, s.pattern_code, s.launch_epoch,
                   s.current_module_code,
                   s.current_node_code, s.created_at, s.last_active_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id)
                       AS message_count
            FROM sessions s
        """
        params: List[Any] = []
        if pattern_code:
            sql += " WHERE s.pattern_code = ?"
            params.append(pattern_code)
        sql += " ORDER BY s.last_active_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_messages(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """All messages of a session across every generation (with launch_epoch;
        ascending by id); None if the session does not exist.
        """
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if exists is None:
                return None
            rows = self._conn.execute(
                """SELECT id, launch_epoch, role, content, stage, metadata, created_at
                   FROM messages WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
            messages = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d["metadata"] or "{}")
                messages.append(d)
            return messages

    # ------------------------------------------------------------------
    # Async twins (asyncio rewrite migration): the sync sqlite3 core is kept
    # during the phase-2..5 window; async callers go through asyncio.to_thread
    # so the blocking core never runs on the event loop. Phase-⑤ replaces the
    # core with aiosqlite and merges the twins into one async method family.
    # ------------------------------------------------------------------

    async def aget_history(self, session_id: str) -> List[SessionMessage]:
        return await asyncio.to_thread(self.get_history, session_id)

    async def areplace_history(self, session: Session, summary_text: str,
                               keep_idx: int) -> None:
        await asyncio.to_thread(self.replace_history, session,
                                summary_text, keep_idx)

    async def acreate_session(self, session: Session) -> None:
        await asyncio.to_thread(self.create_session, session)

    async def asave_snapshot(self, session: Session) -> None:
        await asyncio.to_thread(self.save_snapshot, session)

    async def aload_active_sessions(
            self, ttl_seconds: float) -> List[Tuple[Session, float]]:
        return await asyncio.to_thread(self.load_active_sessions, ttl_seconds)

    async def alist_sessions(self, pattern_code=None, limit=50, offset=0
                             ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_sessions, pattern_code,
                                       limit, offset)

    async def aget_messages(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        return await asyncio.to_thread(self.get_messages, session_id)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)
