"""SQLite session persistence — source of truth for messages (per-message
write-through) + state snapshots + audit.

Governance (TTL/eviction/existence checks) happens in host/governor.py's
in-memory state (wired by host/main.py). Messages are persisted one by one, as they are added, through the
message_sink wired up by ``attach`` (``append_message``; the DB is the source
of truth — a mid-turn crash loses no messages); end of turn ``save_snapshot``
only writes back the sessions state snapshot; startup restores via
``load_active_sessions``; compression reorders rows via ``replace_history``.

Async since the asyncio rewrite: the core is aiosqlite (a dedicated worker
thread executes the SQL; futures resolve on the caller's loop), so all
public methods are coroutines. Construction is ``await SessionStore.create(
path)`` (open + WAL + schema); there is no sync constructor.
"""

import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from nexus.engine.session import Session
from nexus.context import SessionMessage

logger = logging.getLogger(__name__)

# Per-event payload cap (serialized char count; overflow stores a preview +
# truncated marker), and base64 data-URIs are rejected (no binary in trace
# rows — the audit trail stays a lightweight text shape)
_TRACE_PAYLOAD_LIMIT = 8 * 1024
_DATA_URI_RE = re.compile(r"data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    pattern_code        TEXT NOT NULL,
    launch_epoch        INTEGER NOT NULL DEFAULT 0,
    request_id          TEXT,
    task_info           TEXT NOT NULL DEFAULT '{}',
    current_node_code   TEXT,
    graph_state         TEXT NOT NULL DEFAULT '{}',
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

-- Append-only trace trail (docs/design/session-persistence.md §4): kind="trace"
-- events are *facts already happened* — persisted as they fire (mid-turn
-- included, unlike the end-of-turn sessions snapshot). The autoincrement id
-- IS the global ordering; turn_id carries the initiating request_id.
CREATE TABLE IF NOT EXISTS trace_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    turn_id      TEXT NOT NULL DEFAULT '',
    launch_epoch INTEGER NOT NULL DEFAULT 0,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    truncated    INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_session ON trace_events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_trace_turn    ON trace_events(session_id, turn_id, id);
"""


class SessionStore:
    """Session audit store: sessions (state snapshots) + messages (row-level message log).

    All methods are coroutines (aiosqlite core; the single worker thread
    serializes access — no extra locking needed).
    """

    def __init__(self, db_path: str):
        """Direct construction is not supported — use ``await create(...)``.

        Kept only as a type-shelled placeholder so accidental sync
        construction fails with a readable error instead of a missing
        attribute deep inside a turn.
        """
        raise TypeError(
            "SessionStore is async: use `await SessionStore.create(db_path)`")

    @classmethod
    async def create(cls, db_path: str) -> "SessionStore":
        """Open the DB (mkdir + WAL + schema) and return a ready store."""
        self = cls.__new__(cls)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # connect() returns a Connection proxy; awaiting it exactly ONCE
        # starts the worker thread + opens the sqlite handle (a second bare
        # await would try to start the thread again — aiosqlite semantics)
        conn = aiosqlite.connect(db_path)
        await conn
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(_SCHEMA)
        await cls._migrate(conn)
        await conn.commit()
        self._conn = conn
        return self

    @staticmethod
    async def _migrate(conn) -> None:
        """One-shot schema migration for legacy databases: drop the
        module-layer column, add the graph_state column. Fresh databases
        already match _SCHEMA (both steps no-op)."""
        rows = await conn.execute_fetchall(
            "PRAGMA table_info(sessions)")
        cols = {r["name"] for r in rows}
        if "current_module_code" in cols:
            # the module cursor has no successor — dropped (the
            # FSM node cursor lives on in current_node_code)
            try:
                await conn.execute(
                    "ALTER TABLE sessions DROP COLUMN current_module_code")
            except Exception:
                # ancient sqlite without DROP COLUMN: leave the orphan
                # column in place (harmless — nothing reads it)
                pass
        if "graph_state" not in cols:
            await conn.execute(
                "ALTER TABLE sessions ADD COLUMN graph_state TEXT"
                " NOT NULL DEFAULT '{}'")

    async def close(self) -> None:
        await self._conn.close()

    # ------------------------------------------------------------------
    # Launch persistence
    # ------------------------------------------------------------------

    async def create_session(self, session: Session) -> None:
        """Persist a new session (called at launch).

        Re-launching the same session_id starts a new generation
        (launch_epoch + 1): the old generation's message audit rows stay in
        place and the sessions row is upserted (created_at reset).
        """
        now = time.time()
        request_id = (session.cxt.metadata or {}).get("request_id")
        task_info = json.dumps(session.task_info or {}, ensure_ascii=False)
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        rows = await self._conn.execute_fetchall(
            "SELECT launch_epoch FROM sessions WHERE session_id = ?",
            (session.session_id,),
        )
        epoch = (rows[0]["launch_epoch"] + 1) if rows else 0
        graph_state = json.dumps(session.cxt.graph_state or {},
                                 ensure_ascii=False)
        await self._conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, pattern_code, launch_epoch, request_id, task_info,
                current_node_code, graph_state, filled_slots,
                created_at, last_active_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                session.pattern_code,
                epoch,
                request_id,
                task_info,
                session.cxt.current_node_code,
                graph_state,
                filled_slots,
                now,
                now,
            ),
        )
        await self._conn.commit()

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

        The sink is a coroutine function (``add_message`` awaits it since the
        asyncio rewrite); the lambda returns the coroutine un-run.

        trace_sink gets the same wiring for the append-only trace trail: the
        engine's turn task fans each kind="trace" event to it (fire-and-forget
        on the engine side, serialized per turn by a single writer).
        """
        session.cxt.message_sink = (
            lambda msg: self.append_message(session, msg))
        session.cxt.trace_sink = (
            lambda ev: self.append_trace(session, ev))

    async def save_snapshot(
            self, session: Session,
            expected_epoch: Optional[int] = None) -> None:
        """Write back the end-of-turn sessions state snapshot
        (module/node/slots/last-active time).

        Message appending has moved to ``append_message`` (per-message
        write-through once attached); this method no longer touches the
        messages table — one end-of-turn transaction to write back state.

        ``expected_epoch``: generation guard — pass the session's
        launch_epoch as of the start of this turn; if the row's epoch has
        since been bumped by a relaunch (the session was evicted and then
        re-registered), abandon the write — a stale-generation end-of-turn
        snapshot must not overwrite the new generation's state. ``None``
        keeps the old semantics (unconditional write).
        """
        now = time.time()
        filled_slots = json.dumps(session.cxt.filled_slots or {}, ensure_ascii=False)
        graph_state = json.dumps(session.cxt.graph_state or {},
                                 ensure_ascii=False)
        if expected_epoch is None:
            await self._conn.execute(
                """UPDATE sessions
                   SET current_node_code = ?, graph_state = ?,
                       filled_slots = ?, last_active_at = ?
                   WHERE session_id = ?""",
                (
                    session.cxt.current_node_code,
                    graph_state,
                    filled_slots,
                    now,
                    session.session_id,
                ),
            )
        else:
            cursor = await self._conn.execute(
                """UPDATE sessions
                   SET current_node_code = ?, graph_state = ?,
                       filled_slots = ?, last_active_at = ?
                   WHERE session_id = ? AND launch_epoch = ?""",
                (
                    session.cxt.current_node_code,
                    graph_state,
                    filled_slots,
                    now,
                    session.session_id,
                    expected_epoch,
                ),
            )
            if cursor.rowcount == 0:
                logger.debug(
                    "跳过陈旧代际的轮末快照: session=%s expected_epoch=%s",
                    session.session_id, expected_epoch)
                return
        await self._conn.commit()

    async def current_epoch(self, session_id: str) -> int:
        """Public read of the session's current launch generation (0 if no row)."""
        return await self._current_epoch(session_id)

    # ------------------------------------------------------------------
    # Per-message write-through (DB is the source of truth) + compression primitives
    # ------------------------------------------------------------------

    async def _current_epoch(self, session_id: str) -> int:
        """Return the session's current-generation epoch (0 if no row)."""
        rows = await self._conn.execute_fetchall(
            "SELECT launch_epoch FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        return rows[0]["launch_epoch"] if rows else 0

    async def append_message(self, session: Session, msg: SessionMessage) -> None:
        """Persist a single message immediately (write side of message_sink,
        triggered per add_message).

        The epoch is looked up at write time (same idiom as save_snapshot): an
        in-flight turn of a session evicted from memory and re-launched lands
        in the new generation — same kind of deviation as the existing batch
        write, not introduced here.
        """
        epoch = await self._current_epoch(session.session_id)
        await self._conn.execute(
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
        await self._conn.commit()

    async def append_trace(self, session: Session, ev: Dict[str, Any]) -> None:
        """Persist one trace event immediately (write side of trace_sink).

        Shape of ``ev`` (built by the engine's per-turn choke point):
        ``{session_id, turn_id, kind, payload}`` where payload is a
        JSON-ready dict of the TraceEvent's remaining fields. The epoch is
        looked up at write time (same idiom as append_message) and the
        payload is capped: over-limit or base64-carrying payloads are stored
        as a ``{"_truncated": true, "_preview": ...}`` placeholder — audit
        rows stay lightweight text, the flag keeps the cut visible.
        """
        epoch = await self._current_epoch(session.session_id)
        raw = json.dumps(ev.get("payload") or {}, ensure_ascii=False,
                         default=str)
        truncated = 0
        if len(raw) > _TRACE_PAYLOAD_LIMIT or _DATA_URI_RE.search(raw):
            raw = json.dumps(
                {"_truncated": True, "_preview": raw[:_TRACE_PAYLOAD_LIMIT]},
                ensure_ascii=False)
            truncated = 1
        await self._conn.execute(
            """INSERT INTO trace_events
               (session_id, turn_id, launch_epoch, kind, payload, truncated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                ev.get("turn_id") or "",
                epoch,
                ev.get("kind") or "",
                raw,
                truncated,
                time.time(),
            ),
        )
        await self._conn.commit()

    async def get_history(self, session_id: str) -> List[SessionMessage]:
        """Rebuild all messages of the current epoch in ascending id order
        (used for the DB/memory alignment check before compression).

        Tool-call traces round-trip verbatim inside the content/metadata
        payloads (no dedicated columns needed).
        """
        epoch = await self._current_epoch(session_id)
        rows = await self._conn.execute_fetchall(
            """SELECT role, content, stage, metadata
               FROM messages WHERE session_id = ? AND launch_epoch = ?
               ORDER BY id""",
            (session_id, epoch),
        )
        return [
            SessionMessage(
                role=r["role"],
                content=r["content"],
                stage=r["stage"],
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]

    async def replace_history(
        self, session: Session, summary_text: str, keep_idx: int
    ) -> None:
        """Compression rewrite: delete all rows of the current epoch → insert
        the summary row → re-insert ``history[keep_idx:]``.

        First checks the DB row count against ``len(cxt.history)`` and raises
        ``RuntimeError`` on mismatch (nothing has been written at that point,
        so a plain raise leaves the DB untouched); the caller catches it and
        abandons compression — out-of-sync history is never deleted.
        The delete+insert pair runs as one explicit transaction: a mid-way
        failure (or cancellation) rolls the DELETE back, so it can never
        dangle uncommitted and get silently committed by the next
        append_message. Retained rows get renumbered ids
        (AUTOINCREMENT cannot insert before existing rows); the summary
        naturally sorts first.
        """
        now = time.time()
        epoch = await self._current_epoch(session.session_id)
        count_rows = await self._conn.execute_fetchall(
            "SELECT COUNT(*) AS n FROM messages"
            " WHERE session_id = ? AND launch_epoch = ?",
            (session.session_id, epoch),
        )
        if count_rows[0]["n"] != len(session.cxt.history):
            raise RuntimeError(
                f"DB/内存消息数不齐，放弃压缩: session={session.session_id}"
                f" db={count_rows[0]['n']} mem={len(session.cxt.history)}"
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
        # Explicit transaction (`async with self._conn` cannot be used:
        # aiosqlite's __aenter__ would re-await the connection and start
        # the worker thread a second time). DELETE/INSERT implicitly open
        # a transaction; a mid-way failure (cancellation included) rolls
        # everything back, only success commits.
        try:
            await self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND launch_epoch = ?",
                (session.session_id, epoch),
            )
            await self._conn.executemany(
                """INSERT INTO messages
                   (session_id, launch_epoch, role, content, stage, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                await self._conn.rollback()
            raise
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Restart restore
    # ------------------------------------------------------------------

    async def load_active_sessions(self, ttl_seconds: float) -> List[Tuple[Session, float]]:
        """Load sessions whose ``last_active_at`` has not expired (for startup
        restore).

        Returns:
            List of ``(Session, wall-clock last_active_at)``. Session.pattern
            is None and node_map/module_map are empty — resolved and injected
            by the caller from the registries.
        """
        cutoff = time.time() - ttl_seconds
        rows = await self._conn.execute_fetchall(
            "SELECT * FROM sessions WHERE last_active_at >= ?"
            " AND launch_epoch = (SELECT MAX(launch_epoch) FROM sessions s2"
            "                      WHERE s2.session_id = sessions.session_id)"
            " ORDER BY last_active_at DESC",
            (cutoff,),
        )
        restored: List[Tuple[Session, float]] = []
        for row in rows:
            msgs = await self._conn.execute_fetchall(
                "SELECT role, content, stage, metadata FROM messages"
                " WHERE session_id = ? AND launch_epoch = ? ORDER BY id",
                (row["session_id"], row["launch_epoch"]),
            )
            session = Session(
                session_id=row["session_id"],
                pattern_code=row["pattern_code"],
            )
            session.task_info = json.loads(row["task_info"] or "{}")
            session.cxt.metadata["task_info"] = session.task_info
            session.cxt.metadata["request_id"] = row["request_id"]
            session.cxt.current_node_code = row["current_node_code"]
            session.cxt.graph_state = json.loads(row["graph_state"] or "{}")
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

    async def list_sessions(
        self,
        pattern_code: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        session_id_contains: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Session list (descending by last_active_at), with message counts.

        ``session_id_contains`` narrows by a substring of session_id
        (LIKE with ``%``/``_`` escaped — user input stays literal).
        """
        sql = """
            SELECT s.session_id, s.pattern_code, s.launch_epoch,
                   s.current_node_code, s.graph_state,
                   s.created_at, s.last_active_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id)
                       AS message_count
            FROM sessions s
        """
        params: List[Any] = []
        wheres: List[str] = []
        if pattern_code:
            wheres.append("s.pattern_code = ?")
            params.append(pattern_code)
        if session_id_contains:
            escaped = (session_id_contains
                       .replace("\\", "\\\\")
                       .replace("%", "\\%")
                       .replace("_", "\\_"))
            wheres.append("s.session_id LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY s.last_active_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self._conn.execute_fetchall(sql, params)
        return [dict(r) for r in rows]

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Single session summary row (same shape as ``list_sessions`` rows,
        plus request_id/task_info/filled_slots); None if it does not exist."""
        rows = await self._conn.execute_fetchall(
            """SELECT s.session_id, s.pattern_code, s.launch_epoch,
                      s.request_id, s.task_info, s.current_node_code,
                      s.graph_state, s.filled_slots,
                      s.created_at, s.last_active_at,
                      (SELECT COUNT(*) FROM messages m
                       WHERE m.session_id = s.session_id) AS message_count
               FROM sessions s WHERE s.session_id = ?""",
            (session_id,),
        )
        if not rows:
            return None
        return dict(rows[0])

    async def get_messages(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """All messages of a session across every generation (with launch_epoch;
        ascending by id); None if the session does not exist.
        """
        exists_rows = await self._conn.execute_fetchall(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        )
        if not exists_rows:
            return None
        rows = await self._conn.execute_fetchall(
            """SELECT id, launch_epoch, role, content, stage, metadata, created_at
               FROM messages WHERE session_id = ? ORDER BY id""",
            (session_id,),
        )
        messages = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d["metadata"] or "{}")
            messages.append(d)
        return messages

    async def get_trace_events(
        self,
        session_id: str,
        turn_id: Optional[str] = None,
        limit: int = 200,
        after_id: int = 0,
    ) -> Optional[List[Dict[str, Any]]]:
        """Trace trail of a session, ascending by id (the id IS the global
        ordering); optional turn filter + id-cursor pagination (``after_id``
        exclusive). Payload round-trips as a parsed dict. None if the
        session does not exist (mirrors ``get_messages``).
        """
        exists_rows = await self._conn.execute_fetchall(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        )
        if not exists_rows:
            return None
        sql = """SELECT id, turn_id, launch_epoch, kind, payload, truncated, created_at
                 FROM trace_events WHERE session_id = ?"""
        params: List[Any] = [session_id]
        if turn_id:
            sql += " AND turn_id = ?"
            params.append(turn_id)
        if after_id > 0:
            sql += " AND id > ?"
            params.append(after_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        rows = await self._conn.execute_fetchall(sql, params)
        events = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {"_unparsable": str(d["payload"])[:512]}
            d["truncated"] = bool(d["truncated"])
            events.append(d)
        return events
