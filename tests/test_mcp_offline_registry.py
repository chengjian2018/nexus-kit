"""MCP 真实 schema 的离线注册回归(对 claude 配置里 4 个 zai server 的
快照验证,零网络依赖)。

快照来源:2026-09-09 对真实 server 的 list_tools 抓取
(tests/fixtures_mcp_snapshot.json):
- zai(stdio @z_ai/mcp-server,8 视觉工具)
- websearch / webreader / zread(z.ai SSE 远程)

验证三件事:
1. 快照形状完整:每工具 name/description/input_schema 齐、schema 可直接
   转 OpenAI function 格式(真实 server 下发什么,注册管线就吃什么——
   input_schema 字段名兼容已在 atoms/mcp/manager.py 处理)
2. 离线注册:按 manager._register_server_tools 的同款路径把快照工具注册
   进 ToolRegistry(toolset=mcp-<server>,ACL 按 allowed_patterns),经
   get_allowed_tools_for_pattern 验证 deep_research 全量可见——正是
   「web_search_prime 不在本轮可用集合」拦截的回归锚
3. ACL 收窄:server 配置 allowed_patterns 缺省(None)= deny-by-default,
   其它 pattern 看不到这些工具
"""

import json
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).resolve().parent / "fixtures_mcp_snapshot.json"

# 快照的期望形状(2026-09-09 抓取;server 端加工具时更新这里)
EXPECTED_SERVERS = {
    "zai": {"ui_to_artifact", "extract_text_from_screenshot",
            "diagnose_error_screenshot", "understand_technical_diagram",
            "analyze_data_visualization", "ui_diff_check",
            "analyze_image", "analyze_video"},
    "websearch": {"web_search_prime"},
    "webreader": {"webReader"},
    "zread": {"search_doc", "read_file", "get_repo_structure"},
}


def _load_snapshot() -> dict:
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return data


@pytest.fixture()
def offline_register():
    """按 manager 的真实注册路径,把快照工具离线注册进 ToolRegistry。

    与 atoms.mcp.manager._register_server_tools 同款参数(仅 handler 换成
    离线桩——真实路径的闭包桥到 call_tool_sync,离线回归不触网络)。
    """
    from nexus.registry.tools import registry as tool_registry

    snapshot = _load_snapshot()
    calls = []

    def _make_stub_handler(server, tool):
        def _handler(args):
            calls.append({"server": server, "tool": tool, "args": dict(args)})
            return json.dumps({"offline": True, "server": server,
                               "tool": tool}, ensure_ascii=False)
        return _handler

    for server, tools in snapshot.items():
        for t in tools:
            schema = {"name": t["name"], "description": t["description"],
                      "parameters": t["input_schema"]}
            tool_registry.register(
                name=t["name"],
                toolset=f"mcp-{server}",
                schema=schema,
                handler=_make_stub_handler(server, t["name"]),
                is_async=False,
                description=t["description"],
                emoji="🔌",
                allowed_patterns={"deep_research": True},  # server 配置经 _acl_from_cfg 的产物
            )
    yield tool_registry, calls


# ============================================================================
# 1. 快照形状与 schema 可用性
# ============================================================================

def test_snapshot_shape_and_openai_schema_convertible():
    snapshot = _load_snapshot()
    assert set(snapshot) == set(EXPECTED_SERVERS)
    for server, tools in snapshot.items():
        names = {t["name"] for t in tools}
        assert names == EXPECTED_SERVERS[server], (
            f"{server} 工具集与快照不符,server 端可能已变更:"
            f"{names ^ EXPECTED_SERVERS[server]}")
        for t in tools:
            assert t["description"], f"{server}/{t['name']} 缺 description"
            # OpenAI function 格式就绪:parameters 是合法 JSON schema 对象
            params = t["input_schema"]
            assert isinstance(params, dict) and params.get("type") == "object"
            assert isinstance(params.get("properties"), dict)


# ============================================================================
# 2. 离线注册 + ACL 可见性(拦截 WARNING 的回归锚)
# ============================================================================

def test_offline_mcp_tools_visible_to_deep_research(offline_register):
    tool_registry, _calls = offline_register
    visible = tool_registry.get_allowed_tools_for_pattern("deep_research")
    for server, names in EXPECTED_SERVERS.items():
        for name in names:
            assert name in visible, (
                f"{name} 不在 deep_research 可用集合——正是"
                f"「不在本轮可用集合」拦截的复现条件")


def test_offline_mcp_tools_hidden_from_other_patterns(offline_register):
    """server 配置 allowed_patterns: ["deep_research"] → 其它 pattern 全拒
    (deny-by-default 契约)。"""
    tool_registry, _calls = offline_register
    visible = tool_registry.get_allowed_tools_for_pattern("customer_agent")
    assert "web_search_prime" not in visible
    assert "analyze_image" not in visible


def test_offline_definitions_carry_real_schemas(offline_register):
    """get_definitions 输出的 OpenAI function 格式携带真实 schema——
    这是 LLM 实际看到的工具面,参数名/类型错一个模型调用就废。"""
    tool_registry, _calls = offline_register
    defs = tool_registry.get_definitions({"web_search_prime"})
    assert len(defs) == 1
    fn = defs[0]["function"]
    assert fn["name"] == "web_search_prime"
    props = fn["parameters"]["properties"]
    assert "search_query" in props  # 真实参数名(防 server 端改名)
    # zai 视觉工具的图片参数名
    defs2 = tool_registry.get_definitions({"analyze_image"})
    assert "image_source" in defs2[0]["function"]["parameters"]["properties"]


def test_offline_handler_dispatch_roundtrip(offline_register):
    """离线桩 handler 经 ToolRegistry.dispatch 真实分派(执行管线回归)。"""
    from async_utils import arun
    tool_registry, calls = offline_register
    result = arun(tool_registry.dispatch(
        "web_search_prime", {"search_query": "离线回归"}))
    payload = json.loads(result)
    assert payload["offline"] is True
    assert payload["server"] == "websearch"
    assert calls[-1]["tool"] == "web_search_prime"


# ============================================================================
# 3. 连接故障自愈(调用侧异常 → fast-fail + 后台重连,零网络)
# ============================================================================

def test_call_failure_marks_not_ready_and_returns_tool_error():
    """调用超时/连接异常后:返回 tool_error JSON(永不 raise)、conn 置
    not-ready(后续调用 fast-fail 而非傻等满超时)、后台重连被调度。"""
    from atoms.mcp.manager import McpManager, _ServerConn
    from unittest.mock import patch

    mgr = McpManager()
    conn = _ServerConn("fake", {"transport": "bogus"})  # 重连会失败但零网络
    conn.ready = True
    conn.session = object()  # 假 session
    mgr._servers["fake"] = conn

    scheduled = []
    with patch.object(mgr, "_submit", side_effect=TimeoutError(" ConnectTimeout ")), \
         patch.object(mgr, "_schedule_reconnect",
                      side_effect=lambda c: scheduled.append(c.name)):
        result = mgr.call_tool_sync("fake", "web_search_prime", {})

    payload = json.loads(result)
    assert payload["error"] == "mcp_call_exception"
    assert "重连" in payload["hint"]
    assert scheduled == ["fake"]  # 自愈被触发(真实实现里由它置 not-ready)


def test_reconnect_dedup_prevents_storm():
    """同一 conn 的重连去重:进行中不重复调度(reconnecting 标志防线);
    且重连启动失败时标志必须释放(否则一次失败永久卡死自愈)。"""
    from atoms.mcp.manager import McpManager, _ServerConn
    from unittest.mock import patch

    mgr = McpManager()

    # 分支一:重连已在途 → 立即返回,不碰 loop 线程
    conn = _ServerConn("fake", {"transport": "bogus"})
    conn.reconnecting = True
    mgr._schedule_reconnect(conn)
    assert conn.reconnecting is True  # 未被覆盖(去重生效)

    # 分支二:_ensure_loop 启动失败 → reconnecting 必须被释放
    conn2 = _ServerConn("fake2", {"transport": "bogus"})
    conn2.ready = True
    with patch.object(mgr, "_ensure_loop", side_effect=RuntimeError("no loop")):
        mgr._schedule_reconnect(conn2)
    assert conn2.reconnecting is False  # 启动失败已释放
    assert conn2.ready is False
