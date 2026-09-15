"""In-flight turn task registry — host-owned, per session (session_id → tasks).

Turn tasks are owned by the host since the SSE detach redesign
(docs/design/session-persistence.md §5): a consumer disconnect detaches the
*consumption*, not the *turn* — the engine's turn task keeps running in the
background, and the SSE generator's exit neither cancels nor awaits it. That
orphans the task from any caller reference, so something must hold the strong
reference (asyncio only weakly references running tasks) — that is this
registry. The same table answers the three governance questions:

- governor TTL/LRU eviction skips sessions listed here (a mid-turn eviction
  would pair the running turn with a fresh Session object / fresh lock);
- re-launch of a session with an in-flight turn is rejected with 409 (the
  epoch bump would orphan the running turn's writes);
- graceful shutdown cancels everything registered here.

Mutations all happen on the host event loop (FastAPI endpoints / startup /
shutdown coroutines) — single-threaded, no lock needed.
"""

import asyncio
import logging
from typing import Dict, Set

logger = logging.getLogger(__name__)


class TurnRegistry:
    """Session-scoped registry of in-flight turn tasks (≤1 normally; a queued
    same-session turn may briefly coexist with the running one)."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Set[asyncio.Task]] = {}

    def register(self, session_id: str, task: asyncio.Task) -> None:
        """Start tracking a turn task. Pair with a done callback that calls
        ``unregister`` — the registry only ever drops entries on completion
        (a detached turn outlives its SSE response by minutes)."""
        self._tasks.setdefault(session_id, set()).add(task)

    def unregister(self, session_id: str, task: asyncio.Task) -> None:
        """Stop tracking (idempotent; safe from done callbacks)."""
        tasks = self._tasks.get(session_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(session_id, None)

    def has_running(self, session_id: str) -> bool:
        return bool(self._tasks.get(session_id))

    def running_session_ids(self) -> Set[str]:
        """Snapshot of protected ids (governor's eviction-shield input)."""
        return {sid for sid, tasks in self._tasks.items() if tasks}

    async def cancel_all(self) -> int:
        """Graceful-shutdown hook: cancel every tracked task and wait for the
        cancellations to land (events/messages already written stay; the
        turn itself is discarded, restart re-runs it per the existing
        semantics). Returns the number of cancelled tasks."""
        tasks = [t for tasks in self._tasks.values() for t in tasks]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("停机取消进行中对话轮 %d 个", len(tasks))
        return len(tasks)
