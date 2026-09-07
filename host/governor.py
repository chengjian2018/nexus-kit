"""Session governance: sliding-TTL expiry + LRU cap over in-memory sessions.

Extracted verbatim from the old main.py governance block. The FastAPI
endpoints stay in host.main and delegate here; dialogue turns run outside the
lock (LLM calls are slow and must not block other requests) — chat re-fetches
the session by session_id internally, so a concurrent eviction mid-turn does
not affect the running dialogue.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple

from nexus.engine.session import Session

logger = logging.getLogger(__name__)

# Session idle expiry (seconds), counted from last activity (launch/chat)
SESSION_TTL_SECONDS = 2 * 60 * 60
# Session cap: when a launch hits it, evict the oldest sessions by
# last-active time
MAX_SESSIONS = 10_000


class SessionGovernor:
    """In-memory session table with sliding-TTL renewal and over-limit LRU eviction."""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS,
                 max_sessions: int = MAX_SESSIONS):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.sessions: Dict[str, Session] = {}
        # session_id -> last-active timestamp (time.monotonic seconds); kept
        # in sync with sessions on insert/remove
        self.last_active: Dict[str, float] = {}
        # Serializes concurrent access to sessions / last_active (launch
        # registration, chat lookup, TTL and over-limit eviction)
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lock-held primitives
    # ------------------------------------------------------------------

    def _touch(self, session_id: str) -> None:
        """Refresh the session's last-active time (sliding renewal; lock held)."""
        self.last_active[session_id] = time.monotonic()

    def _purge_expired(self) -> int:
        """Purge sessions idle beyond ttl_seconds (lock held)."""
        now = time.monotonic()
        expired = [
            sid for sid, ts in self.last_active.items()
            if now - ts > self.ttl_seconds
        ]
        for sid in expired:
            self.sessions.pop(sid, None)
            self.last_active.pop(sid, None)
        if expired:
            logger.info("清理过期会话 %d 个: %s", len(expired), expired)
        return len(expired)

    def _evict_oldest_if_over_limit(self) -> int:
        """Evict the least-recently-active sessions once the count reaches
        max_sessions (lock held), so the total stays within the cap after
        insertion."""
        evicted = []
        while len(self.sessions) >= self.max_sessions and self.last_active:
            oldest_sid = min(self.last_active, key=self.last_active.get)
            self.sessions.pop(oldest_sid, None)
            self.last_active.pop(oldest_sid, None)
            evicted.append(oldest_sid)
        if evicted:
            logger.info(
                "会话数达到上限 %d，逐出最旧会话 %d 个: %s",
                self.max_sessions, len(evicted), evicted,
            )
        return len(evicted)

    # ------------------------------------------------------------------
    # Lock-taking operations
    # ------------------------------------------------------------------

    def register_new(self, session: Session,
                     exist_ok: bool = False) -> Tuple[bool, Optional[Session]]:
        """Atomic purge + duplicate check + eviction + insert.

        Args:
            exist_ok: when True, an already-existing session_id counts as a
                hit and the existing session is returned (channel
                get-or-create semantics) without being overwritten.

        Returns:
            (True, None) — the session was inserted;
            (False, existing) — the session_id already existed (caller maps
            this to success when exist_ok, to 409 otherwise; the existing
            session was touched).
        """
        with self.lock:
            self._purge_expired()
            existing = self.sessions.get(session.session_id)
            if existing is not None:
                self._touch(session.session_id)
                return False, existing
            self._evict_oldest_if_over_limit()
            self.sessions[session.session_id] = session
            self._touch(session.session_id)
            return True, None

    def get(self, session_id: str) -> Optional[Session]:
        """Governance-aware lookup: purge expired, and on a hit refresh the
        last-active time (sliding renewal)."""
        with self.lock:
            self._purge_expired()
            session = self.sessions.get(session_id)
            if session is not None:
                self._touch(session_id)
            return session

    def adopt_restored(self, session: Session, idle_seconds: float) -> None:
        """Insert a session restored from the store, keeping its pre-restart
        last-active time (DB wall-clock converted onto the monotonic base)."""
        with self.lock:
            self.sessions[session.session_id] = session
            self.last_active[session.session_id] = (
                time.monotonic() - idle_seconds
            )
