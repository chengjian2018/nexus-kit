"""MCP tool bootstrap — import 副作用:读 ``mcp_servers`` 配置,后台连接
各 server 并把工具动态注册进 ToolRegistry(toolset=``mcp-<server>``)。

本文件顶层的 ``registry.register(mcp_list_tools)`` 是 AST 发现锚点
(discover_builtin_tools 只 import 带顶层 registry.register 调用的模块);
紧随其后的 ``bootstrap_mcp()`` 全程 try/except 包裹——配置缺失 / sdk 未装
/ 配置文件损坏都降级为 warning + no-op,绝不阻塞 import 或 host 启动。

真实 server 配置示例见 host/config/local_config.yaml 的 ``mcp_servers:``
注释块(transport 支持 stdio / sse / streamable_http)。
"""

import logging
from typing import Any, Dict

from nexus.registry.tools import registry, tool_result

logger = logging.getLogger(__name__)


# ============================================================================
# 观测工具:列出各 MCP server 连接状态与已注册工具
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
    allowed_patterns={"*": True},
)


# ============================================================================
# bootstrap — import 副作用:配置非空时启动后台连接
# ============================================================================

def bootstrap_mcp() -> None:
    """读 ``mcp_servers`` 配置并启动 MCP 管理器的后台连接。

    全链路 no-op 条件:``NEXUS_MCP_DISABLED`` 置位(测试隔离——pytest 从
    仓库根运行时 CWD 探测会命中真实 local_config.yaml,不关掉则每个
    测试进程都真连 server)/ 未配置 server(返回前)/ 配置文件加载失败
    (warning)/ mcp sdk 未装(manager 内部报错)。永不 raise——这个函数
    在 ``discover_builtin_tools`` 的 import 路径上执行,失败不能拖垮启动。
    """
    import os

    if os.environ.get("NEXUS_MCP_DISABLED"):
        logger.info("[mcp] NEXUS_MCP_DISABLED 已置位,跳过 MCP bootstrap(测试隔离)")
        return

    try:
        from nexus.settings import get_mcp_servers

        servers_cfg = get_mcp_servers()
    except Exception as e:  # noqa: BLE001 -- import 路径上的防御性容错
        logger.warning("[mcp] 读取 mcp_servers 配置失败,MCP 工具不启用: %s", e)
        return

    if not servers_cfg:
        return

    try:
        from atoms.mcp.manager import get_mcp_manager

        get_mcp_manager().bootstrap(servers_cfg)
    except Exception as e:  # noqa: BLE001 -- 同上,启动不被 MCP 拖垮
        logger.error("[mcp] bootstrap 失败(忽略): %s", e)


def ensure_mcp_ready(timeout: float = 15.0) -> None:
    """等待已配置的 MCP server 到达连接终态(消费者侧时序闸)。

    背景:bootstrap 只 spawn 后台连接任务,工具注册完成时刻不定;首个
    对话轮若抢在注册完成之前解析工具(如 deep_research 的
    _resolve_tools),allowed_names 会被冻结成不含 MCP 工具的集合,模型
    后续引用即触发"不在本轮可用集合"拦截。agent executor 在解析工具前
    调用本函数——未配置 server 时立即返回,全部就绪时也立即返回,只有
    启动竞态窗口内的首轮会真正等待(上限 timeout)。
    """
    try:
        from atoms.mcp.manager import get_mcp_manager

        get_mcp_manager().wait_ready(timeout=timeout)
    except Exception as e:  # noqa: BLE001 -- 时序闸绝不能阻塞对话
        logger.warning("[mcp] 等待就绪失败(忽略,继续解析工具): %s", e)


bootstrap_mcp()
