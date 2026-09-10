"""Async test helpers.

The framework is async-only (asyncio rewrite); tests stay plain synchronous
pytest — async entry points are driven through ``arun`` (a fresh
``asyncio.run`` per call). tests/ is on sys.path via conftest, so the import
idiom matches ``from fake_provider import ...``.
"""

import asyncio


def arun(coro):
    """Run a coroutine to completion on a fresh event loop (sync bridge for
    tests). MCP is disabled under pytest (conftest), so no cross-loop
    connection state is carried between arun calls."""
    return asyncio.run(coro)
