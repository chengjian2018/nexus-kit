"""Kernel runtime settings — schema + resolution for local_config.yaml.

The kernel owns the settings *schema* (LLM 三级编排、压缩、DB 路径); only the
host knows where the yaml file lives: host/config calls ``set_config_path()``
at boot. LLM config fields come from ``ProviderEntry`` / ``BaseLLMProvider``
in ``nexus/llm/provider.py`` plus ``OpenAICompatibleProvider`` in
``atoms/providers/openai_provider.py``.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# ============================================================================
# Required and optional LLM config fields (from ProviderEntry in llm/provider.py)
# ============================================================================

_LLM_REQUIRED_FIELDS = {
    "code",   # Unique provider code, matching the provider registered in the registry
    "model",  # Model name to use
}

_LLM_OPTIONAL_FIELDS = {
    "api_base",     # API base URL (overrides the provider default)
    "api_key",      # Set the API key directly (takes precedence over api_key_env)
    "api_key_env",  # Env var name holding the API key, e.g. "DASHSCOPE_API_KEY"
    "temperature",  # Generation temperature, default 0.7
    "max_tokens",   # Max output tokens, default 2048
    "timeout",      # Request timeout in seconds, default 60 (from OpenAICompatibleProvider)
    "max_retries",  # Retry count on failure, default 2 (from OpenAICompatibleProvider)
    "enable_thinking",  # Qwen3 thinking-mode toggle, default False (from OpenAICompatibleProvider)
}

# All valid LLM config fields
_LLM_ALL_FIELDS = _LLM_REQUIRED_FIELDS | _LLM_OPTIONAL_FIELDS


# ============================================================================
# Pattern-level LLM config (spec 2026-09-02): provider connection / model orchestration split
# ============================================================================

# Orchestration fields: fields allowed in llm_default and in pattern_llm per-level entries
_ORCHESTRATION_FIELDS = {
    "code", "model", "temperature", "max_tokens",
    "timeout", "max_retries", "enable_thinking",
}

# Connection fields: fields allowed in each llm_providers section (legacy llm: node splits by this)
_CONNECTION_FIELDS = {
    "api_base", "api_key", "api_key_env", "timeout", "max_retries",
}

_PATTERN_LLM_SUBKEYS = {"modules", "nodes"}


# ============================================================================
# Session persistence configuration
# ============================================================================

# Default session audit SQLite file path (relative to the service startup directory)
DEFAULT_SESSION_DB_PATH = "data/dialogue.db"

# Default knowledge base SQLite file path (product/customer-service knowledge, scope-isolated)
DEFAULT_KNOWLEDGE_DB_PATH = "data/knowledge.db"

# Session history compression: triggers when estimated tokens exceed the threshold
# (0 = off). The estimate is a character approximation
# (CJK×2 + others×0.25, +4 per message), not an exact tokenizer
DEFAULT_SESSION_COMPRESS_TOKEN_THRESHOLD = 6000

# Number of recent messages kept after compression (tool rows included)
DEFAULT_SESSION_COMPRESS_RETAIN_COUNT = 12


# ============================================================================
# Configuration loading
# ============================================================================

# Path injected by the host at boot (host/config); None = resolve via env/CWD
_CONFIG_PATH: Optional[str] = None


def set_config_path(path: str) -> None:
    """Record the config file location (called by the host at boot).

    Tests may also point this at a fixture file; an explicit ``config_path``
    argument to load_config/get_llm_config still wins over this.
    """
    global _CONFIG_PATH
    _CONFIG_PATH = path


def _get_config_path() -> Path:
    """Get the path of local_config.yaml.

    Order: ``set_config_path`` (host boot) > ``$NEXUS_CONFIG`` > CWD probes
    (``host/config/local_config.yaml``, ``config/local_config.yaml`` — the
    latter keeps bare CLI/pytest runs working from a repo root).
    """
    if _CONFIG_PATH:
        return Path(_CONFIG_PATH)
    env = os.environ.get("NEXUS_CONFIG")
    if env:
        return Path(env)
    for candidate in ("host/config/local_config.yaml", "config/local_config.yaml"):
        if Path(candidate).exists():
            return Path(candidate)
    raise FileNotFoundError(
        "配置文件不存在（已探测 host/config/local_config.yaml 与 "
        "config/local_config.yaml）\n"
        "请创建 host/config/local_config.yaml，或经 nexus.settings.set_config_path"
        " / 环境变量 NEXUS_CONFIG 指定路径"
    )


def _validate_llm_config(llm_config: Dict[str, Any]) -> None:
    """Validate the completeness and legality of the LLM config.

    Args:
        llm_config: llm config dict parsed from yaml.

    Raises:
        ValueError: when required fields are missing or unknown fields present.
    """
    if not isinstance(llm_config, dict):
        raise ValueError(
            f"llm 配置应为字典类型，实际为: {type(llm_config).__name__}"
        )

    missing = _LLM_REQUIRED_FIELDS - set(llm_config.keys())
    if missing:
        raise ValueError(
            f"llm 配置缺少必填字段: {sorted(missing)}\n"
            f"必填字段: {sorted(_LLM_REQUIRED_FIELDS)}"
        )

    # Unknown fields: warn only, do not block execution
    unknown = set(llm_config.keys()) - _LLM_ALL_FIELDS
    if unknown:
        import logging
        logging.getLogger(__name__).warning(
            "llm 配置包含未知字段将被忽略: %s", sorted(unknown)
        )


def _convert_legacy_llm(llm_cfg: Dict[str, Any]):
    """Legacy top-level llm: node → (llm_providers, llm_default) (spec §6)."""
    conn = {
        k: llm_cfg[k] for k in _CONNECTION_FIELDS
        if llm_cfg.get(k) not in (None, "")
    }
    orch = {k: v for k, v in llm_cfg.items() if k not in _CONNECTION_FIELDS}
    providers = {llm_cfg["code"]: conn} if conn else {}
    return providers, orch


def _validate_pattern_llm(pattern_llm: Dict[str, Any]) -> None:
    """pattern_llm vocabulary and nesting validation: unknown fields are stripped
    after a warning; modules/nodes nested again inside modules/nodes count as
    illegal nesting and are emptied after a warning."""
    for pcode, pcfg in pattern_llm.items():
        if not isinstance(pcfg, dict):
            raise ValueError(f"pattern_llm.{pcode} 应为字典，实际为: {type(pcfg).__name__}")
        for key in list(pcfg.keys()):
            if key in _PATTERN_LLM_SUBKEYS:
                if not isinstance(pcfg[key], dict):
                    raise ValueError(
                        f"pattern_llm.{pcode}.{key} 应为字典，"
                        f"实际为: {type(pcfg[key]).__name__}")
                for sub_code, sub_cfg in pcfg[key].items():
                    if not isinstance(sub_cfg, dict):
                        raise ValueError(
                            f"pattern_llm.{pcode}.{key}.{sub_code} 应为字典")
                    bad = set(sub_cfg.keys()) & _PATTERN_LLM_SUBKEYS
                    unknown = set(sub_cfg.keys()) - _ORCHESTRATION_FIELDS
                    if bad or unknown:
                        logging.getLogger(__name__).warning(
                            "pattern_llm.%s.%s.%s 含非法嵌套/未知字段 %s，已忽略",
                            pcode, key, sub_code, sorted(bad | unknown))
                        pattern_llm[pcode][key][sub_code] = {
                            k: v for k, v in sub_cfg.items()
                            if k in _ORCHESTRATION_FIELDS
                        }
            elif key not in _ORCHESTRATION_FIELDS:
                logging.getLogger(__name__).warning(
                    "pattern_llm.%s 含未知字段 '%s'，已忽略", pcode, key)
                pattern_llm[pcode].pop(key)


def _validate_llm_providers(providers: Dict[str, Any]) -> None:
    """Fields in each llm_providers section outside the connection vocabulary →
    stripped after a warning (spec §3.4)."""
    for code, conn in providers.items():
        if not isinstance(conn, dict):
            raise ValueError(f"llm_providers.{code} 应为字典")
        unknown = set(conn.keys()) - _CONNECTION_FIELDS
        if unknown:
            logging.getLogger(__name__).warning(
                "llm_providers.%s 含未知连接字段 %s，已忽略", code, sorted(unknown))
            providers[code] = {k: v for k, v in conn.items()
                               if k in _CONNECTION_FIELDS}


def load_config(config_path: str = "") -> Dict[str, Any]:
    """Read the local configuration from local_config.yaml and return it.

    Args:
        config_path: optional; explicit config file path. When empty, looks up
                     ``config/local_config.yaml`` automatically.

    Returns:
        Config dict containing the ``llm_providers`` / ``llm_default`` /
        ``pattern_llm`` / ``session_db_path`` keys. A legacy ``llm:`` node is
        converted automatically at load time into ``llm_providers`` +
        ``llm_default``.

    Raises:
        FileNotFoundError: when the config file does not exist.
        ValueError: when the config format is illegal (missing required fields etc.).

    Example:
        >>> config = load_config()
        >>> llm_cfg = config["llm_default"]
        >>> print(llm_cfg["code"])   # "openai"
        >>> print(llm_cfg["model"])  # "qwen3.8-max"
    """
    if config_path:
        path = Path(config_path)
    else:
        path = _get_config_path()

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if raw is None:
        raise ValueError(
            f"配置文件 {path} 内容为空，请按格式填写 llm 配置"
        )

    if not isinstance(raw, dict):
        raise ValueError(
            f"配置文件顶层应为字典，实际为: {type(raw).__name__}"
        )

    # Extract LLM config: new structure (llm_providers/llm_default/pattern_llm) or legacy llm node
    has_legacy = raw.get("llm") is not None
    has_new = raw.get("llm_providers") is not None or raw.get("llm_default") is not None
    if has_legacy and has_new:
        raise ValueError(
            f"配置文件 {path} 中旧 'llm:' 节点与 'llm_providers:'/'llm_default:' "
            f"并存，请改写为新结构（spec 2026-09-02 §6）"
        )
    if has_legacy:
        llm_cfg = raw["llm"]
        _validate_llm_config(llm_cfg)  # legacy node keeps the legacy validation (code/model required)
        llm_providers, llm_default = _convert_legacy_llm(llm_cfg)
    elif raw.get("llm_default") is not None:
        llm_default = raw["llm_default"]
        if not isinstance(llm_default, dict):
            raise ValueError("llm_default 应为字典")
        _validate_llm_config(llm_default)  # code/model required
        llm_providers = raw.get("llm_providers") or {}
    else:
        raise ValueError(
            f"配置文件 {path} 缺少 'llm_default'（或旧式 'llm'）节点"
        )
    if not isinstance(llm_providers, dict):
        raise ValueError("llm_providers 应为字典")
    _validate_llm_providers(llm_providers)
    pattern_llm = raw.get("pattern_llm") or {}
    if not isinstance(pattern_llm, dict):
        raise ValueError("pattern_llm 应为字典")
    _validate_pattern_llm(pattern_llm)

    return {
        "llm_providers": llm_providers,
        "llm_default": llm_default,
        "pattern_llm": pattern_llm,
        # Session persistence SQLite path (optional, default data/dialogue.db)
        "session_db_path": raw.get("session_db_path", DEFAULT_SESSION_DB_PATH),
        # Knowledge base SQLite path (optional, default data/knowledge.db)
        "knowledge_db_path": raw.get("knowledge_db_path", DEFAULT_KNOWLEDGE_DB_PATH),
        # Session history compression (optional; threshold 0 = off, defaults 6000 / keep 12)
        "session_compress_token_threshold": int(raw.get(
            "session_compress_token_threshold",
            DEFAULT_SESSION_COMPRESS_TOKEN_THRESHOLD)),
        "session_compress_retain_count": int(raw.get(
            "session_compress_retain_count",
            DEFAULT_SESSION_COMPRESS_RETAIN_COUNT)),
        # More top-level nodes may be added later, e.g. "dialogue", "logging", "storage"
    }


def _merge_connection(orch: Dict[str, Any], providers: Dict[str, Any]) -> Dict[str, Any]:
    """Orchestration result ⊕ the provider connection section for its code
    (empty section when absent, falling back to registry defaults)."""
    return {**providers.get(orch.get("code", ""), {}), **orch}


def _resolve_layered(cfg: Dict[str, Any], pattern_code: str,
                     module_code: str, node_code: str) -> Dict[str, Any]:
    """llm_default ⊕ pattern ⊕ module ⊕ node shallow-merged layer by layer (spec §3.2).

    Unconfigured/unknown codes silently fall back to the shallower layer with a warning.
    """
    merged = dict(cfg["llm_default"])
    pcfg = cfg.get("pattern_llm", {}).get(pattern_code) if pattern_code else None
    if pattern_code and pcfg is None:
        logging.getLogger(__name__).debug(
            "pattern_llm 未配置 pattern '%s'，回退全局默认", pattern_code)
    if pcfg:
        merged.update({k: v for k, v in pcfg.items() if k not in _PATTERN_LLM_SUBKEYS})
        for sub_key, code, label in (
            ("modules", module_code, "module"),
            ("nodes", node_code, "node"),
        ):
            if not code:
                continue
            sub_cfg = (pcfg.get(sub_key) or {}).get(code)
            if sub_cfg is None:
                logging.getLogger(__name__).warning(
                    "pattern '%s' 未配置 %s '%s'，回退更浅层", pattern_code, label, code)
                continue
            merged.update(sub_cfg)
    return merged


def get_llm_config(pattern_code: str = "", module_code: str = "",
                   node_code: str = "", override: Optional[Dict[str, Any]] = None,
                   config_path: str = "") -> Dict[str, Any]:
    """Resolve the LLM config for the current position (spec §3.3 / §4.1).

    override not None: skip the three-tier resolution and only try to merge in
    the llm_providers[override.code] connection section; a yaml load failure
    silently degrades to an empty connection section (keeps offline tests sealed).
    Otherwise: merge the three tiers, then merge in the connection layer; a yaml
    load failure raises as usual.
    """
    if override is not None:
        # override carries only user-explicit fields (code/model may each be
        # missing); when code is missing, backfill from llm_default
        # (build_provider depends on llm_config["code"])
        merged = dict(override)
        code = merged.get("code") or ""
        try:
            cfg = load_config(config_path)
        except Exception:
            logging.getLogger(__name__).warning(
                "override 路径加载配置失败，连接层降级为空: %s", code or None)
            cfg = {}
        default = cfg.get("llm_default") or {}
        if not code and default.get("code"):
            merged["code"] = default["code"]
        if not merged.get("model") and default.get("model"):
            # When the CLI picks "维持 config 配置", override carries only code;
            # a missing model would make run_agent raise KeyError on
            # llm_config["model"], so backfill it just like code
            merged["model"] = default["model"]
        return _merge_connection(merged, cfg.get("llm_providers", {}))
    cfg = load_config(config_path)
    return _merge_connection(
        _resolve_layered(cfg, pattern_code, module_code, node_code),
        cfg.get("llm_providers", {}),
    )


def get_session_db_path(config_path: str = "") -> str:
    """Convenience method: return the session persistence SQLite file path.

    Equivalent to ``load_config(config_path)["session_db_path"]``.
    """
    return load_config(config_path)["session_db_path"]


def get_knowledge_db_path(config_path: str = "") -> str:
    """Convenience method: return the knowledge base SQLite file path.

    Equivalent to ``load_config(config_path)["knowledge_db_path"]``.
    """
    return load_config(config_path)["knowledge_db_path"]


def get_session_compress_config(config_path: str = "") -> tuple:
    """Convenience method: return (compression token threshold, retain count); threshold 0 = off."""
    cfg = load_config(config_path)
    return (
        cfg["session_compress_token_threshold"],
        cfg["session_compress_retain_count"],
    )
