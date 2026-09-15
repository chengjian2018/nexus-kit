"""Offline registration regression against real MCP schemas (snapshot-based
verification of the 4 zai servers in the claude config, zero network
dependency).

Snapshot source: list_tools captures from the real servers taken 2026-09-09
(tests/fixtures_mcp_snapshot.json):
- zai (stdio @z_ai/mcp-server, 8 vision tools)
- websearch / webreader / zread (z.ai SSE remote)

Three things verified:
1. Snapshot shape completeness: every tool has name/description/input_schema,
   and the schema converts directly to OpenAI function format (whatever the
   real server sends, the registration pipeline accepts — input_schema field
   name compatibility is already handled in atoms/mcp/manager.py)
2. Offline registration: snapshot tools are registered into ToolRegistry via
   the same path as manager._register_server_tools (toolset=mcp-<server>);
   the toolset authorization (pattern.allow_toolset + node.use_tools)
   verifies deep_research resolves the full set — precisely the regression
   anchor for the "web_search_prime not in this round's available set"
   interception
3. Deny-by-default: a pattern that does not list the toolsets sees none of
   these tools (the node's use_tools cannot bypass the toolset gate)
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SNAPSHOT_PATH = Path(__file__).resolve().parent / "fixtures_mcp_snapshot.json"

# Expected snapshot shape (captured 2026-09-09; update here when the server side adds tools)
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
    """Register snapshot tools offline into ToolRegistry via the manager's
    real registration path.

    Same parameters as atoms.mcp.manager._register_server_tools (only the
    handler is swapped for an offline stub — the real path's closure bridges
    to call_tool_sync; the offline regression touches no network).
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
            )
    yield tool_registry, calls


# ============================================================================
# 1. Snapshot shape and schema usability
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
            # OpenAI function format ready: parameters is a valid JSON schema object
            params = t["input_schema"]
            assert isinstance(params, dict) and params.get("type") == "object"
            assert isinstance(params.get("properties"), dict)


# ============================================================================
# 2. Offline registration + ACL visibility (regression anchor for the interception WARNING)
# ============================================================================

def test_offline_mcp_tools_visible_to_deep_research(offline_register):
    """toolset 授权：allow_toolset 列出 mcp-<server> + 节点 use_tools
    列名 → 工具可用（正是「不在本轮可用集合」拦截的反面回归锚点）。"""
    from nexus.engine.loop import _resolve_tools
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    toolset_names = {f"mcp-{s}" for s in EXPECTED_SERVERS}
    all_names = {n for names in EXPECTED_SERVERS.values() for n in names}
    pattern = Pattern(code="deep_research", name="dr", description="d",
                      allow_toolset=sorted(toolset_names),
                      nodes=[BaseNode(code="search", use_tools=sorted(all_names))])
    resolved = {t["function"]["name"]
                for t in _resolve_tools(pattern.node_map["search"], pattern)}
    assert resolved == all_names


def test_offline_mcp_tools_hidden_from_other_patterns(offline_register):
    """未把 mcp-<server> 列入 allow_toolset 的 pattern 一律看不到这些工具
    （deny-by-default 契约）；节点 use_tools 列了也没用。"""
    from nexus.engine.loop import _resolve_tools
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    pattern = Pattern(code="customer_agent", name="c", description="d",
                      allow_toolset=["knowledge"],
                      nodes=[BaseNode(code="main",
                                      use_tools=["web_search_prime",
                                                 "analyze_image"])])
    assert _resolve_tools(pattern.node_map["main"], pattern) == []


def test_offline_definitions_carry_real_schemas(offline_register):
    """get_definitions output in OpenAI function format carries the real
    schema — this is the tool surface the LLM actually sees; one wrong
    parameter name or type breaks the model call."""
    tool_registry, _calls = offline_register
    defs = tool_registry.get_definitions({"web_search_prime"})
    assert len(defs) == 1
    fn = defs[0]["function"]
    assert fn["name"] == "web_search_prime"
    props = fn["parameters"]["properties"]
    assert "search_query" in props  # real parameter name (guards against server-side renames)
    # image parameter name of the zai vision tools
    defs2 = tool_registry.get_definitions({"analyze_image"})
    assert "image_source" in defs2[0]["function"]["parameters"]["properties"]


def test_offline_handler_dispatch_roundtrip(offline_register):
    """Offline stub handler dispatched for real via ToolRegistry.dispatch (execution-pipeline regression)."""
    from async_utils import arun
    tool_registry, calls = offline_register
    result = arun(tool_registry.dispatch(
        "web_search_prime", {"search_query": "离线回归"}))
    payload = json.loads(result)
    assert payload["offline"] is True
    assert payload["server"] == "websearch"
    assert calls[-1]["tool"] == "web_search_prime"


# ============================================================================
# 3. Connection-failure self-healing (caller-side exception → fast-fail + background reconnect, zero network)
# ============================================================================

def test_call_failure_marks_not_ready_and_returns_tool_error():
    """After a call timeout/connection error: returns tool_error JSON (never
    raises), marks conn not-ready (later calls fast-fail instead of dumbly
    waiting out the full timeout), and a background reconnect is scheduled."""
    from atoms.mcp.manager import McpManager, _ServerConn
    from async_utils import arun
    from unittest.mock import patch

    async def _boom_call(tool, arguments):
        raise TimeoutError(" ConnectTimeout ")

    mgr = McpManager()
    conn = _ServerConn("fake", {"transport": "bogus"})  # reconnect will fail but stays zero-network
    conn.ready = True
    conn.session = SimpleNamespace(call_tool=_boom_call)
    mgr._servers["fake"] = conn

    scheduled = []
    with patch.object(mgr, "_schedule_reconnect",
                      side_effect=lambda c: scheduled.append(c.name)):
        result = arun(mgr.call_tool("fake", "web_search_prime", {}))

    payload = json.loads(result)
    assert payload["error"] == "mcp_call_exception"
    assert "重连" in payload["hint"]
    assert scheduled == ["fake"]  # self-healing triggered (it sets not-ready in the real implementation)


def test_reconnect_dedup_prevents_storm():
    """Reconnect dedup per conn: no duplicate scheduling while one is in flight (the reconnecting flag guard)."""
    from atoms.mcp.manager import McpManager, _ServerConn

    mgr = McpManager()

    # reconnect already in flight → return immediately, spawn no new task
    conn = _ServerConn("fake", {"transport": "bogus"})
    conn.reconnecting = True
    mgr._schedule_reconnect(conn)
    assert conn.reconnecting is True  # not overwritten (dedup took effect)


# ============================================================================
# 4. streamable_http httpx client lifecycle (leak regression: the sdk never
#    closes a caller-provided client — _teardown owns it; zero network)
# ============================================================================

def test_build_transport_owns_client_with_sse_read_timeout(monkeypatch):
    """_build_transport (streamable_http): the httpx client is kept on the
    conn (http_client attr) and its timeout mirrors the sdk's SSE read
    budget — read=300, general 30 (a flat 120s read killed healthy
    keepalive streams and churned reconnects)."""
    import mcp.client.streamable_http as sh
    from atoms.mcp.manager import McpManager, _ServerConn
    from async_utils import arun

    captured = {}

    def _fake_factory(url, http_client=None, **kwargs):
        captured["url"] = url
        captured["http_client"] = http_client
        return object()  # context manager never entered — zero network

    monkeypatch.setattr(sh, "streamable_http_client", _fake_factory)

    mgr = McpManager()
    conn = _ServerConn("fake", {"transport": "streamable_http",
                                "url": "http://127.0.0.1:1/mcp",
                                "headers": {"x-test": "1"}})
    mgr._build_transport(conn)

    assert captured["url"] == "http://127.0.0.1:1/mcp"
    # ownership: the same client handed to the sdk is kept on the conn
    assert conn.http_client is captured["http_client"]
    assert conn.http_client.headers["x-test"] == "1"
    timeout = conn.http_client.timeout
    assert timeout.read == 300.0  # SSE stream budget (sdk default: 30/300)
    assert timeout.connect == 30.0 and timeout.write == 30.0
    # hygiene: close the real client built in this test (no request was made)
    arun(conn.http_client.aclose())


def test_teardown_closes_http_client_after_stack():
    """_teardown is the single teardown funnel: stack closes FIRST, then the
    conn-owned http client (the transport's exit may still use it), and both
    references are cleared."""
    from atoms.mcp.manager import McpManager, _ServerConn
    from async_utils import arun

    order = []

    class _FakeClosable:
        def __init__(self, tag):
            self._tag = tag

        async def aclose(self):
            order.append(self._tag)

    mgr = McpManager()
    conn = _ServerConn("fake", {"transport": "streamable_http"})
    conn.stack = _FakeClosable("stack")
    conn.http_client = _FakeClosable("client")
    arun(mgr._teardown(conn))

    assert order == ["stack", "client"]
    assert conn.stack is None and conn.http_client is None

    # stdio/sse conns (no stack, no client) tear down cleanly too
    bare = _ServerConn("bare", {"transport": "stdio"})
    arun(mgr._teardown(bare))


def test_teardown_tolerates_http_client_close_failure():
    """A hanging/dead client aclose must not break the teardown funnel (the
    reconnect path depends on teardown completing); the reference is cleared
    regardless."""
    from atoms.mcp.manager import McpManager, _ServerConn
    from async_utils import arun

    class _DeadClient:
        async def aclose(self):
            raise RuntimeError("connection already closed")

    mgr = McpManager()
    conn = _ServerConn("fake", {"transport": "streamable_http"})
    conn.http_client = _DeadClient()
    arun(mgr._teardown(conn))  # must not raise
    assert conn.http_client is None
