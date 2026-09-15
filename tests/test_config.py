"""config loading tests (explicit config paths; no dependency on the local yaml)."""

import pytest

from nexus.settings import DEFAULT_SESSION_DB_PATH, get_session_db_path
from nexus.settings import get_llm_config, get_mcp_servers, load_config

_LLM_MIN = """\
llm:
  code: openai
  model: qwen3.8-max
"""


def _write_config(tmp_path, extra=""):
    path = tmp_path / "local_config.yaml"
    path.write_text(_LLM_MIN + extra, encoding="utf-8")
    return str(path)


def test_session_db_path_default(tmp_path):
    """Returns the default path when the config has no session_db_path."""
    assert get_session_db_path(_write_config(tmp_path)) == DEFAULT_SESSION_DB_PATH


def test_session_db_path_override(tmp_path):
    """Returns the override value when the config sets session_db_path explicitly."""
    config_path = _write_config(tmp_path, "\nsession_db_path: /tmp/audit.db\n")
    assert get_session_db_path(config_path) == "/tmp/audit.db"


# ============================================================================
# New structure: llm_providers / llm_default (spec 2026-09-02; the
# pattern_llm section was replaced by per-app apps/*/config.yaml overlays —
# see tests/test_app_config.py; a leftover pattern_llm key is simply ignored)
# ============================================================================

_NEW_STRUCT = """\
llm_providers:
  openai:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
llm_default:
  code: openai
  model: qwen3.8-max
  temperature: 0.7
"""

_LEGACY = """\
llm:
  code: openai
  model: qwen3.8-max
  api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key_env: DASHSCOPE_API_KEY
  temperature: 0.7
"""


def _write(tmp_path, text):
    p = tmp_path / "local_config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_new_structure_parsed(tmp_path):
    cfg = load_config(_write(tmp_path, _NEW_STRUCT))
    assert cfg["llm_default"]["code"] == "openai"
    assert cfg["llm_providers"]["openai"]["api_key_env"] == "DASHSCOPE_API_KEY"
    assert "pattern_llm" not in cfg  # died with the app-config redesign
    assert "llm" not in cfg


def test_legacy_llm_converted(tmp_path):
    cfg = load_config(_write(tmp_path, _LEGACY))
    # connection fields go into llm_providers.<code>, orchestration fields into llm_default
    assert cfg["llm_providers"] == {
        "openai": {
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
        }
    }
    assert cfg["llm_default"] == {"code": "openai", "model": "qwen3.8-max", "temperature": 0.7}


def test_legacy_and_new_coexist_rejected(tmp_path):
    with pytest.raises(ValueError, match="并存"):
        load_config(_write(tmp_path, _LEGACY + "llm_default:\n  code: openai\n  model: m\n"))


def test_llm_default_missing_required_fields(tmp_path):
    with pytest.raises(ValueError, match="必填字段"):
        load_config(_write(tmp_path, "llm_default:\n  code: openai\n"))


def test_no_llm_section_at_all_rejected(tmp_path):
    with pytest.raises(ValueError, match="llm"):
        load_config(_write(tmp_path, "session_db_path: /tmp/x.db\n"))


# ============================================================================
# get_llm_config entry: no-args global + override path (the layered
# llm_default ⊕ app ⊕ node priority chain lives in tests/test_app_config.py)
# ============================================================================


def test_no_args_returns_global(tmp_path):
    cfg = get_llm_config(config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["code"] == "openai"
    assert cfg["model"] == "qwen3.8-max"
    assert cfg["api_key_env"] == "DASHSCOPE_API_KEY"


def test_override_skips_layers(tmp_path):
    ov = {"code": "fake_test_provider", "model": "fake-model", "temperature": 0.1}
    cfg = get_llm_config(pattern_code="xianyu_agent", node_code="xy_route_root",
                         override=ov, config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["model"] == "fake-model" and cfg["temperature"] == 0.1


# ============================================================================
# main startup cross-check: the pattern_llm-era checks died with the
# app-config redesign — the app-config cross-check lives in
# tests/test_app_config.py (host.main._cross_check_app_configs)
# ============================================================================


def test_override_survives_missing_yaml(tmp_path, caplog):
    """With a missing yaml the override path degrades silently (keeps offline tests self-contained)."""
    ov = {"code": "x", "model": "m"}
    with caplog.at_level("WARNING"):
        cfg = get_llm_config(override=ov,
                             config_path=str(tmp_path / "nope.yaml"))
    assert cfg == {"code": "x", "model": "m"}


# ============================================================================
# Final-review regression fixes (Final review I1 / I3)
# ============================================================================

def test_llm_providers_unknown_field_warns_and_stripped(tmp_path, caplog):
    """Unknown fields in the llm_providers section (e.g. the typo api_key_evn) are stripped after a warning."""
    text = _NEW_STRUCT.replace(
        "  openai:\n    api_base:",
        "  openai:\n    api_key_evn: WRONG_ENV\n    api_base:")
    with caplog.at_level("WARNING"):
        cfg = load_config(_write(tmp_path, text))
    assert "api_key_evn" not in cfg["llm_providers"]["openai"]
    assert cfg["llm_providers"]["openai"]["api_key_env"] == "DASHSCOPE_API_KEY"
    assert any("api_key_evn" in r.message for r in caplog.records)


def test_override_without_code_falls_back_to_llm_default(tmp_path):
    """Override with model only (no code) -> the resolved result fills in llm_default's code."""
    ov = {"model": "override-model"}
    cfg = get_llm_config(override=ov, config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["code"] == "openai"
    assert cfg["model"] == "override-model"


def test_override_with_code_but_no_model_falls_back(tmp_path):
    """Override with code but no model: model falls back from llm_default, preventing a run_agent KeyError."""
    ov = {"code": "openai"}
    cfg = get_llm_config(override=ov, config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["code"] == "openai"
    assert cfg["model"] == "qwen3.8-max"


# ============================================================================
# mcp_servers: $VAR / ${VAR} env expansion (secrets via process environment)
# ============================================================================

_MCP_YAML = """\
mcp_servers:
  zai:
    transport: stdio
    command: npx
    args: ["-y", "@z_ai/mcp-server"]
    env:
      Z_AI_API_KEY: ${MCP_TEST_KEY}
  websearch:
    transport: streamable_http
    url: "https://api.z.ai/api/mcp/web_search_prime/mcp"
    headers:
      Authorization: "Bearer $MCP_TEST_KEY"
"""


def test_mcp_env_expansion(tmp_path, monkeypatch):
    """$VAR / ${VAR} in env / headers (and other string values) expand from the process environment."""
    monkeypatch.setenv("MCP_TEST_KEY", "sk-test-123")
    servers = get_mcp_servers(_write_config(tmp_path, _MCP_YAML))
    assert servers["zai"]["env"]["Z_AI_API_KEY"] == "sk-test-123"
    assert servers["websearch"]["headers"]["Authorization"] == "Bearer sk-test-123"


def test_mcp_env_expansion_unset_keeps_raw(tmp_path, monkeypatch, caplog):
    """An unset variable keeps its raw reference with a warning — load_config must stay env-independent."""
    monkeypatch.delenv("MCP_TEST_KEY", raising=False)
    with caplog.at_level("WARNING"):
        servers = get_mcp_servers(_write_config(tmp_path, _MCP_YAML))
    assert servers["zai"]["env"]["Z_AI_API_KEY"] == "${MCP_TEST_KEY}"
    assert servers["websearch"]["headers"]["Authorization"] == "Bearer $MCP_TEST_KEY"
    msgs = " ".join(r.message for r in caplog.records)
    assert "$MCP_TEST_KEY" in msgs


def test_mcp_env_expansion_no_dollar_untouched(tmp_path):
    """Literal values (no $ reference) pass through verbatim — zero behavior change."""
    servers = get_mcp_servers(_write_config(tmp_path, _MCP_YAML.replace(
        "${MCP_TEST_KEY}", "sk-literal").replace("$MCP_TEST_KEY", "sk-literal")))
    assert servers["zai"]["env"]["Z_AI_API_KEY"] == "sk-literal"
