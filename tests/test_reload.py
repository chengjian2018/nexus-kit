"""Hot-reload mechanism tests: llm config mtime caching + pattern/plugin/channel re-import.

Config side: fingerprint hit skips the file re-read / file change re-parses
automatically / programmatic invalidate and reload entries / returned deep
copies do not pollute each other.
Code side: host.reload mtime detection, replace-mode takeover, dependency
order replay, session rebinding, channel spec fetched live per request.
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus import settings
from nexus.settings import (
    get_llm_config,
    invalidate_config_cache,
    load_config,
    reload_config,
)


_LLM_MIN = """\
llm_default:
  code: openai
  model: qwen3.8-max
"""


def _write(tmp_path, text, name="local_config.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _bump_mtime(path, delta=10.0):
    """Explicitly bump mtime (some filesystems have coarse mtime granularity, so a same-second rewrite leaves the fingerprint unchanged)."""
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + delta))


# ============================================================================
# llm config: mtime fingerprint caching
# ============================================================================

def test_config_cache_hit_skips_file_read(tmp_path):
    """Fingerprint unchanged: return the cached value without re-reading the file (read count does not increase)."""
    path = _write(tmp_path, _LLM_MIN)
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        load_config(path)
        assert spy.call_count == 1
        cfg2 = load_config(path)
        assert spy.call_count == 1  # cache hit
    assert cfg2["llm_default"]["model"] == "qwen3.8-max"


def test_config_cache_invalidated_on_edit(tmp_path):
    """File changed (mtime bumped): re-parsed automatically, the new value is read."""
    path = _write(tmp_path, _LLM_MIN)
    assert load_config(path)["llm_default"]["model"] == "qwen3.8-max"

    _write(tmp_path, _LLM_MIN.replace("qwen3.8-max", "qwen-new"))
    _bump_mtime(path)
    assert load_config(path)["llm_default"]["model"] == "qwen-new"


def test_config_deep_copy_no_cache_pollution(tmp_path):
    """Caller mutations of the returned value do not pollute the cache: the next load still gets the original content."""
    path = _write(tmp_path, _LLM_MIN)
    cfg = load_config(path)
    cfg["llm_default"]["model"] = "hacked"
    cfg["pattern_llm"]["injected"] = True
    again = load_config(path)
    assert again["llm_default"]["model"] == "qwen3.8-max"
    assert "injected" not in again["pattern_llm"]


def test_reload_config_programmatic_entry(tmp_path):
    """reload_config forces a re-read (bypassing the fingerprint); a same-fingerprint rewrite hits the cache, the forced entry re-parses."""
    path = _write(tmp_path, _LLM_MIN)
    load_config(path)
    # Same-length replacement + exact mtime nanosecond restore: fake a
    # "same-fingerprint rewrite" (coarse-grained filesystem scenario); with
    # APFS nanosecond granularity a normal rewrite always changes the
    # fingerprint (see the invalidation test)
    st = os.stat(path)
    _write(tmp_path, _LLM_MIN.replace("qwen3.8-max", "qwen-bbbbbb"))  # same length
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        assert load_config(path)["llm_default"]["model"] == "qwen3.8-max"
        assert spy.call_count == 0  # fingerprint unchanged, still served from cache
        reload_config(path)
        assert spy.call_count == 1  # forced re-read
    assert load_config(path)["llm_default"]["model"] == "qwen-bbbbbb"


def test_get_llm_config_uses_cache(tmp_path):
    """The three-level orchestration entry get_llm_config also benefits from the cache (hot path of every R1 round)."""
    path = _write(tmp_path, _LLM_MIN + "\npattern_llm:\n  p1:\n    model: pm\n")
    assert get_llm_config("p1", config_path=path)["model"] == "pm"
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        get_llm_config("p1", config_path=path)
        assert spy.call_count == 0


# ============================================================================
# registry replace mode
# ============================================================================

def test_plugin_registry_replace_mode():
    from nexus.registry.plugins import PluginRegistry

    class _A:
        pass

    class _B:
        pass

    reg = PluginRegistry()
    reg.register("executor", "x", _A)
    with pytest.raises(ValueError):
        reg.register("executor", "x", _B)  # strict by default: a different factory is rejected
    reg.replace_on_conflict = True
    reg.register("executor", "x", _B)  # replace window: swaps it in
    reg.replace_on_conflict = False
    inst = reg.resolve("executor", "x")
    assert isinstance(inst, _B)


def test_plugin_registry_replace_drops_cached_instance():
    from nexus.registry.plugins import PluginRegistry

    class _A:
        pass

    class _B:
        pass

    reg = PluginRegistry()
    reg.register("executor", "x", _A)
    assert isinstance(reg.resolve("executor", "x"), _A)
    reg.replace_on_conflict = True
    reg.register("executor", "x", _B)
    reg.replace_on_conflict = False
    assert isinstance(reg.resolve("executor", "x"), _B)  # instance cache cleared


def test_channel_registry_replace_mode():
    from nexus.registry.channels import ChannelRegistry

    class _SpecA:
        name = "a"
        payload_model = None

        def parse(self, p):  # pragma: no cover
            raise AssertionError

        def build_reply(self, r, s):  # pragma: no cover
            raise AssertionError

    class _SpecB(_SpecA):
        pass

    reg = ChannelRegistry()
    a = _SpecA()
    reg.register(a)
    with pytest.raises(ValueError):
        reg.register(_SpecB())  # duplicate name rejected by default
    reg.replace_on_conflict = True
    b = _SpecB()
    reg.register(b)
    reg.replace_on_conflict = False
    assert reg.get("a") is b
    # re-registering the same object is idempotent (no replace mode needed, no error)
    reg.register(b)


def test_channel_router_uses_live_spec():
    """The router handler fetches the registry spec live per request: a replace takes effect on the next request."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from nexus.channels.base import EngineOps, InboundMessage
    from nexus.channels.webhooks import build_channel_router
    from nexus.registry.channels import ChannelRegistry

    class _Payload(BaseModel):
        user_id: str

    class _SpecV1:
        name = "hotchan"
        payload_model = _Payload
        default_pattern_env = "X"
        token_env = None
        stale_seconds = 300.0

        def parse(self, p):
            return InboundMessage(channel=self.name, text="v1",
                                  session_key=p.user_id)

        def build_reply(self, reply, session_id):
            return {"reply": "v1", "session_id": session_id}

    class _SpecV2(_SpecV1):
        def parse(self, p):
            return InboundMessage(channel=self.name, text="v2",
                                  session_key=p.user_id)

        def build_reply(self, reply, session_id):
            return {"reply": "v2", "session_id": session_id}

    reg = ChannelRegistry()
    v1 = _SpecV1()
    with patch("nexus.registry.channels.registry", reg):
        reg.register(v1)
        app = FastAPI()

        async def _launch(*a, **k):  # pragma: no cover
            return None, "500", "unused"

        async def _run(s, q):
            return q, None

        app.include_router(build_channel_router(
            v1, EngineOps(get_session=lambda _s: SimpleNamespace(session_id=_s),
                          launch_session=_launch, run_chat_turn=_run)))
        client = TestClient(app)
        assert client.post("/api/v1/channel/hotchan",
                           json={"user_id": "u"}).json()["reply"] == "v1"

        # hot swap (mutate the dict directly without the replace flag, simulating a registry content update)
        reg._channels["hotchan"] = _SpecV2()
        assert client.post("/api/v1/channel/hotchan",
                           json={"user_id": "u"}).json()["reply"] == "v2"

        # after deregistration, fall back to the spec captured at wiring time (no 500)
        reg._channels.pop("hotchan")
        assert client.post("/api/v1/channel/hotchan",
                           json={"user_id": "u"}).json()["reply"] == "v1"


# ============================================================================
# host.reload: mtime detection + dependency-order replay + session rebinding
# ============================================================================

class ReloadHarness:
    """Self-registering pattern/plugin/channel modules under a tmp directory + a host.reload driver.

    Writes real .py files, does real imports, real mtime bumps — no mock
    shortcuts; what is verified is the full "edit file -> reload -> registry
    returns the new object" chain. Modules live in sys.modules under unique
    ``_reload_test.``-prefixed names (the fixture patches the discovery list
    generation with a tmp-root equivalent).
    """

    def __init__(self, tmp_path):
        self.root = tmp_path
        (self.root / "apps" / "demo_app").mkdir(parents=True)
        (self.root / "atoms" / "executors").mkdir(parents=True)
        # apps/__init__ / atoms packages are not needed — discovery globs
        # the directories directly, and modules are imported under unique
        # names via spec_from_file_location
        self._counter = 0

    def write_module(self, relpath: str, body: str) -> Path:
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def unique(self, prefix="mod") -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"


_PATTERN_MODULE = '''\
"""Self-registering pattern module (reload test fixture)."""
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

registry.register(Pattern(
    code="reload_demo",
    name="reload demo v{ver}",
    description="d",
    nodes=[BaseNode(code="m1", name="m1", description="d",
                    task_description="t")],
))
'''

_PLUGIN_MODULE = '''\
"""Self-registering plugin module (reload test fixture)."""
from nexus.registry.plugins import registry


class _Exec{ver}:
    def execute(self, ec):  # pragma: no cover -- never executed
        raise AssertionError


registry.register("executor", "reload_demo_exec", _Exec{ver})
'''


def _import_out_of_repo(harness, dotted, relpath):
    """Import out-of-repo registration modules under a unique module name (same bridge as the channel tests).

    Ancestor packages are placed into sys.modules level by level: the
    **direct parent package's** ``__path__`` points at the file's real
    directory (``importlib.reload`` relies on it to re-find the spec);
    higher ancestors only need placeholders (name resolution for
    cross-module imports inside the module body consults sys.modules level
    by level and never reaches disk). Re-execution reads the new content
    because the mtime change invalidates the pyc cache (guaranteed by
    _bump_mtime).
    """
    import importlib.util
    import types

    parts = dotted.split(".")
    path = harness.root / relpath
    for i in range(1, len(parts)):
        ancestor = ".".join(parts[:i])
        if ancestor in sys.modules:
            continue
        pkg = types.ModuleType(ancestor)
        pkg.__path__ = [str(path.parent)] if i == len(parts) - 1 else []
        sys.modules[ancestor] = pkg

    spec = importlib.util.spec_from_file_location(dotted, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def reload_env(tmp_path, monkeypatch):
    """Isolated host.reload environment: tmp scan root + clean registry state."""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry
    from nexus.registry.plugins import registry as plugin_registry

    harness = ReloadHarness(tmp_path)
    # Production discovery filters by sys.modules name prefixes (apps.,
    # atoms.executors.); out-of-repo test modules use the _reload_test.
    # prefix — patch the list generation to hand the unique names back
    monkeypatch.setattr(hr, "_discover_module_names", _make_discover(harness))
    # Clear mtime baselines other tests may have left behind (module names
    # with the _reload_test prefix do not clash, but re-running with the
    # same name would falsely report a change)
    hr._MODULE_MTIMES.clear()
    yield harness
    pattern_registry.deregister("reload_demo")
    plugin_registry.deregister("executor", "reload_demo_exec")
    for name in list(sys.modules):
        if name.startswith("_reload_test."):
            del sys.modules[name]


def _make_discover(harness):
    """Stands in for _discover_module_names: enumerate loaded modules under the tmp root (not limited to registration modules — mirroring production semantics), ordered by sys.modules insertion order (dependencies before consumers)."""
    def _discover():
        order = {n: i for i, n in enumerate(list(sys.modules))}
        names = []
        apps_root = harness.root / "apps"
        for app_dir in sorted(p for p in apps_root.iterdir() if p.is_dir()):
            for path in sorted(app_dir.glob("*.py")):
                if path.name == "__init__.py":
                    continue
                name = f"_reload_test.apps.{app_dir.name}.{path.stem}"
                if name in sys.modules:
                    names.append(name)
        exec_root = harness.root / "atoms" / "executors"
        if exec_root.is_dir():
            for path in sorted(exec_root.glob("*.py")):
                if path.name == "__init__.py":
                    continue
                name = f"_reload_test.executors.{path.stem}"
                if name in sys.modules:
                    names.append(name)
        return sorted(names, key=lambda n: order.get(n, 1 << 30))

    return _discover


def test_reload_detects_and_replaces_pattern(reload_env):
    """Pattern file changed -> reload -> the registry holds the new object (name contains v2)."""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry

    path = reload_env.write_module(
        "apps/demo_app/route.py", _PATTERN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")
    assert pattern_registry.get("reload_demo").name == "reload demo v1"
    old = pattern_registry.get("reload_demo")
    hr.reload_changed()  # first run only establishes the mtime baseline (same at watcher startup)

    time.sleep(0.02)
    reload_env.write_module("apps/demo_app/route.py",
                            _PATTERN_MODULE.format(ver=2))
    _bump_mtime(path, delta=5.0)

    result = hr.reload_changed()
    assert "_reload_test.apps.demo_app.route" in result["changed"]
    assert result["failed"] == []
    new = pattern_registry.get("reload_demo")
    assert new is not old
    assert new.name == "reload demo v2"
    # idempotent: with no further changes the second reload is a no-op
    again = hr.reload_changed()
    assert again["changed"] == []


def test_reload_replaces_plugin_class(reload_env):
    """Plugin (executor) file changed -> replace mode takes over the new class, instance cache refreshed."""
    import host.reload as hr
    from nexus.registry.plugins import registry as plugin_registry

    path = reload_env.write_module(
        "atoms/executors/demo_exec.py", _PLUGIN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.executors.demo_exec",
                        "atoms/executors/demo_exec.py")
    v1 = plugin_registry.resolve("executor", "reload_demo_exec")
    assert type(v1).__name__ == "_Exec1"
    hr.reload_changed()  # establish the baseline

    time.sleep(0.02)
    reload_env.write_module("atoms/executors/demo_exec.py",
                            _PLUGIN_MODULE.format(ver=2))
    _bump_mtime(path, delta=5.0)

    result = hr.reload_changed()
    assert result["failed"] == []
    v2 = plugin_registry.resolve("executor", "reload_demo_exec")
    assert type(v2).__name__ == "_Exec2"
    assert v2 is not v1


def test_reload_failure_keeps_old_registration(reload_env):
    """Replay failure (syntax error) -> warns and keeps the old registration; other modules are unaffected."""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry

    p_pattern = reload_env.write_module(
        "apps/demo_app/route.py", _PATTERN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")
    hr.reload_changed()  # establish the baseline

    time.sleep(0.02)
    reload_env.write_module("apps/demo_app/route.py", "def broken(:\n")
    _bump_mtime(p_pattern, delta=5.0)

    result = hr.reload_changed()
    assert "_reload_test.apps.demo_app.route" in result["changed"]
    assert result["failed"] == ["_reload_test.apps.demo_app.route"]
    assert pattern_registry.get("reload_demo").name == "reload demo v1"


def test_reload_rebind_sessions(reload_env):
    """rebind_sessions: after reload, in-memory sessions switch to the new pattern object and rebuild the map."""
    import host.reload as hr
    from nexus.engine.session import Session
    from nexus.registry.patterns import registry as pattern_registry

    path = reload_env.write_module(
        "apps/demo_app/route.py", _PATTERN_MODULE.format(ver=1))
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")

    session = Session(session_id="s1", pattern_code="reload_demo")
    session.pattern = pattern_registry.get("reload_demo")
    session.cxt.node_map = session.pattern.node_map
    old_pattern = session.pattern
    hr.reload_changed()  # establish the baseline

    time.sleep(0.02)
    reload_env.write_module("apps/demo_app/route.py",
                            _PATTERN_MODULE.format(ver=2))
    _bump_mtime(path, delta=5.0)
    hr.reload_changed()

    sessions = {"s1": session}
    assert hr.rebind_sessions(sessions, pattern_registry) == 1
    assert session.pattern is not old_pattern
    assert session.pattern.name == "reload demo v2"
    assert session.cxt.node_map is session.pattern.node_map

    # sessions of a deregistered pattern: keep the old reference
    pattern_registry.deregister("reload_demo")
    sessions = {"s1": session}
    assert hr.rebind_sessions(sessions, pattern_registry) == 0
    assert session.pattern is not None


def test_reload_all_invalidates_config(reload_env):
    """reload_all invalidates the config cache: after a same-fingerprint rewrite, the next parse re-reads the file."""
    import host.reload as hr

    path = _write(reload_env.root, _LLM_MIN)
    load_config(path)
    # Same-length replacement + exact mtime nanosecond restore: fake a "same-fingerprint rewrite"; only cache invalidation rescues this
    st = os.stat(path)
    _write(reload_env.root, _LLM_MIN.replace("qwen3.8-max", "qwen-cccccc"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    result = hr.reload_all()
    assert result["config"] == "invalidated"
    with patch("nexus.settings._parse_config_file",
               wraps=settings._parse_config_file) as spy:
        assert load_config(path)["llm_default"]["model"] == "qwen-cccccc"
        assert spy.call_count == 1  # re-parse is guaranteed after invalidation
    invalidate_config_cache(path)


# ============================================================================
# Dependency order: non-registration module (prompts) changes take effect via replayed consumers
# ============================================================================

_PROMPTS_MODULE = '''\
"""Pure data module (no registrations) — verifies transitive refresh of the reload dependency order."""
TITLE = "v{ver}"
'''

_DEP_PATTERN_MODULE = '''\
"""Registration module depending on prompts: the Pattern name comes from prompts.TITLE."""
from _reload_test.apps.demo_app.prompts import TITLE
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.registry.patterns import registry

registry.register(Pattern(
    code="reload_demo",
    name=TITLE,
    description="d",
    nodes=[BaseNode(code="m1", name="m1", description="d",
                    task_description="t")],
))
'''


def test_reload_prompts_change_propagates_via_consumer(reload_env):
    """Non-registration module (prompts) change: replay refreshes prompts
    first (by sys.modules insertion order) and then replays route (the
    consumer), so the Pattern in the registry picks up the new TITLE —
    reload does not cascade dependencies; consumers must re-execute
    themselves to bind the new object."""
    import host.reload as hr
    from nexus.registry.patterns import registry as pattern_registry

    reload_env.write_module("apps/demo_app/prompts.py",
                            _PROMPTS_MODULE.format(ver=1))
    reload_env.write_module("apps/demo_app/route.py",
                            _DEP_PATTERN_MODULE.format())
    # Only import route explicitly: prompts is loaded transitively by
    # route's import (insertion order prompts < route, exactly the
    # route->prompts dependency shape of production)
    _import_out_of_repo(reload_env, "_reload_test.apps.demo_app.route",
                        "apps/demo_app/route.py")
    assert pattern_registry.get("reload_demo").name == "v1"
    hr.reload_changed()  # establish the baseline (including the non-registration module prompts)

    time.sleep(0.02)
    p_prompts = reload_env.write_module("apps/demo_app/prompts.py",
                                        _PROMPTS_MODULE.format(ver=2))
    _bump_mtime(p_prompts, delta=5.0)

    result = hr.reload_changed()
    assert "_reload_test.apps.demo_app.prompts" in result["changed"]
    assert result["failed"] == []
    # route unchanged but transitively replayed -> the Pattern registered into the registry uses the new TITLE
    assert pattern_registry.get("reload_demo").name == "v2"
