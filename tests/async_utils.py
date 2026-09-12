"""Async test helpers.

The framework is async-only (asyncio rewrite); tests stay plain synchronous
pytest — async entry points are driven through ``arun``.

arun shares ONE background event loop for the whole test process: a store
connection (aiosqlite) binds its worker thread to the loop it was created
on, and aiosqlite cannot restart that worker across ``asyncio.run``
boundaries ("threads can only be started once") — so per-call fresh loops
would break any stateful object that survives between calls (stores, MCP
connections, asyncio.Locks under contention). The shared loop mirrors
production shape (uvicorn's single main loop) while keeping tests
synchronous. MCP is disabled under pytest (conftest), so the loop stays
purely local.
"""

import asyncio
import concurrent.futures
import os
import threading

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()

# Audit M-25: when the coroutine under test hangs, a timeout-less .result()
# would hang the entire pytest process instead of failing. Default 30s
# (tunable via NEXUS_TEST_ARUN_TIMEOUT) — far above any normal case; only a
# backstop for coroutines that never complete.
_DEFAULT_TIMEOUT = float(os.environ.get("NEXUS_TEST_ARUN_TIMEOUT", "30"))


def _shared_loop() -> asyncio.AbstractEventLoop:
    """The process-wide background loop (lazily started, daemon thread)."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="pytest-arun-loop",
                             daemon=True).start()
        return _loop


def arun(coro, timeout: float | None = None):
    """Run a coroutine to completion on the shared background loop (sync
    bridge for tests).

    Raises AssertionError on timeout (with the coroutine named for
    diagnostics) instead of hanging the pytest process forever.
    """
    timeout = _DEFAULT_TIMEOUT if timeout is None else timeout
    fut = asyncio.run_coroutine_threadsafe(coro, _shared_loop())
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise AssertionError(
            f"arun timed out after {timeout}s — the coroutine likely never "
            f"completed (hung await?): {coro}"
        )
