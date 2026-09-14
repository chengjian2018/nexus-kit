"""System hot-reload API tests (host/main.py: /api/v1/system/*) — offline.

端点语义：``GET /status`` = 插件（kind/code/来源/归属模块）+ MCP server +
toolset 概览；``POST /reload`` = 勾选插件按归属分流重载（studio 托管按文件
重放 / 代码插件走 reload_modules 依赖序重放 + 会话重绑）+ 可选 MCP 工具面
重建（重读配置 → shutdown → bootstrap → ensure_started → wait_ready）。
MCP 全程打桩 get_mcp_manager（记录调用序）与 settings.get_mcp_servers，
零真实连接。
"""

import pytest
from fastapi.testclient import TestClient

import atoms.mcp.manager as mcp_module
import host.main as main


class _StubManager:
    """记录调用序的最小 McpManager 桩（bootstrap 同步、其余 async，对齐真身）。"""

    def __init__(self):
        self.calls = []
        self.servers = [{"server": "demo", "ready": True, "error": None,
                         "tools": ["t_search"]}]

    def bootstrap(self, cfg):
        self.calls.append(("bootstrap", cfg))

    async def shutdown(self):
        self.calls.append(("shutdown",))

    async def ensure_started(self):
        self.calls.append(("ensure_started",))

    async def wait_ready(self, timeout=30.0):
        self.calls.append(("wait_ready", timeout))

    def list_servers(self):
        return self.servers


@pytest.fixture()
def stub(monkeypatch):
    s = _StubManager()
    monkeypatch.setattr(mcp_module, "get_mcp_manager", lambda: s)
    return s


@pytest.fixture()
def mcp_cfg(monkeypatch):
    holder = {"value": {"demo": {"transport": "streamable_http",
                                 "url": "http://127.0.0.1:9/v1"}}}

    def _get(config_path=""):
        if isinstance(holder.get("raise"), Exception):
            raise holder["raise"]
        return holder["value"]

    monkeypatch.setattr("nexus.settings.get_mcp_servers", _get)
    return holder


@pytest.fixture()
def client():
    # TestClient 不用 with（startup 不触发）：不拉起真实 MCP 连接任务
    return TestClient(main.app)


# 自包含的 studio 托管插件（owner = studio_plugin_sys_reload_demo）
STUDIO_PLUGIN_PY = """\
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.turn_result import TurnResult
from nexus.registry.plugins import registry as plugin_registry


class SysReloadDemo(NodeExecutor):
    async def execute(self, ec):
        return TurnResult(content="hi")


plugin_registry.register("executor", "sys_reload_demo", SysReloadDemo)
"""


@pytest.fixture()
def hosted_studio_plugin(tmp_path, monkeypatch):
    """tmp 托管目录 + 一个已装载的 studio 插件（不依赖仓库本机产物）。"""
    import ui.studio.store as studio_store

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    path = plugins_dir / "sys_reload_demo.py"
    path.write_text(STUDIO_PLUGIN_PY, encoding="utf-8")
    monkeypatch.setattr(studio_store, "PLUGINS_DIR", plugins_dir)
    studio_store.import_plugin_module(path)
    return path


# ---------------------------------------------------------------------------
# GET /api/v1/system/status
# ---------------------------------------------------------------------------

def test_system_status_lists_plugins_servers_toolsets(client, stub):
    body = client.get("/api/v1/system/status").json()
    assert body["code"] == "0" and body["status"] is True
    data = body["data"]
    assert data["servers"] == stub.servers
    assert isinstance(data["toolsets"], dict) and data["toolsets"]
    plugins = {(p["kind"], p["code"]): p for p in data["plugins"]}
    # 代码插件（atoms/executors 默认执行器）归属其模块
    assert plugins[("executor", "default_loop")]["source"] == "code"
    assert plugins[("executor", "default_loop")]["module"] \
        == "atoms.executors.loop_executor"
    # 内核默认实现标记 kernel、不可热重载
    assert plugins[("messages_builder", "default")]["source"] == "kernel"


# ---------------------------------------------------------------------------
# POST /api/v1/system/reload：插件
# ---------------------------------------------------------------------------

def test_system_reload_studio_plugin_replays_file(client, stub,
                                                  hosted_studio_plugin):
    body = client.post("/api/v1/system/reload",
                       json={"plugin_codes": ["executor:sys_reload_demo"]}).json()
    assert body["code"] == "0" and body["status"] is True
    report = body["data"]["report"]
    assert report["studio_plugins"]["reloaded"] == ["sys_reload_demo"]
    assert not report["studio_plugins"]["failed"]
    # 重放后注册仍在（replace 窗口吸收新类对象）
    from nexus.registry.plugins import registry as plugin_registry
    assert plugin_registry.has("executor", "sys_reload_demo")


def test_system_reload_code_plugin_replays_module(client, stub):
    body = client.post("/api/v1/system/reload",
                       json={"plugin_codes": ["executor:default_loop"]}).json()
    assert body["code"] == "0" and body["status"] is True
    cm = body["data"]["report"]["code_modules"]
    assert cm["changed"] == ["atoms.executors.loop_executor"]
    assert "atoms.executors.loop_executor" in cm["reloaded"]
    assert isinstance(cm["sessions_rebound"], int)
    assert "代码模块重放" in body["message"]


def test_system_reload_skips_unknown_and_kernel_refs(client, stub):
    body = client.post("/api/v1/system/reload",
                       json={"plugin_codes": ["executor:nope",
                                              "messages_builder:default"]}).json()
    assert body["code"] == "0"
    report = body["data"]["report"]
    assert len(report["skipped"]) == 2
    assert "studio_plugins" not in report and "code_modules" not in report


def test_system_reload_requires_target(client, stub):
    body = client.post("/api/v1/system/reload", json={}).json()
    assert body["code"] == "400" and body["status"] is False
    assert "未选择" in body["message"]


# ---------------------------------------------------------------------------
# POST /api/v1/system/reload：MCP 工具面
# ---------------------------------------------------------------------------

def test_system_reload_mcp_rebuilds_with_fresh_config(client, stub, mcp_cfg):
    body = client.post("/api/v1/system/reload", json={"mcp": True}).json()
    assert body["code"] == "0" and body["status"] is True
    mcp = body["data"]["report"]["mcp"]
    assert mcp["ready"] == 1 and len(mcp["servers"]) == 1
    assert "1/1" in body["message"]
    names = [c[0] for c in stub.calls]
    assert names == ["shutdown", "bootstrap", "ensure_started", "wait_ready"]
    assert stub.calls[1][1] is mcp_cfg["value"]  # bootstrap 收到重读后的新配置


def test_system_reload_bad_mcp_config_keeps_current(client, stub, mcp_cfg):
    mcp_cfg["raise"] = ValueError("transport 非法: 'xxx'")
    body = client.post("/api/v1/system/reload", json={"mcp": True}).json()
    assert body["code"] == "400" and body["status"] is False
    assert "配置非法" in body["message"]
    # 拆连接之前就失败——现有工具面保持不动
    assert stub.calls == []
