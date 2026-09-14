"""Kernel runtime settings — schema + resolution for local_config.yaml.

The kernel owns the settings *schema* (three-tier LLM orchestration,
compression, DB paths); only the host knows where the yaml file lives:
host/config calls ``set_config_path()`` at boot. LLM config fields come from
``ProviderEntry`` / ``BaseLLMProvider`` in ``nexus/llm/provider.py`` plus
``OpenAICompatibleProvider`` in ``atoms/providers/dashscope_provider.py``.
"""

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

# ============================================================================
# Required and optional LLM config fields (from ProviderEntry in nexus/llm/provider.py)
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

_PATTERN_LLM_SUBKEYS = {"nodes"}  # plan-⑧: the modules sub-key is gone


# ============================================================================
# MCP server configuration (deep research / generic tool gateway)
# ============================================================================

# Legal transports for mcp_servers.<name>.transport
_MCP_TRANSPORTS = {"stdio", "sse", "streamable_http"}

# Fields allowed per mcp_servers.<name> entry (after normalization every entry
# also carries a tool_name_prefix, default "")
_MCP_SERVER_FIELDS = {
    "transport", "command", "args", "env", "url", "headers",
    "allowed_patterns", "tool_name_prefix",
}


def _validate_mcp_servers(raw: Any) -> Dict[str, Any]:
    """Validate and normalize the ``mcp_servers:`` node (same fail-fast
    style as _validate_llm_config — a config error surfaces at load time,
    never silently swallowed).

    Shape (see host/config/local_config.example.yaml for a commented example):

    .. code-block:: yaml

        mcp_servers:
          websearch:                  # server name -> toolset mcp-websearch
            transport: stdio          # stdio | sse | streamable_http
            command: npx
            args: ["-y", "@mcp/server-fetch"]
            allowed_patterns: ["deep_research"]   # pattern codes; ["*"] = all
            tool_name_prefix: ""      # optional cross-server collision guard

    Returns a normalized copy (each entry gets ``tool_name_prefix`` defaulted
    to ""). An absent / empty node returns ``{}`` — the whole MCP chain is a
    no-op until servers are configured.

    Raises:
        ValueError: transport outside _MCP_TRANSPORTS; stdio missing
            ``command``; sse/streamable_http missing ``url``; entry not a
            dict; ``allowed_patterns`` not a list of strings.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"mcp_servers 应为字典（server 名 -> 配置），实际为: {type(raw).__name__}"
        )

    normalized: Dict[str, Any] = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise ValueError(
                f"mcp_servers.{name} 应为字典，实际为: {type(cfg).__name__}"
            )
        transport = cfg.get("transport")
        if transport not in _MCP_TRANSPORTS:
            raise ValueError(
                f"mcp_servers.{name}.transport 非法: {transport!r}，"
                f"合法值: {sorted(_MCP_TRANSPORTS)}"
            )
        if transport == "stdio" and not cfg.get("command"):
            raise ValueError(f"mcp_servers.{name} 使用 stdio 传输，缺少必填字段 command")
        if transport in ("sse", "streamable_http") and not cfg.get("url"):
            raise ValueError(
                f"mcp_servers.{name} 使用 {transport} 传输，缺少必填字段 url"
            )
        allowed = cfg.get("allowed_patterns")
        if allowed is not None:
            if (not isinstance(allowed, list)
                    or not all(isinstance(p, str) for p in allowed)
                    or not allowed):
                raise ValueError(
                    f"mcp_servers.{name}.allowed_patterns 应为非空字符串列表"
                    f"（pattern code 或 \"*\"），实际为: {allowed!r}"
                )
        entry = {k: v for k, v in cfg.items() if k in _MCP_SERVER_FIELDS}
        entry["tool_name_prefix"] = str(cfg.get("tool_name_prefix", "") or "")
        normalized[name] = entry
    return normalized


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

# delegate_task sub-agent guardrails (config ``subagent_tool`` section, all
# optional). timeout caps the WHOLE sub ReAct loop (asyncio.wait_for), max_
# rounds caps its tool rounds, max_result_chars truncates the final content
# carried back into the parent agent's context.
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 120
DEFAULT_SUBAGENT_MAX_ROUNDS = 8
DEFAULT_SUBAGENT_MAX_RESULT_CHARS = 8000

# run_workflow guardrails (config ``workflow_tool`` section, all optional).
# timeout caps the WHOLE topology (all leaves + judge calls); max_rounds caps
# each leaf sub-loop; max_width caps parallel fan-out width (aligned with
# graph fanout's DEFAULT_MAX_FANOUT); max_cycles / max_iterations cap the
# adversarial / loop-until-done iteration counts; max_result_chars truncates
# the final content; trace_cap caps the returned steps list.
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 300
DEFAULT_WORKFLOW_MAX_ROUNDS = 8
DEFAULT_WORKFLOW_MAX_WIDTH = 8
DEFAULT_WORKFLOW_MAX_CYCLES = 3
DEFAULT_WORKFLOW_MAX_ITERATIONS = 5
DEFAULT_WORKFLOW_MAX_RESULT_CHARS = 8000
DEFAULT_WORKFLOW_TRACE_CAP = 32

# bash / run_python guardrails (config ``shell_tool`` section, all optional).
# timeout caps ONE command / script run (args may only lower it); max_output_
# chars truncates stdout and stderr each before the payload returns to the
# model's context.
DEFAULT_SHELL_TIMEOUT_SECONDS = 60
DEFAULT_SHELL_MAX_OUTPUT_CHARS = 20000

# File tool guardrails (config ``file_tool`` section, all optional).
# max_read_chars caps a read_text payload; max_write_chars rejects oversized
# write_text content; max_list_entries caps list_dir entries; max_matches
# caps search_files hits (args may only lower it); max_edit_chars caps the
# file size edit_file will touch; max_find_results caps find_files output.
DEFAULT_FILE_MAX_READ_CHARS = 50000
DEFAULT_FILE_MAX_WRITE_CHARS = 200000
DEFAULT_FILE_MAX_LIST_ENTRIES = 500
DEFAULT_FILE_MAX_MATCHES = 100
DEFAULT_FILE_MAX_EDIT_CHARS = 200000
DEFAULT_FILE_MAX_FIND_RESULTS = 500

# Session task-list guardrails (config ``tasks_tool`` section, all optional).
DEFAULT_TASKS_MAX_TASKS = 50
DEFAULT_TASKS_MAX_TASK_CHARS = 500

# Cron scheduler guardrails (config ``cron_tool`` section, all optional).
# fire_timeout caps ONE fire's sub-agent run (create args may only lower it);
# tick_seconds is the scheduler loop cadence; jobs_path is the JSON store
# (atomic write, survives restarts; missed fires while down are not replayed).
DEFAULT_CRON_MAX_JOBS = 20
DEFAULT_CRON_FIRE_TIMEOUT_SECONDS = 300
DEFAULT_CRON_MAX_ROUNDS = 8
DEFAULT_CRON_HISTORY_CAP = 10
DEFAULT_CRON_MAX_INPUT_CHARS = 8000
DEFAULT_CRON_MAX_RESULT_CHARS = 4000
DEFAULT_CRON_JOBS_PATH = "data/cron_jobs.json"
DEFAULT_CRON_TICK_SECONDS = 20

# Tool guard（config ``tool_guard`` 节，P4 工具执行前危险操作播报——
# atoms/hooks/tool_guard.py）。enabled 总开关；llm_fallback 控制旁路的
# 轻量 LLM 判读（规则未命中高危但信号可疑时送审）；llm_max_input_chars
# 截断送审参数；llm_max_queue 有界队列（满则丢弃新条）；llm_timeout_
# seconds 单次判读超时；llm 为叠加在 ambient 连接之上的 judge 模型
# 覆盖（code/model/max_tokens/...，推荐指向便宜小模型）。
DEFAULT_TOOL_GUARD_ENABLED = True
DEFAULT_TOOL_GUARD_LLM_FALLBACK = True
DEFAULT_TOOL_GUARD_LLM_MAX_INPUT_CHARS = 2000
DEFAULT_TOOL_GUARD_LLM_MAX_QUEUE = 64
DEFAULT_TOOL_GUARD_LLM_TIMEOUT_SECONDS = 15.0

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


# ============================================================================
# Config cache — (mtime_ns, size) fingerprint per resolved path
# ============================================================================

# resolved path -> ((mtime_ns, size), parsed config dict); guarded by a lock
# (chat turns resolve llm_config on the event loop, CLI/test threads may load
# concurrently — parse happens outside the lock, only the dict swap is atomic)
_CONFIG_CACHE: Dict[str, Tuple[Tuple[int, int], Dict[str, Any]]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()


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

    mtime cache (this function is the hot path of the per-turn R1 refresh):
    when the file's stat fingerprint (mtime_ns, size) is unchanged, return a
    deep copy of the last parsed result — no re-read, no re-validation; only
    when it changed (or the cache was invalidated) run the full
    read→validate→normalize pipeline. Editor atomic writes (temp file +
    rename) change the inode, so the fingerprint necessarily changes —
    naturally compatible. A parse failure does not populate the cache (the
    last valid result stays usable until the file is fixed).

    Args:
        config_path: optional; explicit config file path. When empty, looks up
                     ``config/local_config.yaml`` automatically.

    Returns:
        Config dict containing the ``llm_providers`` / ``llm_default`` /
        ``pattern_llm`` / ``session_db_path`` keys. A legacy ``llm:`` node is
        converted automatically at load time into ``llm_providers`` +
        ``llm_default``. **Returns a deep copy**: caller-side rewrites (e.g.
        pattern_llm validation stripping illegal keys in place) cannot
        pollute the cache.

    Raises:
        FileNotFoundError: when the config file does not exist.
        ValueError: when the config format is illegal (missing required fields etc.).

    Example:
        >>> config = load_config()
        >>> llm_cfg = config["llm_default"]
        >>> print(llm_cfg["code"])   # "dashscope"
        >>> print(llm_cfg["model"])  # "qwen3.8-max"
    """
    if config_path:
        path = Path(config_path)
    else:
        path = _get_config_path()

    resolved = str(path.resolve())
    stat = path.stat()  # FileNotFoundError propagates with its original semantics
    fingerprint = (stat.st_mtime_ns, stat.st_size)

    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get(resolved)
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])

    cfg = _parse_config_file(path)

    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[resolved] = (fingerprint, copy.deepcopy(cfg))
    return cfg


def reload_config(config_path: str = "") -> Dict[str, Any]:
    """Force-reload the config (programmatic entry): drop the cache, then run
    a full load_config.

    With the mtime cache this normally needs no manual call (a fingerprint
    change reloads automatically); it exists for callers that changed the
    system clock, or want the certainty of "the file was definitely re-read".
    """
    invalidate_config_cache(config_path)
    return load_config(config_path)


def invalidate_config_cache(config_path: str = "") -> None:
    """Drop the config cache (all of it, or one resolved path's entry).

    Tests use this between writes to the same path with an unchanged fingerprint
    (same mtime_ns + size within filesystem resolution); production rarely
    needs it — the fingerprint check in load_config handles real edits.
    """
    with _CONFIG_CACHE_LOCK:
        if config_path:
            resolved = str(Path(config_path).resolve())
            _CONFIG_CACHE.pop(resolved, None)
        else:
            _CONFIG_CACHE.clear()


def _parse_config_file(path: Path) -> Dict[str, Any]:
    """Read the file + validate + normalize (the cache-free core of load_config)."""
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

    subagent_tool = raw.get("subagent_tool") or {}
    if not isinstance(subagent_tool, dict):
        raise ValueError("subagent_tool 应为字典")

    workflow_tool = raw.get("workflow_tool") or {}
    if not isinstance(workflow_tool, dict):
        raise ValueError("workflow_tool 应为字典")

    shell_tool = raw.get("shell_tool") or {}
    if not isinstance(shell_tool, dict):
        raise ValueError("shell_tool 应为字典")

    file_tool = raw.get("file_tool") or {}
    if not isinstance(file_tool, dict):
        raise ValueError("file_tool 应为字典")

    tasks_tool = raw.get("tasks_tool") or {}
    if not isinstance(tasks_tool, dict):
        raise ValueError("tasks_tool 应为字典")

    cron_tool = raw.get("cron_tool") or {}
    if not isinstance(cron_tool, dict):
        raise ValueError("cron_tool 应为字典")

    tool_guard = raw.get("tool_guard") or {}
    if not isinstance(tool_guard, dict):
        raise ValueError("tool_guard 应为字典")
    tool_guard_llm = tool_guard.get("llm") or {}
    if not isinstance(tool_guard_llm, dict):
        raise ValueError("tool_guard.llm 应为字典")

    return {
        "llm_providers": llm_providers,
        "llm_default": llm_default,
        "pattern_llm": pattern_llm,
        # MCP servers (optional; empty dict = the whole MCP chain is a no-op).
        # Validated + normalized by _validate_mcp_servers (fail-fast on
        # transport/required-field errors)
        "mcp_servers": _validate_mcp_servers(raw.get("mcp_servers")),
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
        # delegate_task sub-agent guardrails (optional; defaults see
        # DEFAULT_SUBAGENT_* constants)
        "subagent_tool": {
            "timeout_seconds": int(subagent_tool.get(
                "timeout_seconds", DEFAULT_SUBAGENT_TIMEOUT_SECONDS)),
            "max_rounds": int(subagent_tool.get(
                "max_rounds", DEFAULT_SUBAGENT_MAX_ROUNDS)),
            "max_result_chars": int(subagent_tool.get(
                "max_result_chars", DEFAULT_SUBAGENT_MAX_RESULT_CHARS)),
        },
        # run_workflow topology guardrails (optional; defaults see
        # DEFAULT_WORKFLOW_* constants)
        "workflow_tool": {
            "timeout_seconds": int(workflow_tool.get(
                "timeout_seconds", DEFAULT_WORKFLOW_TIMEOUT_SECONDS)),
            "max_rounds": int(workflow_tool.get(
                "max_rounds", DEFAULT_WORKFLOW_MAX_ROUNDS)),
            "max_width": int(workflow_tool.get(
                "max_width", DEFAULT_WORKFLOW_MAX_WIDTH)),
            "max_cycles": int(workflow_tool.get(
                "max_cycles", DEFAULT_WORKFLOW_MAX_CYCLES)),
            "max_iterations": int(workflow_tool.get(
                "max_iterations", DEFAULT_WORKFLOW_MAX_ITERATIONS)),
            "max_result_chars": int(workflow_tool.get(
                "max_result_chars", DEFAULT_WORKFLOW_MAX_RESULT_CHARS)),
            "trace_cap": int(workflow_tool.get(
                "trace_cap", DEFAULT_WORKFLOW_TRACE_CAP)),
        },
        # bash / run_python guardrails (optional; defaults see
        # DEFAULT_SHELL_* constants)
        "shell_tool": {
            "timeout_seconds": int(shell_tool.get(
                "timeout_seconds", DEFAULT_SHELL_TIMEOUT_SECONDS)),
            "max_output_chars": int(shell_tool.get(
                "max_output_chars", DEFAULT_SHELL_MAX_OUTPUT_CHARS)),
        },
        # read_text / write_text / list_dir / search_files / edit_file /
        # find_files guardrails (optional; defaults see DEFAULT_FILE_* consts)
        "file_tool": {
            "max_read_chars": int(file_tool.get(
                "max_read_chars", DEFAULT_FILE_MAX_READ_CHARS)),
            "max_write_chars": int(file_tool.get(
                "max_write_chars", DEFAULT_FILE_MAX_WRITE_CHARS)),
            "max_list_entries": int(file_tool.get(
                "max_list_entries", DEFAULT_FILE_MAX_LIST_ENTRIES)),
            "max_matches": int(file_tool.get(
                "max_matches", DEFAULT_FILE_MAX_MATCHES)),
            "max_edit_chars": int(file_tool.get(
                "max_edit_chars", DEFAULT_FILE_MAX_EDIT_CHARS)),
            "max_find_results": int(file_tool.get(
                "max_find_results", DEFAULT_FILE_MAX_FIND_RESULTS)),
        },
        # read_tasks / write_tasks guardrails (optional; defaults see
        # DEFAULT_TASKS_* constants)
        "tasks_tool": {
            "max_tasks": int(tasks_tool.get(
                "max_tasks", DEFAULT_TASKS_MAX_TASKS)),
            "max_task_chars": int(tasks_tool.get(
                "max_task_chars", DEFAULT_TASKS_MAX_TASK_CHARS)),
        },
        # cron scheduler guardrails (optional; defaults see DEFAULT_CRON_*
        # constants)
        "cron_tool": {
            "max_jobs": int(cron_tool.get(
                "max_jobs", DEFAULT_CRON_MAX_JOBS)),
            "fire_timeout_seconds": int(cron_tool.get(
                "fire_timeout_seconds", DEFAULT_CRON_FIRE_TIMEOUT_SECONDS)),
            "max_rounds": int(cron_tool.get(
                "max_rounds", DEFAULT_CRON_MAX_ROUNDS)),
            "history_cap": int(cron_tool.get(
                "history_cap", DEFAULT_CRON_HISTORY_CAP)),
            "max_input_chars": int(cron_tool.get(
                "max_input_chars", DEFAULT_CRON_MAX_INPUT_CHARS)),
            "max_result_chars": int(cron_tool.get(
                "max_result_chars", DEFAULT_CRON_MAX_RESULT_CHARS)),
            "jobs_path": str(cron_tool.get(
                "jobs_path", DEFAULT_CRON_JOBS_PATH)),
            "tick_seconds": int(cron_tool.get(
                "tick_seconds", DEFAULT_CRON_TICK_SECONDS)),
        },
        # tool guard（可选；defaults see DEFAULT_TOOL_GUARD_* constants）
        "tool_guard": {
            "enabled": bool(tool_guard.get(
                "enabled", DEFAULT_TOOL_GUARD_ENABLED)),
            "llm_fallback": bool(tool_guard.get(
                "llm_fallback", DEFAULT_TOOL_GUARD_LLM_FALLBACK)),
            "llm_max_input_chars": int(tool_guard.get(
                "llm_max_input_chars", DEFAULT_TOOL_GUARD_LLM_MAX_INPUT_CHARS)),
            "llm_max_queue": int(tool_guard.get(
                "llm_max_queue", DEFAULT_TOOL_GUARD_LLM_MAX_QUEUE)),
            "llm_timeout_seconds": float(tool_guard.get(
                "llm_timeout_seconds", DEFAULT_TOOL_GUARD_LLM_TIMEOUT_SECONDS)),
            "llm": dict(tool_guard_llm),
        },
        # More top-level nodes may be added later, e.g. "dialogue", "logging", "storage"
    }


def _merge_connection(orch: Dict[str, Any], providers: Dict[str, Any]) -> Dict[str, Any]:
    """Orchestration result ⊕ the provider connection section for its code
    (empty section when absent, falling back to registry defaults)."""
    return {**providers.get(orch.get("code", ""), {}), **orch}


def _resolve_layered(cfg: Dict[str, Any], pattern_code: str,
                     node_code: str) -> Dict[str, Any]:
    """llm_default ⊕ pattern ⊕ node shallow-merged layer by layer (spec §3.2;
    plan-⑧: the module sub-layer is gone with the module layer).

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


def get_llm_config(pattern_code: str = "", node_code: str = "",
                   override: Optional[Dict[str, Any]] = None,
                   config_path: str = "") -> Dict[str, Any]:
    """Resolve the LLM config for the current position (spec §3.3 / §4.1).

    override not None (the plugins["llm"] declaration resolves as
    ``{"code": ...}`` here; the CLI's explicit pick wins): skip the layered
    resolution and only try to merge in the llm_providers[override.code]
    connection section; a yaml load failure silently degrades to an empty
    connection section (keeps offline tests sealed). Otherwise: merge the
    layers, then merge in the connection layer; a yaml load failure raises
    as usual.
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
            # a missing model would make the loop executor raise KeyError on
            # llm_config["model"], so backfill it just like code
            merged["model"] = default["model"]
        return _merge_connection(merged, cfg.get("llm_providers", {}))
    cfg = load_config(config_path)
    return _merge_connection(
        _resolve_layered(cfg, pattern_code, node_code),
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


def get_mcp_servers(config_path: str = "") -> Dict[str, Any]:
    """Convenience method: return the validated ``mcp_servers`` config.

    Equivalent to ``load_config(config_path)["mcp_servers"]`` — an empty dict
    when no servers are configured (the MCP chain then stays a no-op).
    """
    return load_config(config_path)["mcp_servers"]


def get_session_compress_config(config_path: str = "") -> tuple:
    """Convenience method: return (compression token threshold, retain count); threshold 0 = off."""
    cfg = load_config(config_path)
    return (
        cfg["session_compress_token_threshold"],
        cfg["session_compress_retain_count"],
    )


def get_subagent_tool_config(config_path: str = "") -> Dict[str, Any]:
    """Return the ``delegate_task`` sub-agent guardrails.

    Equivalent to ``load_config(config_path)["subagent_tool"]`` —
    ``{"timeout_seconds", "max_rounds", "max_result_chars"}``, all optional
    in the config file (defaults see the DEFAULT_SUBAGENT_* constants).
    """
    return load_config(config_path)["subagent_tool"]


def get_workflow_tool_config(config_path: str = "") -> Dict[str, Any]:
    """Return the ``run_workflow`` topology guardrails.

    Equivalent to ``load_config(config_path)["workflow_tool"]`` —
    ``{"timeout_seconds", "max_rounds", "max_width", "max_cycles",
    "max_iterations", "max_result_chars", "trace_cap"}``, all optional in
    the config file (defaults see the DEFAULT_WORKFLOW_* constants).
    """
    return load_config(config_path)["workflow_tool"]


def get_shell_tool_config(config_path: str = "") -> Dict[str, Any]:
    """Return the ``bash`` / ``run_python`` guardrails.

    Equivalent to ``load_config(config_path)["shell_tool"]`` —
    ``{"timeout_seconds", "max_output_chars"}``, all optional in the
    config file (defaults see the DEFAULT_SHELL_* constants).
    """
    return load_config(config_path)["shell_tool"]


def get_file_tool_config(config_path: str = "") -> Dict[str, Any]:
    """Return the file tools guardrails.

    Equivalent to ``load_config(config_path)["file_tool"]`` —
    ``{"max_read_chars", "max_write_chars", "max_list_entries",
    "max_matches", "max_edit_chars", "max_find_results"}``, all optional
    in the config file (defaults see the DEFAULT_FILE_* constants).
    """
    return load_config(config_path)["file_tool"]


def get_tasks_tool_config(config_path: str = "") -> Dict[str, Any]:
    """Return the session task-list guardrails.

    Equivalent to ``load_config(config_path)["tasks_tool"]`` —
    ``{"max_tasks", "max_task_chars"}``, all optional in the config file
    (defaults see the DEFAULT_TASKS_* constants).
    """
    return load_config(config_path)["tasks_tool"]


def get_cron_tool_config(config_path: str = "") -> Dict[str, Any]:
    """Return the cron scheduler guardrails.

    Equivalent to ``load_config(config_path)["cron_tool"]`` —
    ``{"max_jobs", "fire_timeout_seconds", "max_rounds", "history_cap",
    "max_input_chars", "max_result_chars", "jobs_path", "tick_seconds"}``,
    all optional in the config file (defaults see DEFAULT_CRON_* constants).
    """
    return load_config(config_path)["cron_tool"]


def get_tool_guard_config(config_path: str = "") -> Dict[str, Any]:
    """Return the tool guard config (P4 危险操作播报).

    Equivalent to ``load_config(config_path)["tool_guard"]`` —
    ``{"enabled", "llm_fallback", "llm_max_input_chars", "llm_max_queue",
    "llm_timeout_seconds", "llm"}``, all optional in the config file
    (defaults see DEFAULT_TOOL_GUARD_* constants). ``llm`` is a passthrough
    dict of judge-model overrides merged over the ambient connection.
    """
    return load_config(config_path)["tool_guard"]
