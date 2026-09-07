"""config loading tests (explicit config paths; no dependency on the local yaml)."""

import pytest

from nexus.settings import DEFAULT_SESSION_DB_PATH, get_session_db_path
from nexus.settings import get_llm_config, load_config

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
# Pattern-level LLM config, new structure (spec 2026-09-02): llm_providers / llm_default / pattern_llm
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
pattern_llm:
  xianyu_agent:
    model: qwen-flash
    modules:
      xianyu_root: {model: qwen3.8-max}
    nodes:
      xy_route_root: {code: deepseek, model: deepseek-chat}
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
    assert cfg["pattern_llm"]["xianyu_agent"]["modules"]["xianyu_root"]["model"] == "qwen3.8-max"
    assert cfg["pattern_llm"]["xianyu_agent"]["nodes"]["xy_route_root"]["code"] == "deepseek"
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
    assert cfg.get("pattern_llm") == {}


def test_legacy_and_new_coexist_rejected(tmp_path):
    with pytest.raises(ValueError, match="并存"):
        load_config(_write(tmp_path, _LEGACY + "llm_default:\n  code: openai\n  model: m\n"))


def test_llm_default_missing_required_fields(tmp_path):
    with pytest.raises(ValueError, match="必填字段"):
        load_config(_write(tmp_path, "llm_default:\n  code: openai\n"))


def test_no_llm_section_at_all_rejected(tmp_path):
    with pytest.raises(ValueError, match="llm"):
        load_config(_write(tmp_path, "session_db_path: /tmp/x.db\n"))


def test_unknown_orchestration_field_warns(tmp_path, caplog):
    text = _NEW_STRUCT.replace(
        "xianyu_agent:\n    model: qwen-flash",
        "xianyu_agent:\n    model: qwen-flash\n    bogus_field: 1",
    )
    with caplog.at_level("WARNING"):
        cfg = load_config(_write(tmp_path, text))
    assert cfg["pattern_llm"]["xianyu_agent"].get("bogus_field") is None
    assert any("bogus_field" in r.message for r in caplog.records)


def test_nested_modules_rejected_with_warning(tmp_path, caplog):
    text = _NEW_STRUCT.replace(
        "xianyu_root: {model: qwen3.8-max}",
        "xianyu_root:\n        modules: {inner: {model: m}}",
    )
    with caplog.at_level("WARNING"):
        cfg = load_config(_write(tmp_path, text))
    assert cfg["pattern_llm"]["xianyu_agent"]["modules"]["xianyu_root"] == {}
    assert any("嵌套" in r.message for r in caplog.records)


# ============================================================================
# get_llm_config three-tier merge (spec 2026-09-02 §3.2/§3.3)
# ============================================================================

def test_layered_merge_priority(tmp_path):
    """node > module > pattern > global, shallow-merged layer by layer."""
    path = _write(tmp_path, _NEW_STRUCT + """\
  xianyu_agent2:
    model: qwen3.8-max
    modules:
      m1: {model: m-flash}
      m2: {temperature: 0.2}
    nodes:
      n1: {code: deepseek, model: deepseek-chat}
""")
    cfg = get_llm_config(pattern_code="xianyu_agent2",
                         module_code="m2", node_code="n1", config_path=path)
    # n1 switches code -> connection layer switches to the deepseek section (empty if that section is absent); temperature inherited from m2
    assert cfg["code"] == "deepseek"
    assert cfg["model"] == "deepseek-chat"
    assert cfg["temperature"] == 0.2
    assert cfg.get("api_base", "") == ""


def test_cross_provider_connection_switch(tmp_path):
    """When a node switches code, connection fields come from the new code's provider section, with no cross-wiring."""
    text = """\
llm_providers:
  openai:
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
  deepseek:
    api_base: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
llm_default:
  code: openai
  model: qwen3.8-max
pattern_llm:
  p:
    nodes:
      n: {code: deepseek, model: deepseek-chat}
"""
    cfg = get_llm_config(pattern_code="p", node_code="n",
                         config_path=_write(tmp_path, text))
    assert cfg["api_base"] == "https://api.deepseek.com/v1"
    assert cfg["api_key_env"] == "DEEPSEEK_API_KEY"


def test_unknown_codes_fallback_to_shallow_layer(tmp_path, caplog):
    cfg = get_llm_config(pattern_code="no_such_pattern", config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg["model"] == "qwen3.8-max"
    cfg2 = get_llm_config(pattern_code="xianyu_agent", module_code="no_such_module",
                          config_path=_write(tmp_path, _NEW_STRUCT))
    assert cfg2["model"] == "qwen-flash"
    # An unconfigured pattern is the normal case, so it drops to debug; a module miss still warns
    with caplog.at_level("DEBUG"):
        get_llm_config(pattern_code="no_such_pattern", config_path=_write(tmp_path, _NEW_STRUCT))
    assert any("no_such_pattern" in r.message for r in caplog.records)
    with caplog.at_level("WARNING"):
        get_llm_config(pattern_code="xianyu_agent", module_code="no_such_module",
                       config_path=_write(tmp_path, _NEW_STRUCT))
    assert any("no_such_module" in r.message for r in caplog.records)


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
# main startup cross-check (spec 2026-09-02 §5): unknown codes only warn, never block
# ============================================================================

def test_cross_check_warns_unknown_codes(tmp_path, caplog):
    """Unknown pattern/module/node codes in pattern_llm only warn, never raise."""
    import host.main as main
    text = _NEW_STRUCT + """\
  no_such_pattern:
    model: m
"""
    # For an unregistered pattern the modules/nodes branch is unreachable (continue), so the
    # unknown module/node branches are verified separately under a registered pattern
    text = text.replace("xy_route_root: {code: deepseek, model: deepseek-chat}",
                        "no_such_node: {code: deepseek, model: deepseek-chat}")
    text = text.replace("xianyu_root: {model: qwen3.8-max}",
                        "no_such_module: {model: qwen3.8-max}")
    with caplog.at_level("WARNING"):
        main._cross_check_pattern_llm(config_path=_write(tmp_path, text))
    msgs = " ".join(r.message for r in caplog.records)
    assert "no_such_pattern" in msgs
    assert "no_such_module" in msgs
    assert "no_such_node" in msgs


def test_cross_check_skips_on_load_failure(tmp_path, caplog):
    """On load_config failure only an exception is logged; nothing raises."""
    import host.main as main
    with caplog.at_level("WARNING"):
        main._cross_check_pattern_llm(config_path="/no/such/file.yaml")
    assert not any("未注册" in r.message for r in caplog.records)


def test_cross_check_registered_codes_no_warning(tmp_path, caplog):
    """Registered pattern/module/node codes produce no warning."""
    import host.main as main
    pattern = main.pattern_registry.list_codes()
    assert pattern  # discover already ran at import main
    with caplog.at_level("WARNING"):
        main._cross_check_pattern_llm(config_path=_write(tmp_path, _NEW_STRUCT))
    assert not any("未注册" in r.message for r in caplog.records)


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
