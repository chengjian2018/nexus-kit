"""MCP tool bootstrap — import side effect: read the ``mcp_servers``
config, record each server for background connection (the connections are
spawned by ``ensure_started``), and dynamically register
the tools into ToolRegistry (toolset=``mcp-<server>``).

The module-level ``registry.register(mcp_list_tools)`` at the top of this
file is the AST discovery anchor (discover_builtin_tools only imports
modules carrying a top-level registry.register call); the
``bootstrap_mcp()`` right after it is fully wrapped in try/except — a
missing config / missing sdk / broken config file all degrade to a
warning + no-op, never blocking import or host startup.

See the ``mcp_servers:`` comment block in
host/config/local_config.yaml for a real server config example
(transport supports stdio / sse / streamable_http).
"""

import logging
from typing import Any, Dict

from nexus.registry.tools import registry, tool_result

logger = logging.getLogger(__name__)


# ============================================================================
# Observation tool: list each MCP server's connection state and tools
# ============================================================================

MCP_LIST_TOOLS_SCHEMA = {
    "name": "mcp_list_tools",
    "description": (
        "列出当前已配置 MCP server 的连接状态与各自注册的工具清单"
        "(观测用,不调用任何 server)"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def _handle_mcp_list_tools(args: Dict[str, Any]) -> str:
    from atoms.mcp.manager import get_mcp_manager

    servers = get_mcp_manager().list_servers()
    if not servers:
        return tool_result(
            {"servers": [],
             "message": "未配置任何 MCP server(local_config.yaml 的 mcp_servers 节点为空)"})
    return tool_result({"servers": servers, "count": len(servers)})


registry.register(
    name="mcp_list_tools",
    toolset="mcp",
    schema=MCP_LIST_TOOLS_SCHEMA,
    handler=_handle_mcp_list_tools,
    description=MCP_LIST_TOOLS_SCHEMA["description"],
    emoji="🔌",
)


# ============================================================================
# bootstrap — import side effect: start background connections when configured
# ============================================================================

def bootstrap_mcp() -> None:
    """Read the ``mcp_servers`` config and record the servers in the MCP
    manager (the background connections are spawned by ``ensure_started``).

    No-op across the whole chain when: ``NEXUS_MCP_DISABLED`` is set (test
    isolation — pytest run from the repo root would probe CWD into the
    real local_config.yaml, and every test process would genuinely connect
    to servers otherwise) / no server configured (returns early) / config
    file load fails (warning) / mcp sdk not installed (errors inside the
    manager). Never raises — this function runs on the import path of
    ``discover_builtin_tools``; a failure must not sink startup.
    """
    import os

    if os.environ.get("NEXUS_MCP_DISABLED"):
        logger.info("[mcp] NEXUS_MCP_DISABLED 已置位,跳过 MCP bootstrap(测试隔离)")
        return

    try:
        from nexus.settings import get_mcp_servers

        servers_cfg = get_mcp_servers()
    except Exception as e:  # noqa: BLE001 -- defensive tolerance on the import path
        logger.warning("[mcp] 读取 mcp_servers 配置失败,MCP 工具不启用: %s", e)
        return

    if not servers_cfg:
        return

    try:
        from atoms.mcp.manager import get_mcp_manager

        get_mcp_manager().bootstrap(servers_cfg)
    except Exception as e:  # noqa: BLE001 -- same as above: startup must not be dragged down by MCP
        logger.error("[mcp] bootstrap 失败(忽略): %s", e)


async def ensure_mcp_ready(timeout: float = 15.0) -> None:
    """Wait for the configured MCP servers to reach a connection final
    state (consumer-side timing gate).

    Background: bootstrap only records the config; connections complete
    asynchronously via tasks spawned by ensure_started, so the moment tool
    registration finishes is undefined. If the first dialogue turn resolves
    tools before registration completes (e.g. deep_research's
    _resolve_tools), allowed_names gets frozen without the MCP tools, and
    any later model reference trips the "not in this turn's available set"
    guard. The agent executor calls this before resolving tools — with no
    server configured it returns immediately, with everything ready it
    also returns immediately; only the first turn inside the startup race
    window truly waits (capped at timeout).

    Fallback self-healing: if the host forgot to mount ensure_started at
    startup, wait_ready here spawns the connections first (see
    manager.wait_ready).
    """
    try:
        from atoms.mcp.manager import get_mcp_manager

        await get_mcp_manager().wait_ready(timeout=timeout)
    except Exception as e:  # noqa: BLE001 -- a timing gate must never block the dialogue
        logger.warning("[mcp] 等待就绪失败(忽略,继续解析工具): %s", e)


bootstrap_mcp()
