"""MCP connection manager — the client layer behind toolset ``mcp-<server>``.

Architecture after the asyncio rewrite (the old "dedicated background
thread + dedicated event loop" is gone):

- Connections live directly on the **caller's event loop** (uvicorn's main
  loop). ``ClientSession`` is bound to the loop that created it anyway —
  the framework is async-only now, no cross-thread bridge needed.
- ``bootstrap`` only records state (cannot await at import time): parse
  config + build ``_ServerConn``; the actual connecting is spawned by
  ``ensure_started()`` (asyncio task, no waiting — preserving the
  "startup never blocks" semantics).
- Two mount points: ① the host HTTP service's FastAPI startup;
  ② ``ensure_mcp_ready``'s self-healing fallback (even if the host forgets
  to mount, the first dialogue turn lazily starts the connections).
- ``wait_ready`` uses each conn's ``asyncio.Event`` (set on final state),
  no busy polling.
- Tool handlers are registered as async (``is_async=True``), awaiting
  ``call_tool`` directly — ``ToolRegistry._run_async`` bridge retired.
- ``notifications/tools/list_changed`` is not subscribed in V1: refreshes
  happen via explicit ``arefresh()`` (the hook for ops / cron).

The mcp sdk import is lazy (only on connect): with no sdk installed and no
server configured, importing this module / the no-op bootstrap never
errors — graceful degradation.
"""

import asyncio
import json
import logging
import sys
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Max characters of a single MCP tool result (oversized results truncated,
# so one search cannot blow up the LLM context)
_MAX_TOOL_RESULT_CHARS = 8000

# Default wait seconds for call_tool
_DEFAULT_CALL_TIMEOUT = 120.0


def _truncate(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """Middle-truncate an oversized tool result (keep head and tail, mark
    the omission)."""
    if len(text) <= limit:
        return text
    head, tail = limit // 2, limit // 4
    return f"{text[:head]}\n...[结果过长,已截断 {len(text) - head - tail} 字符]...\n{text[-tail:]}"


def _content_to_text(result: Any) -> str:
    """CallToolResult.content (list[TextContent|ImageContent|EmbeddedResource])
    → plain-text join. Text blocks take ``.text``; non-text blocks get a
    placeholder marker (images/resources must not vanish silently). An
    isError result is wrapped whole as a tool_error JSON (handler contract:
    never raise; errors are normal return values too)."""
    from nexus.registry.tools import tool_error

    if getattr(result, "isError", False):
        parts = [getattr(b, "text", "") or "" for b in (result.content or [])]
        return tool_error("mcp_call_failed", detail="\n".join(p for p in parts if p))

    pieces: List[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            pieces.append(str(text))
        else:
            pieces.append(f"[{type(block).__name__}: 非文本内容已省略]")
    joined = "\n".join(pieces) if pieces else ""
    return _truncate(joined) if joined else json.dumps(
        {"ok": True, "message": "工具执行成功(无文本输出)"}, ensure_ascii=False)


class _ServerConn:
    """Connection state of one MCP server.

    ``stack`` holds the transport + ClientSession's AsyncExitStack — its
    lifetime is the connection's lifetime; ``registered_tools`` records
    this server's tool names registered into ToolRegistry (the teardown
    list for nuke-and-repave refreshes). ``done_event`` is set when the
    connection reaches a final state (success or failure) — the wait
    anchor for ``wait_ready``. ``http_client`` (streamable_http only) is
    the caller-owned httpx client: the sdk never closes a client it did
    not create, so ``_teardown`` owns closing it.
    """

    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.stack: Optional[AsyncExitStack] = None
        self.session: Any = None
        self.registered_tools: List[str] = []
        self.ready = False
        self.error: Optional[str] = None
        # Caller-owned httpx AsyncClient built by _build_transport for
        # streamable_http (None for stdio/sse) — closed in _teardown
        self.http_client: Any = None
        # Reconnect dedup flag: a caller-side-failure-triggered background
        # reconnect is in flight (prevents reconnect storms)
        self.reconnecting = False
        # Final-state event (None = no connection task spawned on this loop yet)
        self.done_event: Optional[asyncio.Event] = None

    @property
    def toolset(self) -> str:
        return f"mcp-{self.name}"


class McpManager:
    """MCP connection manager: asyncio-native (connections live on the
    caller's loop).

    Lifecycle: ``bootstrap`` (sync, pure recording at import time) →
    ``ensure_started`` (async, idempotently spawns connection tasks,
    no waiting) → ``wait_ready`` (async, waits for all final states) →
    ``call_tool`` (await) → ``shutdown`` (async, tears everything down).
    """

    def __init__(self):
        self._servers: Dict[str, _ServerConn] = {}
        self._bootstrapped = False
        self._started = False
        # bootstrap may run during import with no loop (pure recording,
        # needs no loop); the lazy singleton itself is also cross-thread
        # reachable — keep a threading lock guarding dict writes
        self._lock = threading.RLock()
        # Task references spawned by ensure_started (prevent mid-flight GC)
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def bootstrap(self, servers_cfg: Dict[str, Any]) -> None:
        """Startup entry (called as an import side effect): pure recording —
        parse config + build conns.

        Idempotent: subsequent calls are ignored once bootstrapped
        (including with a different config — to change config use
        arefresh / restart the process). No connecting happens here
        (cannot await at import time); the actual spawn is in
        ``ensure_started`` (host startup / ensure_mcp_ready, the two
        mount points).
        """
        with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True

        if not servers_cfg:
            return

        try:
            import mcp  # noqa: F401 -- lazy probe: readable error when the sdk is absent
        except ImportError as e:
            # A loud startup-time notice (not just logger.error — in host
            # service scenarios logs are often swallowed and the user would
            # only see "MCP not registered" without knowing why)
            print(f"[mcp] ⚠️ 已配置 {len(servers_cfg)} 个 MCP server,但当前解释器"
                  f"未安装 mcp sdk(pip install mcp),MCP 工具未启用: {e}",
                  file=sys.stderr)
            logger.error(
                "[mcp] 已配置 %d 个 server 但 mcp sdk 未安装"
                "(pip install mcp): %s", len(servers_cfg), e)
            return

        with self._lock:
            for name, cfg in servers_cfg.items():
                conn = _ServerConn(str(name), dict(cfg))
                self._servers[conn.name] = conn
        logger.info("[mcp] bootstrap: %d 个 server 已登记(连接待 ensure_started) %s",
                    len(servers_cfg), sorted(servers_cfg))

    async def ensure_started(self) -> None:
        """Idempotent start: spawn connection tasks for configured,
        not-yet-connected conns (without waiting for completion).

        Must be called inside an event loop. Mount points: the host HTTP
        service's FastAPI startup / ``ensure_mcp_ready``'s fallback — even
        if the host forgets to mount, the first dialogue turn starts them
        lazily.
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            pending = [c for c in self._servers.values()
                       if c.done_event is None or not c.done_event.is_set()]

        for conn in pending:
            if conn.done_event is None:
                conn.done_event = asyncio.Event()
            elif conn.done_event.is_set() and conn.error is None:
                continue  # already connected successfully
            task = asyncio.create_task(self._connect_and_register(conn))
            self._tasks.append(task)
        if pending:
            logger.info("[mcp] ensure_started: %d 个 server 连接任务已 spawn",
                        len(pending))

    async def wait_ready(self, timeout: float = 30.0) -> None:
        """Wait for every configured server to reach a final state
        (connected or failed; for tests / ops).

        Lazy-host compatible: if ensure_started has not been called yet,
        spawn first, then wait (self-healing).
        """
        await self.ensure_started()
        with self._lock:
            events = [c.done_event for c in self._servers.values()
                      if c.done_event is not None]
        if not events:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(e.wait() for e in events)), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[mcp] wait_ready 超时 %.0fs(继续,工具面按现状解析)",
                           timeout)

    async def arefresh(self, server_name: Optional[str] = None) -> None:
        """Nuke-and-repave refresh: deregister that server's toolset (the
        mcp- prefix hits ToolRegistry.deregister's MCP exemption) →
        reconnect → re-register.

        The explicit replacement for
        ``notifications/tools/list_changed``. A None server_name refreshes
        everything.
        """
        with self._lock:
            targets = ([self._servers[server_name]] if server_name in self._servers
                       else list(self._servers.values()))
        for conn in targets:
            if conn.done_event is None:
                conn.done_event = asyncio.Event()
            asyncio.create_task(self._teardown_and_reconnect(conn))

    async def shutdown(self) -> None:
        """Close all connections (called by the host shutdown hook;
        idempotent).

        Stops no loop — the loop belongs to the caller (uvicorn's main
        loop); this only tears down connections + resets state, allowing a
        restart within the same process.
        """
        with self._lock:
            conns = list(self._servers.values())
            self._started = False
            self._servers.clear()
            self._bootstrapped = False
        for conn in conns:
            try:
                await self._teardown(conn)
            except Exception as e:  # noqa: BLE001 -- shutdown best-effort
                logger.warning("[mcp] shutdown 清理 '%s' 异常(忽略): %s",
                               conn.name, e)

    def list_servers(self) -> List[Dict[str, Any]]:
        """Observation data: [{server, ready, error, tools}] (the data
        source of ``mcp_list_tools``)."""
        with self._lock:
            return [{"server": c.name, "ready": c.ready, "error": c.error,
                     "tools": list(c.registered_tools)}
                    for c in self._servers.values()]

    # ------------------------------------------------------------------
    # Tool invocation (handler contract: async handler, never raises)
    # ------------------------------------------------------------------

    async def call_tool(self, server_name: str, tool_name: str,
                        args: Dict[str, Any],
                        timeout: float = _DEFAULT_CALL_TIMEOUT) -> str:
        """Call a server tool directly on this loop.

        Any failure (not connected / timeout / server error) returns a
        tool_error JSON string — matching the ToolRegistry handler
        contract; the error text reaches the LLM only after
        _sanitize_tool_error scrubbing.
        """
        from nexus.registry.tools import tool_error

        with self._lock:
            conn = self._servers.get(server_name)
        if conn is None or not conn.ready or conn.session is None:
            return tool_error(
                "mcp_server_not_ready",
                server=server_name,
                hint="MCP server 未连接或已断开;可尝试 arefresh 恢复")
        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments=args),
                timeout=timeout)
            return _content_to_text(result)
        except Exception as e:  # noqa: BLE001 -- handler contract: never raise
            logger.warning("[mcp] call_tool %s/%s 失败: %s",
                           server_name, tool_name, e)
            # Session-level self-healing: after a connection-class failure
            # (ConnectTimeout / ResetError / timeout) the underlying session
            # is most likely broken — once SSE's post_writer dies, every
            # subsequent call would just run out its full timeout one by
            # one. Mark not-ready and reconnect in the background, so the
            # next call either fails fast or is already recovered on the
            # new connection
            self._schedule_reconnect(conn)
            return tool_error("mcp_call_exception",
                              detail=str(e)[:500],
                              hint="连接故障,已触发重连;模型可稍后重试该工具")

    # ------------------------------------------------------------------
    # Connect / register (executed on the caller's loop)
    # ------------------------------------------------------------------

    def _schedule_reconnect(self, conn: _ServerConn) -> None:
        """Background reconnect after a caller-side failure (dedup: at most
        one reconnect task per conn at a time).

        Sets not-ready immediately (subsequent calls take the fast-fail
        error instead of dumbly waiting out the timeout); _connect_and_
        register restores ready once reconnected. Reconnect-storm guard:
        the ``reconnecting`` flag is released at task end."""
        if conn.reconnecting:
            return
        conn.reconnecting = True
        conn.ready = False

        async def _run():
            try:
                await self._teardown_and_reconnect(conn)
            finally:
                conn.reconnecting = False

        asyncio.create_task(_run())
        logger.info("[mcp] server '%s' 连接故障,后台重连已启动", conn.name)

    async def _connect_and_register(self, conn: _ServerConn) -> None:
        """Connect one server and register its tools into ToolRegistry.

        Failures never propagate: conn.error is marked (observable),
        other servers are unaffected.
        """
        try:
            await self._teardown(conn)
            stack = AsyncExitStack()
            transport_cm = self._build_transport(conn)
            streams = await stack.enter_async_context(transport_cm)
            read_stream, write_stream = streams[0], streams[1]
            session = await stack.enter_async_context(
                _client_session(read_stream, write_stream))
            await session.initialize()
            conn.stack, conn.session = stack, session

            tools_result = await session.list_tools()
            self._register_server_tools(conn, tools_result.tools or [])
            conn.ready, conn.error = True, None
            logger.info("[mcp] server '%s' 连接成功,注册 %d 个工具: %s",
                        conn.name, len(conn.registered_tools),
                        conn.registered_tools)
        except Exception as e:  # noqa: BLE001 -- one server's failure must not sink the rest
            conn.ready, conn.error = False, str(e)
            logger.error("[mcp] server '%s' 连接失败: %s", conn.name, e)
        finally:
            if conn.done_event is not None:
                conn.done_event.set()

    async def _teardown_and_reconnect(self, conn: _ServerConn) -> None:
        """For arefresh: tear down the connection (deregistering its tools)
        then reconnect."""
        await self._teardown(conn)
        await self._connect_and_register(conn)

    async def _teardown(self, conn: _ServerConn) -> None:
        """Tear down the connection + deregister this server's tools
        (nuke-and-repave).

        ``aclose`` carries a 10s timeout: an SSE session's cleanup path can
        hang when the connection is already dead (empirically: while
        grabbing a snapshot, an SSE server's exit hung until process
        timeout) — hanging is worse than erroring: the reconnect task
        would be stuck in teardown forever.

        The conn's http_client (streamable_http) is closed AFTER the stack:
        the transport's exit path may still be using it, and the sdk never
        closes a caller-provided client (leak fix)."""
        stack, conn.stack = conn.stack, None
        conn.session, conn.ready = None, False
        if stack is not None:
            try:
                await asyncio.wait_for(stack.aclose(), timeout=10.0)
            except Exception as e:  # noqa: BLE001 -- teardown best-effort (timeout included)
                logger.debug("[mcp] server '%s' 连接关闭异常/超时(忽略): %s",
                             conn.name, e)
        client, conn.http_client = conn.http_client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=10.0)
            except Exception as e:  # noqa: BLE001 -- teardown best-effort (timeout included)
                logger.debug("[mcp] server '%s' http client 关闭异常/超时(忽略): %s",
                             conn.name, e)
        for name in conn.registered_tools:
            _tool_registry().deregister(name)
        conn.registered_tools = []

    def _build_transport(self, conn: _ServerConn):
        """Build the transport async context manager per config (sdk
        imported at connect time).

        mcp 2.x: all three transports yield (read_stream, write_stream).
        """
        cfg = conn.cfg
        transport = cfg.get("transport")
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=str(cfg["command"]),
                args=[str(a) for a in (cfg.get("args") or [])],
                env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
                or None,
            )
            return stdio_client(params)
        if transport == "sse":
            from mcp.client.sse import sse_client
            return sse_client(url=str(cfg["url"]),
                              headers=cfg.get("headers") or None)
        if transport == "streamable_http":
            # mcp 2.2+: headers go through a pre-configured httpx client
            # (the client factory no longer takes a headers kwarg); pass the
            # client even with empty headers to keep timeout behavior
            # consistent. The AsyncClient now naturally lives on the
            # caller's loop. NOTE: the sdk (mcp 2.2) vendors on the httpx2
            # fork — an httpx 0.x AsyncClient passed here wedges the
            # transport (requests never leave the client), so import httpx2
            # when present and only fall back to httpx for older sdks.
            try:
                import httpx2 as _httpx
            except ImportError:  # pragma: no cover -- older sdk without the fork
                import httpx as _httpx
            from mcp.client.streamable_http import streamable_http_client
            # read=300 mirrors the sdk's own SSE read budget (mcp/shared/
            # _httpx_utils: 30s general / 300s read): the GET event-stream
            # is a long-lived read and server keepalive below 300s is legal
            # — a flat 120s read timeout would kill healthy streams and
            # churn reconnects. Per-call deadline is enforced separately by
            # asyncio.wait_for in call_tool.
            client = _httpx.AsyncClient(
                headers=cfg.get("headers") or None,
                timeout=_httpx.Timeout(30.0, read=300.0))
            # The sdk only closes clients it created itself (client_
            # provided check in streamable_http) — keep the reference on
            # the conn so _teardown closes it (else every reconnect leaks
            # the client + its connection pool).
            conn.http_client = client
            return streamable_http_client(url=str(cfg["url"]), http_client=client)
        raise ValueError(f"mcp server '{conn.name}' transport 非法: {transport!r}")

    def _register_server_tools(self, conn: _ServerConn, mcp_tools) -> None:
        """Register the server's list_tools result into ToolRegistry.

        - Registered name = ``tool_name_prefix + original`` (default keeps
          the original, LLM-friendly; configure a prefix when cross-server
          isolation is needed)
        - toolset = ``mcp-<server>`` (ownership and refresh granularity;
          cross-server name clashes are backstopped by ToolRegistry's
          mcp-→mcp- override exemption; ALSO the authorization unit —
          a pattern grants itself this server's tools by
          listing the toolset in ``pattern.allow_toolset``, the node
          narrows via ``use_tools``; the registration-time allowed_patterns
          ACL is gone)
        - handlers are async (awaiting call_tool directly — after the
          asyncio rewrite, dispatch and connections share one loop; no
          dedicated-thread bridge needed anymore)
        """
        registry = _tool_registry()
        prefix = str(conn.cfg.get("tool_name_prefix", "") or "")

        for t in mcp_tools:
            name = f"{prefix}{t.name}"
            # mcp 2.x field names are snake_case (input_schema); older /
            # third-party description objects may be camelCase
            # (inputSchema) — accept both
            input_schema = (getattr(t, "input_schema", None)
                            or getattr(t, "inputSchema", None))
            schema = {
                "name": name,
                "description": t.description or "",
                "parameters": input_schema or {
                    "type": "object", "properties": {}},
            }
            registry.register(
                name=name,
                toolset=conn.toolset,
                schema=schema,
                handler=_make_handler(conn.name, t.name),
                is_async=True,  # asyncio rewrite: dispatch shares the loop, await directly
                description=t.description or "",
                emoji="🔌",
                max_result_size_chars=_MAX_TOOL_RESULT_CHARS,
            )
            conn.registered_tools.append(name)


def _client_session(read_stream, write_stream):
    """Construct the ClientSession as an async context (lazy sdk import)."""
    from mcp import ClientSession
    return ClientSession(read_stream, write_stream)


def _tool_registry():
    from nexus.registry.tools import registry
    return registry


def _make_handler(server_name: str, tool_name: str):
    """Closure factory: an async handler bound to (server, tool), awaiting
    the manager directly."""
    async def _handler(args: Dict[str, Any]) -> str:
        return await get_mcp_manager().call_tool(server_name, tool_name,
                                                 dict(args or {}))
    return _handler


# ============================================================================
# Module-level lazy singleton
# ============================================================================

_mcp_manager: Optional[McpManager] = None
_singleton_lock = threading.Lock()


def get_mcp_manager() -> McpManager:
    """Lazy singleton (bootstrap / handler bridge / shutdown share one
    instance)."""
    global _mcp_manager
    with _singleton_lock:
        if _mcp_manager is None:
            _mcp_manager = McpManager()
        return _mcp_manager
