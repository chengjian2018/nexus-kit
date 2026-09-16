"""App-level config (apps/*/config.yaml, design 2026-09-15) tests: loading,
per-file validation (vocabulary / connection fields / duplicate binding),
the llm_default ⊕ app ⊕ node priority chain, mtime fingerprint caching, and
the loop / compression / guardrails / custom-bag accessors.

Isolation mirrors tests/conftest.py's posture (never touch the live
host/config/local_config.yaml or the real apps/): every test runs against a
tmp apps root via ``$NEXUS_APPS_DIR`` + a tmp global yaml via
``settings._CONFIG_PATH``, with cache invalidation on both ends. The
migrated layered-priority cases (node > app > global, cross-provider
switch, unknown-code fallback) moved here from tests/test_config.py when
pattern_llm died.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus import settings
from nexus.settings import (
    DEFAULT_LOOP_MAX_TOOL_ROUNDS,
    _load_app_configs,
    get_cron_tool_config,
    get_llm_config,
    get_loop_limits,
    get_pattern_custom_config,
    get_session_compress_config,
    get_shell_tool_config,
    invalidate_config_cache,
    load_config,
    resolve_max_fanout,
    resolve_max_steps,
)


_GLOBAL = """\
llm_providers:
  zai:
    api_base: https://zai.example/v1
    api_key_env: ZAI_KEY
  dashscope:
    api_base: https://dashscope.example/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
llm_default:
  code: zai
  model: glm-5.3-flash
  temperature: 0.7
  max_tokens: 10000
  enable_thinking: true
"""


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Isolated app-config environment: tmp apps root + tmp global yaml."""
    root = tmp_path / "apps"
    root.mkdir()
    cfg = tmp_path / "local_config.yaml"
    cfg.write_text(_GLOBAL, encoding="utf-8")
    monkeypatch.setattr(settings, "_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("NEXUS_APPS_DIR", str(root))
    invalidate_config_cache()
    yield root
    invalidate_config_cache()


def _write_app(root, name, text):
    """apps/<name>/config.yaml; name may contain slashes for nesting."""
    app_dir = root / name
    app_dir.mkdir(parents=True, exist_ok=True)
    path = app_dir / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _bump_mtime(path, delta=10.0):
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + delta))


# ============================================================================
# Loading + normalization
# ============================================================================

_FULL_APP = """\
pattern: archify
llm:
  temperature: 0.2
  max_tokens: 16000
  enable_thinking: true
nodes:
  af_author:
    llm: {max_tokens: 24000, timeout: 180, max_retries: 3}
    loop: {max_tool_rounds: 20}
  af_repair:
    llm: {temperature: 0.1}
loop:
  max_tool_rounds: 12
  max_steps: 16
  max_fanout: 8
compression:
  threshold: 6000
  retain_count: 12
guardrails:
  shell_tool:
    timeout_seconds: 120
skills:
  dir: my_skills
config:
  author_rounds: 10
  workspace_root: data/archify
"""


def test_load_full_app_config(app_env):
    """A full config.yaml parses into the normalized eight-key view."""
    _write_app(app_env, "archify_agent", _FULL_APP)
    configs = _load_app_configs()
    assert list(configs) == ["archify"]
    view = configs["archify"]
    assert view["pattern"] == "archify"
    assert view["llm"] == {
        "temperature": 0.2, "max_tokens": 16000, "enable_thinking": True}
    assert set(view["nodes"]) == {"af_author", "af_repair"}
    assert view["nodes"]["af_author"] == {
        "llm": {"max_tokens": 24000, "timeout": 180, "max_retries": 3},
        "loop": {"max_tool_rounds": 20}}
    assert view["nodes"]["af_repair"]["loop"] == {}  # node without loop
    assert view["loop"] == {"max_tool_rounds": 12, "max_steps": 16,
                            "max_fanout": 8}
    assert view["compression"] == {"threshold": 6000, "retain_count": 12}
    assert view["guardrails"] == {"shell_tool": {"timeout_seconds": 120}}
    assert view["skills"] == {"dir": "my_skills"}
    assert view["config"] == {"author_rounds": 10,
                              "workspace_root": "data/archify"}


def test_minimal_app_config_only_pattern(app_env):
    """Only the binding key: every overlay section is an empty dict."""
    _write_app(app_env, "tiny", "pattern: tiny\n")
    view = _load_app_configs()["tiny"]
    for key in ("llm", "nodes", "loop", "compression", "guardrails",
                "skills", "config"):
        assert view[key] == {}


def test_no_apps_dir_returns_empty(app_env):
    """No config.yaml anywhere: the overlay is a no-op (zero-change compat)."""
    (app_env / "some_app").mkdir()
    assert _load_app_configs() == {}


# ============================================================================
# Fail-fast validation (missing pattern / connection fields / duplicates)
# ============================================================================

def test_missing_pattern_key_fails_fast(app_env):
    path = _write_app(app_env, "no_bind", "llm: {temperature: 0.2}\n")
    with pytest.raises(ValueError, match="pattern"):
        _load_app_configs()
    # a parse failure must not populate the cache (fix the file -> reload works)
    path.write_text("pattern: no_bind\n", encoding="utf-8")
    _bump_mtime(path)
    assert _load_app_configs()["no_bind"]["pattern"] == "no_bind"


def test_non_string_pattern_fails_fast(app_env):
    _write_app(app_env, "bad_bind", "pattern: 123\n")
    with pytest.raises(ValueError, match="pattern"):
        _load_app_configs()


@pytest.mark.parametrize("field", ["api_base", "api_key", "api_key_env"])
def test_connection_fields_fail_fast_pattern_level(app_env, field):
    """Connection fields (credentials!) in the app llm section hard-reject the file."""
    _write_app(app_env, "leaky", f"pattern: leaky\nllm:\n  {field}: x\n")
    with pytest.raises(ValueError, match="连接字段"):
        _load_app_configs()


@pytest.mark.parametrize("field", ["api_base", "api_key", "api_key_env"])
def test_connection_fields_fail_fast_node_level(app_env, field):
    _write_app(app_env, "leaky_node",
               f"pattern: leaky\nnodes:\n  n1:\n    llm:\n      {field}: x\n")
    with pytest.raises(ValueError, match="连接字段"):
        _load_app_configs()


def test_duplicate_pattern_binding_fails_fast(app_env):
    """Two files binding the same pattern code name both paths in the error."""
    _write_app(app_env, "app_a", "pattern: dup\n")
    _write_app(app_env, "app_b", "pattern: dup\n")
    with pytest.raises(ValueError, match="多份 app 配置绑定"):
        _load_app_configs()


def test_structural_errors_fail_fast(app_env):
    """A dict expected where a scalar sits raises (same style as the global yaml)."""
    path = _write_app(app_env, "struct", "pattern: s\n")
    for text, pattern in (
        ("pattern: s\nnodes: 3\n", "nodes 应为字典"),
        ("pattern: s\nnodes:\n  n: not-a-dict\n", "nodes.n 应为字典"),
        ("pattern: s\nconfig: nope\n", "config 应为字典"),
    ):
        path.write_text(text, encoding="utf-8")
        _bump_mtime(path)  # parse failures skip the cache; make the stat differ too
        with pytest.raises(ValueError, match=pattern):
            _load_app_configs()


# ============================================================================
# Vocabulary: unknown keys / illegal values warn and are ignored
# ============================================================================

def test_unknown_keys_warn_not_raise(app_env, caplog):
    """Unknown keys at every level warn + are ignored (global-yaml behavior)."""
    _write_app(app_env, "noisy", """\
pattern: noisy
future_key: 1
llm:
  temperature: 0.1
  bogus: 2
nodes:
  n1:
    llm: {temperature: 0.0}
    compression: {threshold: 1}
    nested: 3
loop:
  max_tool_rounds: 11
  bogus_rounds: 5
guardrails:
  no_such_tool:
    timeout_seconds: 1
  shell_tool:
    timeout_seconds: 30
    bogus_field: 9
""")
    with caplog.at_level("WARNING"):
        configs = _load_app_configs()
    view = configs["noisy"]
    assert view["llm"] == {"temperature": 0.1}
    assert view["nodes"]["n1"] == {"llm": {"temperature": 0.0}, "loop": {}}
    assert view["loop"] == {"max_tool_rounds": 11}
    assert view["guardrails"] == {"shell_tool": {"timeout_seconds": 30}}
    msgs = " ".join(r.message for r in caplog.records)
    assert "future_key" in msgs
    assert "bogus" in msgs
    assert "no_such_tool" in msgs


@pytest.mark.parametrize("value", ["0", "-1", '"many"', "true"])
def test_illegal_loop_values_warn_and_drop(app_env, caplog, value):
    """loop values must be int >= 1; illegal ones drop back to the shallower layer."""
    _write_app(app_env, "loopy",
               f"pattern: loopy\nloop:\n  max_tool_rounds: {value}\n")
    with caplog.at_level("WARNING"):
        view = _load_app_configs()["loopy"]
    assert view["loop"] == {}
    assert any("max_tool_rounds" in r.message for r in caplog.records)


def test_illegal_compression_values_warn_and_drop(app_env, caplog):
    _write_app(app_env, "comp", "pattern: comp\ncompression:\n  threshold: -1\n")
    with caplog.at_level("WARNING"):
        view = _load_app_configs()["comp"]
    assert view["compression"] == {}
    assert any("threshold" in r.message for r in caplog.records)


def test_guardrails_bad_values_warn_and_drop(app_env, caplog):
    """Values run through the global sections' int()/str() coercion; failures drop."""
    _write_app(app_env, "gr", """\
pattern: gr
guardrails:
  shell_tool:
    max_output_chars: lots
    timeout_seconds: 30
""")
    with caplog.at_level("WARNING"):
        view = _load_app_configs()["gr"]
    assert view["guardrails"]["shell_tool"] == {"timeout_seconds": 30}
    assert any("max_output_chars" in r.message for r in caplog.records)


def test_cron_infra_fields_not_app_overridable(app_env, caplog):
    """jobs_path / tick_seconds are process-level infrastructure: the app
    overlay vocabulary deliberately excludes them (an app override would
    split jobs across files — add uses the ambient code, fire the frozen
    one, update/remove the global). Writing them warns + drops; the global
    cron_tool section still configures both."""
    _write_app(app_env, "cronish", """\
pattern: cronish
guardrails:
  cron_tool:
    max_jobs: 5
    jobs_path: data/cron_cronish.json
    tick_seconds: 99
""")
    with caplog.at_level("WARNING"):
        view = _load_app_configs()["cronish"]
    assert view["guardrails"]["cron_tool"] == {"max_jobs": 5}
    msgs = " ".join(r.message for r in caplog.records)
    assert "jobs_path" in msgs and "tick_seconds" in msgs
    # the merged guardrail keeps the global infra values
    cron = get_cron_tool_config("cronish")
    assert cron["max_jobs"] == 5
    assert cron["jobs_path"] == "data/cron_jobs.json"   # global default
    assert cron["tick_seconds"] == 20                   # global default
    # the global section remains the one place to configure them
    cfg_path = Path(settings._get_config_path())
    cfg_path.write_text(_GLOBAL + """
cron_tool:
  jobs_path: data/my_cron.json
  tick_seconds: 30
""", encoding="utf-8")
    _bump_mtime(cfg_path)
    cron = get_cron_tool_config("cronish")
    assert cron["jobs_path"] == "data/my_cron.json"
    assert cron["tick_seconds"] == 30


# ============================================================================
# Global loop section (local_config.yaml)
# ============================================================================

def test_global_loop_default_when_absent(app_env):
    assert load_config()["loop"] == {"max_tool_rounds":
                                     DEFAULT_LOOP_MAX_TOOL_ROUNDS}


def test_global_loop_valid_and_invalid_values(app_env, caplog):
    """Valid values parse; illegal ones (0 / non-int / bool) warn + fall back to 10."""
    cfg_path = Path(settings._get_config_path())
    for value, expected in (("25", 25), ("0", 10), ('"x"', 10), ("true", 10)):
        cfg_path.write_text(
            _GLOBAL + f"\nloop:\n  max_tool_rounds: {value}\n  bogus: 1\n",
            encoding="utf-8")
        _bump_mtime(cfg_path)
        with caplog.at_level("WARNING"):
            cfg = load_config()
        assert cfg["loop"]["max_tool_rounds"] == expected
    assert any("max_tool_rounds" in r.message for r in caplog.records)
    assert any("bogus" in r.message for r in caplog.records)  # unknown key


# ============================================================================
# LLM priority chain: llm_default ⊕ app.llm ⊕ app.nodes[node].llm
# ============================================================================

def test_llm_priority_chain_field_level(app_env):
    """The design §5.1 walkthrough: node wins per field, absent fields inherit."""
    _write_app(app_env, "archify_agent", _FULL_APP)
    cfg = get_llm_config(pattern_code="archify", node_code="af_author")
    assert cfg == {
        # connection layer from llm_providers.zai (app files carry no secrets)
        "api_base": "https://zai.example/v1",
        "api_key_env": "ZAI_KEY",
        # ① llm_default base, ② app llm, ③ node llm field-level overlays
        "code": "zai",
        "model": "glm-5.3-flash",
        "temperature": 0.2,
        "max_tokens": 24000,
        "enable_thinking": True,
        "timeout": 180,
        "max_retries": 3,
    }


def test_cross_provider_node_switch(app_env):
    """A node switching code takes the connection layer of the new provider, no cross-wiring."""
    _write_app(app_env, "mixed", """\
pattern: mixed
nodes:
  af_report:
    llm: {code: dashscope, model: qwen3.8-max, temperature: 0.4}
""")
    cfg = get_llm_config(pattern_code="mixed", node_code="af_report")
    assert cfg["code"] == "dashscope"
    assert cfg["model"] == "qwen3.8-max"
    assert cfg["api_base"] == "https://dashscope.example/compatible-mode/v1"
    assert cfg["api_key_env"] == "DASHSCOPE_API_KEY"


def test_unknown_codes_fallback_to_shallow_layer(app_env, caplog):
    """Unbound pattern is the normal case (debug); a bound pattern missing one node warns."""
    _write_app(app_env, "bound", "pattern: bound\nnodes:\n  n1: {llm: {}}\n")
    assert get_llm_config(pattern_code="no_such_pattern")["model"] == "glm-5.3-flash"
    with caplog.at_level("DEBUG"):
        get_llm_config(pattern_code="no_such_pattern")
    assert any("no_such_pattern" in r.message for r in caplog.records)
    with caplog.at_level("WARNING"):
        cfg = get_llm_config(pattern_code="bound", node_code="no_such_node")
    assert cfg["model"] == "glm-5.3-flash"
    assert any("no_such_node" in r.message for r in caplog.records)


def test_no_app_files_get_llm_config_matches_global(app_env, caplog):
    """Zero app files: layered output is field-for-field the pure global one."""
    with caplog.at_level("DEBUG"):
        layered = get_llm_config(pattern_code="whatever", node_code="n")
    assert layered == get_llm_config() == {
        "api_base": "https://zai.example/v1",
        "api_key_env": "ZAI_KEY",
        "code": "zai",
        "model": "glm-5.3-flash",
        "temperature": 0.7,
        "max_tokens": 10000,
        "enable_thinking": True,
    }


def test_broken_app_file_surfaces_through_accessors(app_env):
    """The fail-fast contract: accessors raise (not silently degrade) on a broken file."""
    _write_app(app_env, "leak", "pattern: leak\nllm: {api_key: sk-x}\n")
    _write_app(app_env, "ok", "pattern: ok\n")
    with pytest.raises(ValueError, match="连接字段"):
        get_llm_config(pattern_code="ok")  # any lookup scans the whole table


# ============================================================================
# Cache: fingerprint invalidation + hot reload
# ============================================================================

def test_app_config_cache_hit_and_edit_reload(app_env):
    path = _write_app(app_env, "cached",
                      "pattern: cached\nloop: {max_tool_rounds: 12}\n")
    assert get_loop_limits("cached")["max_tool_rounds"] == 12
    # cache hit: no re-parse while the fingerprint is unchanged
    with patch("nexus.settings._parse_app_config_file",
               wraps=settings._parse_app_config_file) as spy:
        _load_app_configs()
        assert spy.call_count == 0
    # edit + mtime bump: the new value is read automatically
    path.write_text("pattern: cached\nloop: {max_tool_rounds: 20}\n",
                    encoding="utf-8")
    _bump_mtime(path)
    assert get_loop_limits("cached")["max_tool_rounds"] == 20


def test_app_config_cache_same_fingerprint_invalidated(app_env):
    """A same-fingerprint rewrite is rescued by invalidate_config_cache (coarse filesystems)."""
    path = _write_app(app_env, "sf", "pattern: sf\nloop: {max_tool_rounds: 5}\n")
    assert get_loop_limits("sf")["max_tool_rounds"] == 5
    st = os.stat(path)
    path.write_text("pattern: sf\nloop: {max_tool_rounds: 7}\n",
                    encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore fingerprint
    invalidate_config_cache()
    assert get_loop_limits("sf")["max_tool_rounds"] == 7


def test_deleted_app_file_invalidates_overlay(app_env):
    """The file set itself is part of the fingerprint: deleting the file drops the overlay."""
    path = _write_app(app_env, "gone",
                      "pattern: gone\nloop: {max_tool_rounds: 42}\n")
    assert get_loop_limits("gone")["max_tool_rounds"] == 42
    path.unlink()
    assert get_loop_limits("gone")["max_tool_rounds"] == 10  # global default


# ============================================================================
# Loop limits / step budgets
# ============================================================================

def test_get_loop_limits_three_layers(app_env):
    _write_app(app_env, "loopy", """\
pattern: loopy
nodes:
  hot_node:
    loop: {max_tool_rounds: 20}
  cool_node: {llm: {temperature: 0.1}}
loop: {max_tool_rounds: 12}
""")
    assert get_loop_limits()["max_tool_rounds"] == 10       # global default
    assert get_loop_limits("unbound")["max_tool_rounds"] == 10
    assert get_loop_limits("loopy")["max_tool_rounds"] == 12  # pattern level
    assert get_loop_limits("loopy", "hot_node")["max_tool_rounds"] == 20
    # a node entry without its own loop inherits the pattern level silently
    assert get_loop_limits("loopy", "cool_node")["max_tool_rounds"] == 12
    assert get_loop_limits("loopy", "no_such_node")["max_tool_rounds"] == 12


def test_global_loop_overrides_default(app_env):
    cfg_path = Path(settings._get_config_path())
    cfg_path.write_text(_GLOBAL + "\nloop: {max_tool_rounds: 15}\n",
                        encoding="utf-8")
    _bump_mtime(cfg_path)
    assert get_loop_limits()["max_tool_rounds"] == 15
    assert get_loop_limits("unbound")["max_tool_rounds"] == 15


def test_resolve_max_steps_and_fanout(app_env):
    """Code-declared budgets are the default; the app yaml wins when present."""
    pattern = SimpleNamespace(code="archify", max_steps=16, max_fanout=4)
    assert resolve_max_steps(pattern) == 16
    assert resolve_max_fanout(pattern) == 4
    _write_app(app_env, "archify_agent",
               "pattern: archify\nloop: {max_steps: 20}\n")
    assert resolve_max_steps(pattern) == 20
    assert resolve_max_fanout(pattern) == 4  # untouched key keeps code default


# ============================================================================
# Compression / guardrails / custom bag overlays
# ============================================================================

def test_compression_field_level_merge(app_env):
    """App compression overrides only the fields it declares; threshold 0 = off."""
    assert get_session_compress_config() == (6000, 12)  # global defaults
    assert get_session_compress_config("unbound") == (6000, 12)
    _write_app(app_env, "c1", "pattern: c1\ncompression: {threshold: 0}\n")
    assert get_session_compress_config("c1") == (0, 12)  # off, retain inherited
    _write_app(app_env, "c2", "pattern: c2\ncompression: {retain_count: 20}\n")
    assert get_session_compress_config("c2") == (6000, 20)
    _write_app(app_env, "c3",
               "pattern: c3\ncompression: {threshold: 8000, retain_count: 5}\n")
    assert get_session_compress_config("c3") == (8000, 5)


def test_guardrails_field_level_merge_bidirectional(app_env):
    """The overlay may loosen AND tighten — only its own fields change."""
    global_shell = get_shell_tool_config()
    assert global_shell == {"timeout_seconds": 60, "max_output_chars": 20000}
    assert get_shell_tool_config("unbound") == global_shell
    _write_app(app_env, "looser",
               "pattern: looser\nguardrails:\n  shell_tool: {timeout_seconds: 120}\n")
    assert get_shell_tool_config("looser") == {
        "timeout_seconds": 120, "max_output_chars": 20000}  # loosened
    _write_app(app_env, "tighter",
               "pattern: tighter\nguardrails:\n  shell_tool: {max_output_chars: 1000}\n")
    assert get_shell_tool_config("tighter") == {
        "timeout_seconds": 60, "max_output_chars": 1000}  # tightened
    # other sections stay untouched by an app's cron overlay (the infra
    # fields jobs_path/tick_seconds are not app-overridable at all — see
    # test_cron_infra_fields_not_app_overridable)
    _write_app(app_env, "cronish",
               "pattern: cronish\nguardrails:\n  cron_tool: {max_jobs: 5}\n")
    cron = get_cron_tool_config("cronish")
    assert cron["max_jobs"] == 5
    assert cron["jobs_path"] == "data/cron_jobs.json"  # inherited from global
    assert cron["tick_seconds"] == 20                  # inherited from global
    assert get_shell_tool_config("cronish") == global_shell


def test_custom_config_bag(app_env):
    """The free bag passes through untouched; unbound patterns get an empty dict."""
    assert get_pattern_custom_config() == {}
    assert get_pattern_custom_config("unbound") == {}
    _write_app(app_env, "baggy",
               "pattern: baggy\nconfig:\n  author_rounds: 10\n"
               "  nested: {k: v}\n  flag: true\n")
    assert get_pattern_custom_config("baggy") == {
        "author_rounds": 10, "nested": {"k": "v"}, "flag": True}
    # caller mutations must not pollute the cache
    bag = get_pattern_custom_config("baggy")
    bag["injected"] = True
    assert "injected" not in get_pattern_custom_config("baggy")


# ============================================================================
# pattern_llm removal + mcp allowed_patterns dead-key cleanup
# ============================================================================

def test_leftover_pattern_llm_key_ignored(app_env, caplog):
    """A legacy pattern_llm section no longer parses nor breaks, and the
    silent fall-back to llm_default is loudly reported (a real deployment
    upgrading with content in there must get a migration signal)."""
    cfg_path = Path(settings._get_config_path())
    cfg_path.write_text(_GLOBAL + "\npattern_llm:\n  some_pattern:\n    model: m\n",
                        encoding="utf-8")
    _bump_mtime(cfg_path)
    with caplog.at_level("WARNING"):
        cfg = load_config()
    assert "pattern_llm" not in cfg
    assert get_llm_config(pattern_code="some_pattern")["model"] == "glm-5.3-flash"
    assert any("pattern_llm" in r.message and "废弃" in r.message
               for r in caplog.records)


def test_mcp_allowed_patterns_stripped(app_env):
    """The deprecated mcp_servers.allowed_patterns key is silently dropped from entries."""
    cfg_path = Path(settings._get_config_path())
    cfg_path.write_text(_GLOBAL + """\
mcp_servers:
  zai:
    transport: stdio
    command: npx
    allowed_patterns: ["deep_research"]
""", encoding="utf-8")
    _bump_mtime(cfg_path)
    entry = load_config()["mcp_servers"]["zai"]
    assert "allowed_patterns" not in entry
    assert entry["command"] == "npx"
    assert entry["tool_name_prefix"] == ""  # normalization still applies


# ============================================================================
# Phase 2 engine wiring: loop rounds through the graph runtime, the tool
# context's pattern_code, guardrail tool read points, the cron freeze, and
# maybe_compress's locate key
# ============================================================================

import json  # noqa: E402

from async_utils import arun  # noqa: E402
from nexus.engine.chat import chat_turn  # noqa: E402
from nexus.engine.compression import maybe_compress  # noqa: E402
from nexus.engine.session import Session  # noqa: E402
from nexus.engine.tool_context import (  # noqa: E402
    ToolCallContext,
    ambient_pattern_code,
    current_tool_context,
    subagent_scope,
    tool_call_context,
    workflow_scope,
)
from nexus.model.node import BaseNode  # noqa: E402
from nexus.model.pattern import Pattern  # noqa: E402
from nexus.registry.tools import registry as tool_registry  # noqa: E402

import atoms.executors  # noqa: F401,E402 -- default_loop must be registered
from atoms.tools import file_tool  # noqa: F401,E402 -- write_text must be registered


# ---------------------------------------------------------------------------
# loop_executor: the three-layer max_tool_rounds drives the real loop
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """Always answers with one hallucinated tool call — the loop never ends
    before the budget, so the consumed-call count IS the resolved budget."""

    def __init__(self, rounds=30):
        self.remaining = rounds
        self.seen = 0

    async def achat_completion(self, messages, model, temperature=0.7,
                               max_tokens=2048, tools=None, tool_choice=None):
        self.seen += 1
        self.remaining -= 1
        return {"content": None, "tool_calls": [
            {"id": f"c{self.seen}", "type": "function",
             "function": {"name": "no_such_tool", "arguments": "{}"}}]}


def _rounds_session(pattern_code):
    node = BaseNode(code="main", name="主节点", use_tools=[])
    p = Pattern(code=pattern_code, name="t", description="t",
                allow_toolset=[], nodes=[node])
    s = Session(session_id="s-wire", pattern_code=pattern_code)
    s.pattern = p
    s.cxt.node_map = p.node_map
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    return s


def _run_until_budget(session, provider):
    with patch("atoms.executors.loop_executor.build_provider",
               return_value=provider), \
         patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        result = arun(chat_turn("跑", session.session_id,
                                {session.session_id: session}))
    assert result.text == "抱歉，处理超时，请稍后重试。"
    return provider.seen


def test_loop_rounds_global_default(app_env):
    """No app file: the loop consumes exactly the global default 10 rounds."""
    assert _run_until_budget(_rounds_session("unbound"), _ScriptedProvider()) == 10


def test_loop_rounds_global_yaml_layer(app_env):
    cfg_path = Path(settings._get_config_path())
    cfg_path.write_text(_GLOBAL + "\nloop: {max_tool_rounds: 2}\n",
                        encoding="utf-8")
    _bump_mtime(cfg_path)
    assert _run_until_budget(_rounds_session("unbound"), _ScriptedProvider()) == 2


def test_loop_rounds_app_pattern_and_node_layers(app_env):
    _write_app(app_env, "wire_app", """\
pattern: wire_app
nodes:
  main:
    loop: {max_tool_rounds: 1}
loop: {max_tool_rounds: 3}
""")
    # node level beats pattern level
    assert _run_until_budget(_rounds_session("wire_app"),
                             _ScriptedProvider()) == 1
    # a node without its own loop entry inherits the pattern level
    other = BaseNode(code="quiet", name="n", use_tools=[])
    p = Pattern(code="wire_app", name="t", description="t",
                allow_toolset=[], nodes=[other])
    s = Session(session_id="s-wire2", pattern_code="wire_app")
    s.pattern = p
    s.cxt.node_map = p.node_map
    s.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    assert _run_until_budget(s, _ScriptedProvider()) == 3


# ---------------------------------------------------------------------------
# ToolCallContext.pattern_code: publication, defaults, scope inheritance
# ---------------------------------------------------------------------------

def test_tool_context_pattern_code_passing_and_default():
    assert ambient_pattern_code() == ""          # detached call
    with tool_call_context({"code": "x"}, ["filesystem"]) as ctx:
        assert ctx.pattern_code == ""            # legacy callers: default ""
        assert ambient_pattern_code() == ""
        assert current_tool_context() is ctx
    with tool_call_context({"code": "x"}, [], pattern_code="archify"):
        assert ambient_pattern_code() == "archify"
    # sub-agent / workflow scopes re-publish via replace() — the key rides along
    base = ToolCallContext(llm_config={}, allow_toolsets=frozenset(),
                           pattern_code="archify")
    with subagent_scope(base):
        assert ambient_pattern_code() == "archify"
    with workflow_scope(base):
        assert ambient_pattern_code() == "archify"


# ---------------------------------------------------------------------------
# Guardrail tools: the ambient pattern_code reaches the settings accessor
# ---------------------------------------------------------------------------

def test_file_tool_reads_app_overlay(app_env, tmp_path):
    _write_app(app_env, "gr_app",
               "pattern: gr_app\nguardrails:\n"
               "  file_tool: {max_write_chars: 5}\n")
    target = tmp_path / "f.txt"
    # executing pattern bound to the app → the overlay cap applies
    with tool_call_context({"code": "x"}, [], pattern_code="gr_app"):
        r = json.loads(arun(tool_registry.dispatch(
            "write_text", {"path": str(target), "content": "0123456789"})))
    assert "上限" in r["error"]
    # legacy self-built context (custom executors that never pass
    # pattern_code) → "" → global cap: the compatibility floor
    with tool_call_context({"code": "x"}, []):
        r = json.loads(arun(tool_registry.dispatch(
            "write_text", {"path": str(target), "content": "0123456789"})))
    assert r.get("created") is True
    # detached dispatch (no context at all) → global cap as well
    r = json.loads(arun(tool_registry.dispatch(
        "write_text", {"path": str(tmp_path / "g.txt"), "content": "0123456789"})))
    assert r.get("created") is True


# ---------------------------------------------------------------------------
# Cron: pattern_code frozen into the job snapshot; fire resolves with it
# ---------------------------------------------------------------------------

_CRON_GUARD = {"max_jobs": 20, "fire_timeout_seconds": 300, "max_rounds": 8,
               "history_cap": 10, "max_input_chars": 8000,
               "max_result_chars": 4000, "jobs_path": "/unused/cron.json",
               "tick_seconds": 20}


def test_cron_job_freezes_pattern_code(app_env):
    from atoms.tools import cron_tool  # noqa: F401 -- the module import registers
    from atoms.tools._cron_core import get_scheduler, reset_scheduler

    reset_scheduler()
    try:
        with tool_call_context({"code": "x"}, ["cron"],
                               pattern_code="frozen_app"):
            r = json.loads(arun(tool_registry.dispatch(
                "create_cron", {"name": "j", "interval_minutes": 60,
                                "input": "t"})))
        job = get_scheduler().get_job(r["job_id"])
        assert job["pattern_code"] == "frozen_app"
        # detached create (no ambient) → "" = the global guardrails at fire
        r2 = json.loads(arun(tool_registry.dispatch(
            "create_cron", {"name": "j2", "interval_minutes": 60,
                            "input": "t"})))
        assert get_scheduler().get_job(r2["job_id"])["pattern_code"] == ""
    finally:
        reset_scheduler()


def test_cron_store_roundtrip_preserves_pattern_code(tmp_path):
    from atoms.tools._cron_core import CronStore

    store = CronStore(str(tmp_path / "jobs.json"))
    store.save({"job_a": {"id": "job_a", "pattern_code": "archify"}})
    assert store.load()["job_a"]["pattern_code"] == "archify"


def test_cron_fire_resolves_guardrails_with_frozen_code(app_env):
    from atoms.tools import _cron_core
    from atoms.tools._cron_core import CronScheduler

    calls = []

    def spy(pattern_code="", config_path=""):
        calls.append(pattern_code)
        return dict(_CRON_GUARD)

    async def stub(job):
        return {"status": "ok", "content": "x", "rounds": 1, "usage": {}}

    with patch("atoms.tools._cron_core.get_cron_tool_config",
               side_effect=spy):
        sched = CronScheduler(execute_job=stub)
        base = {"id": "job_f", "name": "t", "enabled": True,
                "schedule": {"interval_minutes": 60}, "input": "task",
                "tools": [], "runs": 0, "next_fire_at": 1.0, "history": []}
        sched.jobs["job_f"] = {**base, "pattern_code": "frozen_app"}
        legacy = {**base, "id": "job_l"}     # old job file: field missing
        sched.jobs["job_l"] = legacy
        arun(sched._fire("job_f"))
        arun(sched._fire("job_l"))
    assert calls == ["frozen_app", ""]       # legacy jobs fall back to global


def test_cron_default_execute_job_uses_frozen_code(app_env):
    from atoms.tools import _cron_core

    llm_calls = []

    def llm_spy(pattern_code="", node_code="", override=None,
                config_path=""):
        llm_calls.append(pattern_code)
        return {"code": "x", "model": "m", "temperature": 0.7}

    async def stub_sub_agent(**kwargs):
        return {"status": "ok", "content": "done", "rounds": 1, "usage": {}}

    with patch("nexus.settings.get_llm_config", side_effect=llm_spy), \
         patch("nexus.llm.resolve.build_provider", return_value=object()), \
         patch("atoms.tools._subagent_core._run_sub_agent",
               side_effect=stub_sub_agent):
        payload = arun(_cron_core.default_execute_job(
            {"input": "t", "tools": [], "system_prompt": "",
             "pattern_code": "frozen_app"}))
    assert payload["status"] == "ok"
    assert llm_calls == ["frozen_app"]


# ---------------------------------------------------------------------------
# maybe_compress: the session's pattern_code keys the compression overlay
# ---------------------------------------------------------------------------

def test_maybe_compress_reads_session_pattern_code(app_env):
    calls = []
    real = settings.get_session_compress_config

    def spy(pattern_code="", config_path=""):
        calls.append(pattern_code)
        return real(pattern_code, config_path)

    session = SimpleNamespace(session_id="s", pattern_code="comp_app",
                              cxt=SimpleNamespace(history=[]))
    with patch("nexus.settings.get_session_compress_config",
               side_effect=spy):
        arun(maybe_compress(session, object()))   # short history: no-op
    assert calls == ["comp_app"]


# ---------------------------------------------------------------------------
# host startup cross-check (replaces the pattern_llm-era checks)
# ---------------------------------------------------------------------------

def test_cross_check_app_configs_warns_only(app_env, caplog):
    import host.main as main

    main.pattern_registry.register(Pattern(
        code="xc_app", name="t", description="t",
        nodes=[BaseNode(code="n1", name="a")]))
    main.pattern_registry.register(Pattern(
        code="xc_ok", name="t", description="t",
        nodes=[BaseNode(code="n1", name="a")]))
    try:
        _write_app(app_env, "xc_bad", "pattern: no_such_pattern\n")
        _write_app(app_env, "xc_node", """\
pattern: xc_app
nodes:
  ghost: {llm: {temperature: 0.1}}
""")
        _write_app(app_env, "xc_ok_file", """\
pattern: xc_ok
nodes:
  n1: {llm: {temperature: 0.1}}
""")
        with caplog.at_level("WARNING"):
            main._cross_check_app_configs()   # never raises, warn only
    finally:
        main.pattern_registry.deregister("xc_app")
        main.pattern_registry.deregister("xc_ok")
    msgs = " ".join(r.message for r in caplog.records)
    assert "no_such_pattern" in msgs
    assert "ghost" in msgs
    assert "n1" not in msgs                  # the healthy binding stays silent


# ============================================================================
# Phase 3/4 wrap-up: end-to-end over the committed real file
# apps/archify_agent/config.yaml, plus the skill chain's app-level overlay
# ============================================================================

_REPO_APPS = Path(__file__).resolve().parents[1] / "apps"


@pytest.fixture()
def real_apps(app_env, monkeypatch):
    """Restore the real repo anchor: undo conftest's NEXUS_APPS_DIR isolation so
    apps/ points back at the repo root (the committed real file
    apps/archify_agent/config.yaml). The global yaml stays app_env's tmp copy
    — "real file" means the app side; the connection layer never depends on
    the deployment machine's local_config.yaml. Teardown restores both ways
    (monkeypatch reversal + double invalidation)."""
    monkeypatch.delenv("NEXUS_APPS_DIR", raising=False)
    invalidate_config_cache()
    yield
    invalidate_config_cache()


def test_real_repo_app_config_end_to_end(real_apps):
    """The repo's real apps/archify_agent/config.yaml: load + three accessors
    reading the overridden values (the design §4.1 example was the first to
    land; this test pins it against drift)."""
    assert (_REPO_APPS / "archify_agent" / "config.yaml").is_file()
    assert "archify" in _load_app_configs()

    # The llm priority chain (§5.1 walkthrough): (1) llm_default ⊕ (2) app
    # llm ⊕ (3) node llm (the authoring/repair nodes override only sampling
    # budgets; the provider inherits (1)'s zai/glm-5.3-flash wholesale)
    cfg = get_llm_config(pattern_code="archify", node_code="af_author")
    assert cfg["temperature"] == 0.2        # layer (2), the pattern level
    assert cfg["max_tokens"] == 100000      # layer (3), the node level wins
    assert cfg["timeout"] == 180 and cfg["max_retries"] == 3
    assert cfg["code"] == "zai"             # layer (1) inherited (the node never switches provider)
    assert cfg["model"] == "glm-5.3-flash"
    assert cfg["api_base"] == "https://zai.example/v1"
    # af_report overrides only temperature: everything else inherits wholesale
    report = get_llm_config(pattern_code="archify", node_code="af_report")
    assert report["code"] == "zai"
    assert report["model"] == "glm-5.3-flash"
    assert report["temperature"] == 0.4
    # af_percept perceptual review: the zai vision model glm-5.3-flash (thinking off at node level)
    percept = get_llm_config(pattern_code="archify", node_code="af_percept")
    assert percept["code"] == "zai"
    assert percept["model"] == "glm-5.3-flash"
    assert percept["enable_thinking"] is False
    assert percept["temperature"] == 0.0
    assert percept["max_tokens"] == 40000

    # loop: pattern-level 12 → node-level af_author 20
    assert get_loop_limits("archify")["max_tool_rounds"] == 12
    assert get_loop_limits("archify", "af_author")["max_tool_rounds"] == 20

    # config bag: the executor's read face after the archify migration
    # (executor._runtime_settings)
    bag = get_pattern_custom_config("archify")
    assert bag["author_rounds"] == 10
    assert bag["repair_rounds"] == 3
    assert bag["route_retries"] == 1
    assert bag["stale_limit"] == 3     # tightened by deployment (stale-3 honest exit)
    assert bag["percept_retries"] == 1
    assert bag["workspace_root"] == "data/archify"
    assert bag["repo_root"] == "."     # repo-evidence verification root (the executor pins it absolute)
    # skill_dir is not written into the real yaml → the executor falls back
    # to the code default (also ~/.claude/skills/archify, see
    # executor._DEFAULT_SKILL_DIR)
    assert bag.get("skill_dir") is None

    # guardrails: shell relaxed to 120s (the archify CLI runs validate/deliver via bash)
    assert get_shell_tool_config("archify")["timeout_seconds"] == 120


def test_app_skills_dir_beats_pattern_config(app_env, tmp_path):
    """The skills chain (design §5.4): the app skills.dir beats
    pattern.config.skills_dir (the code-level default); with no app binding →
    the code default applies. A real SKILL.md structure (the same technique
    as tests/test_skills.py); the scan result identifies the actually
    effective root."""
    from nexus.skills import (
        invalidate_skills_cache,
        resolve_skills_dir,
        scan_skills,
    )

    def make_root(name):
        root = tmp_path / name
        (root / "probe_skill").mkdir(parents=True)
        (root / "probe_skill" / "SKILL.md").write_text(
            "---\ndescription: 探针技能\n---\n正文\n", encoding="utf-8")
        return root

    app_root, code_root = make_root("app_root"), make_root("code_root")
    pattern = SimpleNamespace(code="sk_app",
                              config={"skills_dir": str(code_root)})

    # No app binding: pattern.config.skills_dir (the code-level default) applies
    invalidate_skills_cache()
    assert resolve_skills_dir(pattern) == code_root
    assert list(scan_skills(code_root)) == ["probe_skill"]

    # With an app binding: the app skills.dir wins (same pattern object; the yaml overrides the code declaration)
    _write_app(app_env, "sk_app_dir",
               f"pattern: sk_app\nskills:\n  dir: {app_root}\n")
    invalidate_skills_cache()
    assert resolve_skills_dir(pattern) == app_root
    entries = scan_skills(app_root)
    assert list(entries) == ["probe_skill"]
    assert entries["probe_skill"].path == app_root / "probe_skill"
