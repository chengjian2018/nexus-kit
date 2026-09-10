"""MCP 连接管理器 — toolset ``mcp-<server>`` 背后的客户端层。

架构约束(为什么不用 ToolRegistry._run_async 桥):

- ``ClientSession`` 绑定创建它的 event loop,不能跨 loop 复用;而
  ``ToolRegistry.dispatch`` 对 async handler 走的 ``_run_async`` 每次起一个
  新 loop——两者不兼容。因此本管理器自持「专职后台线程 + 专职 event
  loop」,所有 MCP 操作经 ``asyncio.run_coroutine_threadsafe`` 提交,工具
  handler 用 sync 签名(``is_async=False``)内部桥接。
- 连接生命周期:bootstrap 只 spawn 后台连接任务(不阻塞 import / host
  启动);长连接复用(stdio 子进程 / HTTP session);断线标记 not-ready,
  ``refresh()`` 走 deregister nuke-and-repave(命中 ToolRegistry 的 mcp-
  豁免)后重连重注册。
- ``notifications/tools/list_changed`` V1 不订阅:刷新由显式 ``refresh()``
  触发(运维 / 定时任务的挂点)。

mcp sdk 延迟 import(连接时才 import):未装 sdk 且未配置 server 时,本
模块的 import / no-op bootstrap 都不报错——优雅降级。
"""

import asyncio
import json
import logging
import sys
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 单条 MCP 工具结果的最大字符数(超长截断,防单次搜索结果撑爆 LLM 上下文)
_MAX_TOOL_RESULT_CHARS = 8000

# call_tool_sync 的默认等待秒数
_DEFAULT_CALL_TIMEOUT = 120.0


def _truncate(text: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """超长工具结果中段截断(保头尾,标记省略)。"""
    if len(text) <= limit:
        return text
    head, tail = limit // 2, limit // 4
    return f"{text[:head]}\n...[结果过长,已截断 {len(text) - head - tail} 字符]...\n{text[-tail:]}"


def _content_to_text(result: Any) -> str:
    """CallToolResult.content(list[TextContent|ImageContent|EmbeddedResource])
    → 纯文本拼接。文本块取 .text;非文本块以占位符标注(不让图片/资源
    静默丢失)。isError 结果整体包成 tool_error JSON(handler 契约:永不
    raise,错误也是正常返回值)。"""
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
    """一个 MCP server 的连接态。

    ``stack`` 持有 transport + ClientSession 的 AsyncExitStack,生命周期
    即连接生命周期;``registered_tools`` 记录本 server 注册进 ToolRegistry
    的工具名(refresh 时 nuke-and-repave 的拆除清单)。
    """

    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.stack: Optional[AsyncExitStack] = None
        self.session: Any = None
        self.registered_tools: List[str] = []
        self.ready = False
        self.error: Optional[str] = None
        # 重连去重标记:调用侧故障触发的后台重连进行中(防重连风暴)
        self.reconnecting = False

    @property
    def toolset(self) -> str:
        return f"mcp-{self.name}"


class McpManager:
    """MCP 连接管理器:专职后台线程 + 专职 event loop。

    线程模型:``_loop_thread`` 跑 ``_loop``(专职 asyncio loop),
    ``_ready_event`` 标记 loop 就绪;所有 async 工作以 coroutine 函数经
    ``_submit`` 从任意线程投递。连接任务之间不互相等待(一个 server 失败
    不影响其它),``wait_ready`` 供需要确定性的调用方同步终态。
    """

    def __init__(self):
        self._servers: Dict[str, _ServerConn] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._bootstrapped = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 后台 loop 线程
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.run(self._loop_main())

    async def _loop_main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_ready.set()
        # loop 存活直至 shutdown 投递哨兵
        await asyncio.Event().wait()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """启动专职线程并等 loop 就绪(幂等)。"""
        with self._lock:
            if self._loop_thread is not None and self._loop_thread.is_alive():
                return self._loop
            self._loop_thread = threading.Thread(
                target=self._run_loop, name="mcp-manager-loop", daemon=True)
            self._loop_thread.start()
        self._loop_ready.wait(timeout=10.0)
        if self._loop is None:
            raise RuntimeError("MCP 专职 event loop 启动失败")
        return self._loop

    def _submit(self, coro_fn, *args, timeout: float = _DEFAULT_CALL_TIMEOUT):
        """把 coroutine 工厂投递到专职 loop,阻塞等待结果(异常原样抛)。"""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro_fn(*args), loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def bootstrap(self, servers_cfg: Dict[str, Any]) -> None:
        """启动入口(import 副作用调用):解析配置 + spawn 后台连接任务。

        幂等:已 bootstrap 过则忽略后续调用(含不同配置——改配置请走
        refresh / 重启进程)。同步部分绝不阻塞——stdio 子进程启动 / HTTP
        握手都在后台 loop 里完成,注册完成的工具在之后的 _resolve_tools
        中自然可见(ToolRegistry 的线程安全为 MCP 动态刷新而设计)。
        """
        with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True

        if not servers_cfg:
            return

        try:
            import mcp  # noqa: F401 -- 延迟探测:未装 sdk 时给出可读错误
        except ImportError as e:
            # 显眼的启动期提示(不止 logger.error——CLI 场景日志常被吞,
            # 用户只会看到"MCP 没注册"却不知原因)
            print(f"[mcp] ⚠️ 已配置 {len(servers_cfg)} 个 MCP server,但当前解释器"
                  f"未安装 mcp sdk(pip install mcp),MCP 工具未启用: {e}",
                  file=sys.stderr)
            logger.error(
                "[mcp] 已配置 %d 个 server 但 mcp sdk 未安装"
                "(pip install mcp): %s", len(servers_cfg), e)
            return

        loop = self._ensure_loop()
        for name, cfg in servers_cfg.items():
            conn = _ServerConn(str(name), dict(cfg))
            self._servers[conn.name] = conn
            asyncio.run_coroutine_threadsafe(
                self._connect_and_register(conn), loop)
        logger.info("[mcp] bootstrap: %d 个 server 后台连接中 %s",
                    len(servers_cfg), sorted(servers_cfg))

    def wait_ready(self, timeout: float = 30.0) -> None:
        """等待全部已配置 server 到达终态(连接成功或失败;测试 / 运维用)。"""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if all(c.ready or c.error for c in self._servers.values()):
                    return
            time.sleep(0.05)

    def refresh(self, server_name: Optional[str] = None) -> None:
        """nuke-and-repave 刷新:deregister 该 server toolset(前缀 mcp-,
        命中 ToolRegistry.deregister 的 MCP 豁免)→ 重连 → 重注册。

        ``notifications/tools/list_changed`` 的显式替代。server_name 为
        None 时刷新全部。
        """
        with self._lock:
            targets = ([self._servers[server_name]] if server_name in self._servers
                       else list(self._servers.values()))
        loop = self._ensure_loop()
        for conn in targets:
            asyncio.run_coroutine_threadsafe(
                self._teardown_and_reconnect(conn), loop)

    def shutdown(self) -> None:
        """关闭全部连接并停 loop(host shutdown 钩子调用;幂等)。"""
        with self._lock:
            conns = list(self._servers.values())
            loop, thread = self._loop, self._loop_thread
            self._loop, self._loop_thread = None, None
            self._bootstrapped = False
            self._servers.clear()
        if loop is None or thread is None or not thread.is_alive():
            return

        async def _close_all():
            for conn in conns:
                await self._teardown(conn)
            loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(_close_all(), loop).result(timeout=10.0)
        except Exception as e:  # noqa: BLE001 -- shutdown 尽力而为
            logger.warning("[mcp] shutdown 清理异常(忽略): %s", e)

    def list_servers(self) -> List[Dict[str, Any]]:
        """观测数据:[{server, ready, error, tools}](``mcp_list_tools`` 的数据源)。"""
        with self._lock:
            return [{"server": c.name, "ready": c.ready, "error": c.error,
                     "tools": list(c.registered_tools)}
                    for c in self._servers.values()]

    # ------------------------------------------------------------------
    # 工具调用桥(handler 契约:sync 签名,永不 raise)
    # ------------------------------------------------------------------

    def call_tool_sync(self, server_name: str, tool_name: str,
                       args: Dict[str, Any],
                       timeout: float = _DEFAULT_CALL_TIMEOUT) -> str:
        """同步桥:提交 call_tool 到专职 loop,阻塞等结果。

        任何异常(未连接 / 超时 / server 报错)都返回 tool_error JSON 字符串
        ——符合 ToolRegistry handler 契约,错误信息经 _sanitize_tool_error
        清洗后才到达 LLM。
        """
        from nexus.registry.tools import tool_error

        with self._lock:
            conn = self._servers.get(server_name)
        if conn is None or not conn.ready or conn.session is None:
            return tool_error(
                "mcp_server_not_ready",
                server=server_name,
                hint="MCP server 未连接或已断开;可尝试 refresh 恢复")
        try:
            async def _call():
                return await conn.session.call_tool(tool_name, arguments=args)

            result = self._submit(lambda: _call(), timeout=timeout)
            return _content_to_text(result)
        except Exception as e:  # noqa: BLE001 -- handler 契约:永不 raise
            logger.warning("[mcp] call_tool %s/%s 失败: %s",
                           server_name, tool_name, e)
            # 会话级故障自愈:连接类异常(ConnectTimeout / ResetError /
            # 超时)之后底层 session 大概率已坏——SSE 的 post_writer 死亡
            # 后,后续调用只会一个个等满超时。标记未就绪并后台重连,让
            # 下一次调用要么快速失败、要么已在新连接上恢复
            self._schedule_reconnect(conn)
            return tool_error("mcp_call_exception",
                              detail=str(e)[:500],
                              hint="连接故障,已触发重连;模型可稍后重试该工具")

    # ------------------------------------------------------------------
    # 连接 / 注册(loop 线程内执行)
    # ------------------------------------------------------------------

    def _schedule_reconnect(self, conn: _ServerConn) -> None:
        """调用侧故障后的后台重连(去重:同一 conn 同时只允许一个重连任务)。

        立即置 not-ready(后续调用走 fast-fail 错误而不是傻等超时),
        重连完成后由 _connect_and_register 恢复 ready。重连风暴防线:
        ``reconnecting`` 标志在任务终态时释放。"""
        with self._lock:
            if conn.reconnecting:
                return
            conn.reconnecting = True
            conn.ready = False
        try:
            loop = self._ensure_loop()
        except Exception as e:  # noqa: BLE001 -- 自愈失败不向调用方传播
            conn.reconnecting = False
            logger.warning("[mcp] server '%s' 重连启动失败: %s", conn.name, e)
            return
        future = asyncio.run_coroutine_threadsafe(
            self._teardown_and_reconnect(conn), loop)

        def _release(_fut):
            conn.reconnecting = False

        future.add_done_callback(_release)
        logger.info("[mcp] server '%s' 连接故障,后台重连已启动", conn.name)

    async def _connect_and_register(self, conn: _ServerConn) -> None:
        """连接一个 server 并把它的工具注册进 ToolRegistry。

        失败不抛:标记 conn.error(观测可见),不影响其它 server。
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
        except Exception as e:  # noqa: BLE001 -- 单 server 失败不拖垮整体
            conn.ready, conn.error = False, str(e)
            logger.error("[mcp] server '%s' 连接失败: %s", conn.name, e)

    async def _teardown_and_reconnect(self, conn: _ServerConn) -> None:
        """refresh 用:先拆连接(连带 deregister 工具)再重连。"""
        await self._teardown(conn)
        await self._connect_and_register(conn)

    async def _teardown(self, conn: _ServerConn) -> None:
        """拆除连接 + deregister 该 server 注册的工具(nuke-and-repave)。

        ``aclose`` 带 10s 超时:SSE 会话的清理路径在连接已死时可能挂住
        (实证:抓快照时 SSE server 的退出挂死到进程超时),挂住比报错
        更糟——重连任务会永远卡在 teardown。"""
        stack, conn.stack = conn.stack, None
        conn.session, conn.ready = None, False
        if stack is not None:
            try:
                await asyncio.wait_for(stack.aclose(), timeout=10.0)
            except Exception as e:  # noqa: BLE001 -- teardown 尽力而为(含超时)
                logger.debug("[mcp] server '%s' 连接关闭异常/超时(忽略): %s",
                             conn.name, e)
        for name in conn.registered_tools:
            _tool_registry().deregister(name)
        conn.registered_tools = []

    def _build_transport(self, conn: _ServerConn):
        """按配置构造 transport async 上下文管理器(连接期 import sdk)。

        mcp 2.x:三种 transport 都 yield (read_stream, write_stream)。
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
            # mcp 2.2+: headers 经预配置的 httpx.AsyncClient 传入(客户端
            # 工厂不再收 headers kwarg);headers 为空时仍传 client 以保
            # 证超时行为一致
            import httpx as _httpx
            from mcp.client.streamable_http import streamable_http_client
            client = _httpx.AsyncClient(
                headers=cfg.get("headers") or None, timeout=_DEFAULT_CALL_TIMEOUT)
            return streamable_http_client(url=str(cfg["url"]), http_client=client)
        raise ValueError(f"mcp server '{conn.name}' transport 非法: {transport!r}")

    def _register_server_tools(self, conn: _ServerConn, mcp_tools) -> None:
        """把 server 的 list_tools 结果注册进 ToolRegistry。

        - 注册名 = ``tool_name_prefix + 原名``(默认保留原名,LLM 友好;
          需要跨 server 隔离时配置前缀)
        - toolset = ``mcp-<server>``(归属与刷新粒度;跨 server 撞名由
          ToolRegistry 的 mcp-→mcp- 覆盖豁免兜底)
        - allowed_patterns 从 server 配置转 ACL 形状;缺省 None = deny
          (与 ToolRegistry 的 deny-by-default 一致)
        """
        registry = _tool_registry()
        prefix = str(conn.cfg.get("tool_name_prefix", "") or "")
        allowed = conn.cfg.get("allowed_patterns")
        if allowed is None:
            acl: Optional[Dict[str, Any]] = None
        elif "*" in allowed:
            acl = {"*": True}
        else:
            acl = {p: True for p in allowed}

        for t in mcp_tools:
            name = f"{prefix}{t.name}"
            # mcp 2.x 字段名 snake_case(input_schema);老版本/第三方描述
            # 对象可能是 camelCase(inputSchema)——双名兼容
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
                is_async=False,  # sync 桥:run_coroutine_threadsafe 进专职 loop
                description=t.description or "",
                emoji="🔌",
                max_result_size_chars=_MAX_TOOL_RESULT_CHARS,
                allowed_patterns=acl,
            )
            conn.registered_tools.append(name)


def _client_session(read_stream, write_stream):
    """ClientSession 构造为 async 上下文(延迟 import sdk)。"""
    from mcp import ClientSession
    return ClientSession(read_stream, write_stream)


def _tool_registry():
    from nexus.registry.tools import registry
    return registry


def _make_handler(server_name: str, tool_name: str):
    """闭包工厂:绑定 (server, tool) 的 sync handler,桥到管理器。"""
    def _handler(args: Dict[str, Any]) -> str:
        return get_mcp_manager().call_tool_sync(server_name, tool_name,
                                                dict(args or {}))
    return _handler


# ============================================================================
# 模块级懒单例
# ============================================================================

_mcp_manager: Optional[McpManager] = None
_singleton_lock = threading.Lock()


def get_mcp_manager() -> McpManager:
    """懒单例(bootstrap / handler 桥 / shutdown 共用同一实例)。"""
    global _mcp_manager
    with _singleton_lock:
        if _mcp_manager is None:
            _mcp_manager = McpManager()
        return _mcp_manager
