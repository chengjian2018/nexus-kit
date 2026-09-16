"""System hot-reload API tests (host/main.py: /api/v1/system/*) — offline.

Endpoint semantics: ``GET /status`` = plugins (kind/code/source/owner
module) + MCP servers + the toolset overview; ``POST /reload`` = the
checked plugins reload routed by owner (studio-hosted replayed by file /
code plugins via reload_modules in dependency order + session rebinding)
plus an optional MCP tool-surface rebuild (re-read config → shutdown →
bootstrap → ensure_started → wait_ready). get_mcp_manager (recording call
order) and settings.get_mcp_servers are stubbed throughout; zero real
connections.
"""

import pytest
from fastapi.testclient import TestClient

import atoms.mcp.manager as mcp_module
import host.main as main


class _StubManager:
    """A minimal McpManager stub recording call order (sync bootstrap, async elsewhere — matching the real one)."""

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
    # TestClient without with (startup not triggered): no real MCP connection tasks are started
    return TestClient(main.app)


# A self-contained studio-hosted plugin (owner = studio_plugin_sys_reload_demo)
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
    """A tmp hosted directory + one loaded studio plugin (no dependence on repo-local artifacts)."""
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
    # Code plugins (the atoms/executors default executor) are owned by their module
    assert plugins[("executor", "default_loop")]["source"] == "code"
    assert plugins[("executor", "default_loop")]["module"] \
        == "atoms.executors.loop_executor"
    # Kernel default implementations are marked kernel and not hot-reloadable
    assert plugins[("messages_builder", "default")]["source"] == "kernel"


# ---------------------------------------------------------------------------
# POST /api/v1/system/reload: plugins
# ---------------------------------------------------------------------------

def test_system_reload_studio_plugin_replays_file(client, stub,
                                                  hosted_studio_plugin):
    body = client.post("/api/v1/system/reload",
                       json={"plugin_codes": ["executor:sys_reload_demo"]}).json()
    assert body["code"] == "0" and body["status"] is True
    report = body["data"]["report"]
    assert report["studio_plugins"]["reloaded"] == ["sys_reload_demo"]
    assert not report["studio_plugins"]["failed"]
    # After replay the registration survives (the replace window absorbs the new class objects)
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
# POST /api/v1/system/reload: the MCP tool surface
# ---------------------------------------------------------------------------

def test_system_reload_mcp_rebuilds_with_fresh_config(client, stub, mcp_cfg):
    body = client.post("/api/v1/system/reload", json={"mcp": True}).json()
    assert body["code"] == "0" and body["status"] is True
    mcp = body["data"]["report"]["mcp"]
    assert mcp["ready"] == 1 and len(mcp["servers"]) == 1
    assert "1/1" in body["message"]
    names = [c[0] for c in stub.calls]
    assert names == ["shutdown", "bootstrap", "ensure_started", "wait_ready"]
    assert stub.calls[1][1] is mcp_cfg["value"]  # bootstrap received the re-read fresh config


def test_system_reload_bad_mcp_config_keeps_current(client, stub, mcp_cfg):
    mcp_cfg["raise"] = ValueError("transport 非法: 'xxx'")
    body = client.post("/api/v1/system/reload", json={"mcp": True}).json()
    assert body["code"] == "400" and body["status"] is False
    assert "配置非法" in body["message"]
    # Failing before the connections are torn down — the existing tool surface stays untouched
    assert stub.calls == []


# ---------------------------------------------------------------------------
# Startup assembly validation _validate_registered_patterns: console-hosted patterns' lenient tool surface
# ---------------------------------------------------------------------------

class _StubPatternRegistry:
    """A minimal pattern-registry stub (list_codes/get only; the validation functions read exactly those)."""

    def __init__(self, patterns):
        self._patterns = patterns

    def list_codes(self):
        return list(self._patterns)

    def get(self, code):
        return self._patterns[code]


def _pattern_with_unregistered_tool(code):
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    node = BaseNode(code="n1", name="n1")
    node.use_tools = ["no_such_tool_xyz"]
    return Pattern(code=code, name=code, description="测试",
                   pattern_type="fsm", nodes=[node])


def test_startup_validation_lenient_for_console_patterns(tmp_path, monkeypatch):
    # The publish chain (store.load_pattern_text) leniently allows
    # unregistered tools, so startup validation must be equally lenient —
    # otherwise "publish OK → restart SystemExit bricks the host"; code
    # versions of patterns stay strict
    (tmp_path / "pub.yml").write_text("placeholder: 1", encoding="utf-8")
    monkeypatch.setattr("ui.studio.store.PATTERNS_DIR", tmp_path)

    both = _StubPatternRegistry({
        "pub": _pattern_with_unregistered_tool("pub"),          # console-hosted
        "code_pat": _pattern_with_unregistered_tool("code_pat")  # pure code version
    })
    monkeypatch.setattr(main, "pattern_registry", both)
    with pytest.raises(SystemExit):
        main._validate_registered_patterns()   # the code version is strict → startup aborts

    only_console = _StubPatternRegistry(
        {"pub": _pattern_with_unregistered_tool("pub")})
    monkeypatch.setattr(main, "pattern_registry", only_console)
    main._validate_registered_patterns()       # the console version is lenient → passes without raising


def test_system_reload_replays_console_after_code_modules(client, stub,
                                                           monkeypatch):
    # reload_modules replaying code modules re-registers the code versions
    # of patterns, silently overwriting console edits — replaying the console
    # hosted directories must happen after code-module reloads and before
    # session rebinding
    import host.reload as reload_module
    import ui.studio.store as studio_store

    seq = []

    def _fake_reload(mods):
        seq.append("reload")
        return {"changed": mods, "reloaded": mods, "failed": []}

    def _fake_replay(*args, **kwargs):
        seq.append("replay")
        return {"plugins": {"loaded": [], "failed": {}},
                "patterns": {"loaded": [], "failed": {}}}

    monkeypatch.setattr(reload_module, "reload_modules", _fake_reload)
    monkeypatch.setattr(studio_store, "load_console_artifacts", _fake_replay)

    body = client.post("/api/v1/system/reload",
                       json={"plugin_codes": ["executor:default_loop"]}).json()
    assert body["code"] == "0" and body["status"] is True
    assert seq == ["reload", "replay"]
    cm = body["data"]["report"]["code_modules"]
    assert cm["console_replayed"] == 0
    assert "console_replay_failed" not in cm


def test_discover_tracks_atoms_hooks_modules():
    # atoms.hooks.* and atoms.executors.* are both module-level registered
    # plugins — the discovery domain must cover them, otherwise checking
    # tool_guard on the studio system-plugins page for reload would silently
    # land in "unknown"
    import atoms.hooks.tool_guard  # noqa: F401 -- forces the import into sys.modules
    from host.reload import _discover_module_names

    names = _discover_module_names()
    assert "atoms.hooks.tool_guard" in names
    assert "atoms.executors.loop_executor" in names
    assert not any(n.startswith("nexus.") for n in names)
