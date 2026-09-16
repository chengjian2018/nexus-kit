"""Kernel runtime settings — schema + resolution for local_config.yaml.

The kernel owns the settings *schema* (three-tier LLM orchestration,
compression, DB paths); only the host knows where the yaml file lives:
host/config calls ``set_config_path()`` at boot. LLM config fields come from
``ProviderEntry`` / ``BaseLLMProvider`` in ``nexus/llm/provider.py`` plus
``OpenAICompatibleProvider`` in ``atoms/providers/dashscope_provider.py``.

On top of the global yaml sits the per-app overlay (``apps/*/config.yaml``):
orchestration / loop budgets / guardrails / custom bag, never credentials
(the connection layer stays global-only). See docs/design/app-config.md.
"""

import copy
import logging
import os
import re
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

# Connection fields: fields allowed in each llm_providers section (legacy llm: node splits by this)
_CONNECTION_FIELDS = {
    "api_base", "api_key", "api_key_env", "timeout", "max_retries",
}


# ============================================================================
# MCP server configuration (deep research / generic tool gateway)
# ============================================================================

# Legal transports for mcp_servers.<name>.transport
_MCP_TRANSPORTS = {"stdio", "sse", "streamable_http"}

# Fields allowed per mcp_servers.<name> entry (after normalization every entry
# also carries a tool_name_prefix, default "")
_MCP_SERVER_FIELDS = {
    "transport", "command", "args", "env", "url", "headers",
    "tool_name_prefix",
}

# $VAR / ${VAR} environment reference in mcp server string values (secrets
# stay out of the yaml — same discipline as llm_providers.api_key_env)
_MCP_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_mcp_env_refs(value: str, where: str) -> str:
    """Expand ``$VAR`` / ``${VAR}`` in an mcp server config string from the
    process environment.

    An unset variable keeps its raw reference and logs a warning —
    ``load_config()`` must stay environment-independent (bare calls run in
    tests / CLI tooling without provider secrets exported); the affected
    server then fails auth at connect time, with this warning as the pointer
    to the missing variable.
    """
    def _sub(match: "re.Match[str]") -> str:
        var = match.group(1) or match.group(2)
        resolved = os.environ.get(var)
        if not resolved:
            logging.getLogger(__name__).warning(
                "[config] mcp_servers.%s 引用的环境变量 $%s 未设置,保留原样"
                "(对应 server 连接时会因鉴权失败不可用)", where, var)
            return match.group(0)
        return resolved
    return _MCP_ENV_REF.sub(_sub, value)


def _expand_entry_env_refs(name: str, entry: Dict[str, Any]) -> None:
    """Expand env references across an entry's string values in place:
    command / url / args list items / env values / headers values (keys are
    not expanded)."""
    for field in ("command", "url"):
        if isinstance(entry.get(field), str):
            entry[field] = _expand_mcp_env_refs(entry[field], f"{name}.{field}")
    if isinstance(entry.get("args"), list):
        entry["args"] = [
            _expand_mcp_env_refs(a, f"{name}.args") if isinstance(a, str) else a
            for a in entry["args"]
        ]
    for section in ("env", "headers"):
        sec = entry.get(section)
        if isinstance(sec, dict):
            entry[section] = {
                k: _expand_mcp_env_refs(v, f"{name}.{section}.{k}")
                if isinstance(v, str) else v
                for k, v in sec.items()
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
            tool_name_prefix: ""      # optional cross-server collision guard

    Returns a normalized copy (each entry gets ``tool_name_prefix`` defaulted
    to ""). String values in command / url / args / env / headers support
    ``$VAR`` / ``${VAR}`` environment references (an unset variable keeps its
    raw text with a warning — see _expand_mcp_env_refs). An absent / empty
    node returns ``{}`` — the whole MCP chain is a no-op until servers are
    configured.

    Raises:
        ValueError: transport outside _MCP_TRANSPORTS; stdio missing
            ``command``; sse/streamable_http missing ``url``; entry not a
            dict.
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
        entry = {k: v for k, v in cfg.items() if k in _MCP_SERVER_FIELDS}
        entry["tool_name_prefix"] = str(cfg.get("tool_name_prefix", "") or "")
        _expand_entry_env_refs(name, entry)
        normalized[name] = entry
    return normalized


# ============================================================================
# ReAct loop guard (config ``loop`` section)
# ============================================================================

# Default tool-round budget (global ``loop.max_tool_rounds``; re-read every
# turn — app configs may refine per pattern / node, see get_loop_limits'
# three-layer resolution)
DEFAULT_LOOP_MAX_TOOL_ROUNDS = 10


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

# Skill-asset scan root (config ``skills`` section; consumed by
# nexus/skills.py). A relative dir resolves against the service startup
# directory (same semantics as the file tool) with ~ expansion; a missing
# root silences the whole skill chain (mirroring the mcp_servers: {} stance).
# A pattern may override it via config.skills_dir (app-bundled skills).
DEFAULT_SKILLS_DIR = "skills"

# Tool guard (config ``tool_guard`` section — P4 pre-execution announce of
# dangerous tool calls; atoms/hooks/tool_guard.py). ``enabled`` is the master
# switch; ``llm_fallback`` controls the bypass lightweight LLM review (rule
# miss but suspicious signals → send for adjudication); ``llm_max_input_chars``
# truncates the submitted args; ``llm_max_queue`` bounds the review queue
# (full queue drops new entries); ``llm_timeout_seconds`` is the per-review
# timeout; ``llm`` is a judge-model override layered on top of the ambient
# connection (code/model/max_tokens/... — point it at a cheap small model).
DEFAULT_TOOL_GUARD_ENABLED = True
# LLM review defaults to OFF (explicit opt-in): when the config lacks a
# tool_guard section, silently falling through to the ambient provider would
# ship tool args out of process — aligned with the conservative "rules on,
# LLM off" stance taken when config reads fail; deployments that want the
# review write llm_fallback: true explicitly.
DEFAULT_TOOL_GUARD_LLM_FALLBACK = False
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
    """Legacy top-level llm: node → (llm_providers, llm_default)."""
    conn = {
        k: llm_cfg[k] for k in _CONNECTION_FIELDS
        if llm_cfg.get(k) not in (None, "")
    }
    orch = {k: v for k, v in llm_cfg.items() if k not in _CONNECTION_FIELDS}
    providers = {llm_cfg["code"]: conn} if conn else {}
    return providers, orch


def _validate_llm_providers(providers: Dict[str, Any]) -> None:
    """Fields in each llm_providers section outside the connection vocabulary →
    stripped after a warning (unknown keys are never authoritative)."""
    for code, conn in providers.items():
        if not isinstance(conn, dict):
            raise ValueError(f"llm_providers.{code} 应为字典")
        unknown = set(conn.keys()) - _CONNECTION_FIELDS
        if unknown:
            logging.getLogger(__name__).warning(
                "llm_providers.%s 含未知连接字段 %s，已忽略", code, sorted(unknown))
            providers[code] = {k: v for k, v in conn.items()
                               if k in _CONNECTION_FIELDS}


# ============================================================================
# App-level config overlay: apps/*/config.yaml (spec 2026-09-15)
# ============================================================================

# Top-level vocabulary (``pattern`` is the required explicit binding key —
# the directory name ≠ pattern code; a wrong binding fails fast)
_APP_TOP_FIELDS = {
    "pattern", "llm", "nodes", "loop", "compression",
    "guardrails", "skills", "config",
}

# Connection fields appearing in an app file fail fast: app configs are
# committed to git, the connection layer (with secrets) may only live in the
# global llm_providers — the validation layer hard-rejects; a warn is not enough
_APP_LLM_CONNECTION_FIELDS = {"api_base", "api_key", "api_key_env"}

# Per-node entry vocabulary: only llm and loop.max_tool_rounds are open
_APP_NODE_FIELDS = {"llm", "loop"}
_APP_NODE_LOOP_FIELDS = {"max_tool_rounds"}

# Pattern-level loop vocabulary (node level only has max_tool_rounds —
# max_steps/max_fanout are graph-level budgets with no node dimension)
_APP_LOOP_FIELDS = {"max_tool_rounds", "max_steps", "max_fanout"}

# compression vocabulary (session-level; threshold 0 = compression off for the app)
_APP_COMPRESSION_FIELDS = {"threshold", "retain_count"}

# Guardrail section vocabulary: section names match the global 6 guardrail
# sections and field validation reuses the global sections' int()/str()
# coercion (the field sets parsed by each global local_config.yaml section
# are tabulated here — single source of truth for both). One exception,
# cron_tool: its vocabulary is deliberately narrower than the global section —
# jobs_path / tick_seconds are process-level infrastructure (jobs repo path /
# scheduler cadence); an app override would split jobs across files (add uses
# ambient, fire uses frozen, update/remove use global), so apps may not
# override them (written values get warn+dropped, same stance as illegal
# values); configure them in the global cron_tool section only.
_GUARDRAIL_FIELD_COERCERS: Dict[str, Dict[str, Any]] = {
    "subagent_tool": {
        "timeout_seconds": int, "max_rounds": int, "max_result_chars": int,
    },
    "workflow_tool": {
        "timeout_seconds": int, "max_rounds": int, "max_width": int,
        "max_cycles": int, "max_iterations": int, "max_result_chars": int,
        "trace_cap": int,
    },
    "shell_tool": {
        "timeout_seconds": int, "max_output_chars": int,
    },
    "file_tool": {
        "max_read_chars": int, "max_write_chars": int, "max_list_entries": int,
        "max_matches": int, "max_edit_chars": int, "max_find_results": int,
    },
    "tasks_tool": {
        "max_tasks": int, "max_task_chars": int,
    },
    "cron_tool": {
        "max_jobs": int, "fire_timeout_seconds": int, "max_rounds": int,
        "history_cap": int, "max_input_chars": int, "max_result_chars": int,
        # jobs_path / tick_seconds deliberately absent from the app
        # vocabulary: process-level infrastructure, see the table header
        # comment (the global cron_tool section can still configure them)
    },
}


def _positive_int(value: Any, default: int, field: str) -> int:
    """Coerce to int ≥ 1: bool / non-int / out-of-range values warn and fall
    back to the default (bool is an int subclass — a hand-written yaml true
    must be treated as illegal, not as 1)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        if value is not None:
            logging.getLogger(__name__).warning(
                "%s 应为 int ≥ 1，实际为 %r，按默认 %r 处理", field, value, default)
        return default
    return value


def _non_negative_int(value: Any, field: str) -> Optional[int]:
    """Coerce to int ≥ 0 (used by the app compression overlay): illegal values
    warn and get dropped; reads fall back to the global (None = no override)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        logging.getLogger(__name__).warning(
            "%s 应为 int ≥ 0，实际为 %r，已忽略", field, value)
        return None
    return value


def _validate_app_llm(llm_cfg: Any, where: str) -> Dict[str, Any]:
    """Validate the app-side llm vocabulary: reuses ``_LLM_ALL_FIELDS``;
    connection fields fail fast.

    Unknown in-vocabulary fields get warn+dropped (same as the global yaml).
    A ``code`` without ``model`` keeps the layered merge's backfill semantics —
    the shallower layer's model naturally survives; switching provider
    requires writing both together (see the design doc §4.1 note).
    """
    if llm_cfg is None:
        return {}
    if not isinstance(llm_cfg, dict):
        raise ValueError(f"{where} 应为字典，实际为: {type(llm_cfg).__name__}")
    conn = set(llm_cfg.keys()) & _APP_LLM_CONNECTION_FIELDS
    if conn:
        raise ValueError(
            f"{where} 含连接字段 {sorted(conn)}——连接层（含密钥）只允许出现在"
            f"全局 llm_providers，app 配置文件入库不带密钥")
    unknown = set(llm_cfg.keys()) - _LLM_ALL_FIELDS
    if unknown:
        logging.getLogger(__name__).warning(
            "%s 含未知字段 %s，已忽略", where, sorted(unknown))
    return {k: v for k, v in llm_cfg.items() if k in _LLM_ALL_FIELDS}


def _validate_app_loop(loop_cfg: Any, vocab, where: str) -> Dict[str, int]:
    """Validate the app-side loop section: in-vocabulary int ≥ 1; illegal
    values warn+drop (reads fall back to the shallower layer — the global
    loop section or the pattern level)."""
    if loop_cfg is None:
        return {}
    if not isinstance(loop_cfg, dict):
        raise ValueError(f"{where} 应为字典，实际为: {type(loop_cfg).__name__}")
    out: Dict[str, int] = {}
    for key, value in loop_cfg.items():
        if key not in vocab:
            logging.getLogger(__name__).warning(
                "%s 含未知键 '%s'，已忽略", where, key)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            logging.getLogger(__name__).warning(
                "%s.%s 应为 int ≥ 1，实际为 %r，已忽略", where, key, value)
            continue
        out[key] = value
    return out


def _validate_app_compression(comp_cfg: Any, where: str) -> Dict[str, int]:
    """Validate the app-side compression section: threshold / retain_count
    int ≥ 0; illegal values warn+drop (field-level override — absent fields
    keep the global session_compress_*)."""
    if comp_cfg is None:
        return {}
    if not isinstance(comp_cfg, dict):
        raise ValueError(f"{where} 应为字典，实际为: {type(comp_cfg).__name__}")
    out: Dict[str, int] = {}
    for key, value in comp_cfg.items():
        if key not in _APP_COMPRESSION_FIELDS:
            logging.getLogger(__name__).warning(
                "%s 含未知键 '%s'，已忽略", where, key)
            continue
        coerced = _non_negative_int(value, f"{where}.{key}")
        if coerced is not None:
            out[key] = coerced
    return out


def _validate_app_guardrails(gr_cfg: Any, where: str) -> Dict[str, Dict[str, Any]]:
    """Validate the app-side guardrails section: section names ∈ the global 6
    guardrail sections; field validation reuses the global sections' int()/str()
    coercion; unknown sections / fields / values warn+drop (reads fall back
    to the global section)."""
    if gr_cfg is None:
        return {}
    if not isinstance(gr_cfg, dict):
        raise ValueError(f"{where} 应为字典，实际为: {type(gr_cfg).__name__}")
    out: Dict[str, Dict[str, Any]] = {}
    for section, fields in gr_cfg.items():
        coercers = _GUARDRAIL_FIELD_COERCERS.get(section)
        if coercers is None:
            logging.getLogger(__name__).warning(
                "%s.%s 段名非法（合法: %s），已忽略",
                where, section, sorted(_GUARDRAIL_FIELD_COERCERS))
            continue
        if not isinstance(fields, dict):
            raise ValueError(
                f"{where}.{section} 应为字典，实际为: {type(fields).__name__}")
        section_out: Dict[str, Any] = {}
        for key, value in fields.items():
            fn = coercers.get(key)
            if fn is None:
                logging.getLogger(__name__).warning(
                    "%s.%s 含未知字段 '%s'，已忽略", where, section, key)
                continue
            try:
                section_out[key] = fn(value)
            except (TypeError, ValueError):
                logging.getLogger(__name__).warning(
                    "%s.%s.%s 值非法（%r），已忽略", where, section, key, value)
        out[section] = section_out
    return out


def _parse_app_config_file(path: Path) -> Dict[str, Any]:
    """Read one app config + validate + normalize (the cache-free core of
    _load_app_configs).

    The normalized view always has eight keys: pattern / llm / nodes / loop /
    compression / guardrails / skills / config (absent sections are empty
    dicts; reads fall back to global). Unknown keys warn+ignore; a missing
    pattern, connection fields, or structural errors (section not a dict)
    fail fast with a raise — an app config is an explicitly declared
    deployment artifact; errors surface early.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"app 配置 {path} 顶层应为字典，实际为: {type(raw).__name__}")

    pattern_code = raw.get("pattern")
    if not isinstance(pattern_code, str) or not pattern_code:
        raise ValueError(
            f"app 配置 {path} 缺少必填键 'pattern'（显式绑定 pattern code）")

    unknown = set(raw.keys()) - _APP_TOP_FIELDS
    if unknown:
        logging.getLogger(__name__).warning(
            "app 配置 %s 含未知键 %s，已忽略", path, sorted(unknown))

    nodes = raw.get("nodes") or {}
    if not isinstance(nodes, dict):
        raise ValueError(f"app 配置 {path} nodes 应为字典")
    nodes_view: Dict[str, Dict[str, Any]] = {}
    for node_code, ncfg in nodes.items():
        if not isinstance(ncfg, dict):
            raise ValueError(
                f"app 配置 {path} nodes.{node_code} 应为字典，"
                f"实际为: {type(ncfg).__name__}")
        unknown = set(ncfg.keys()) - _APP_NODE_FIELDS
        if unknown:
            logging.getLogger(__name__).warning(
                "app 配置 %s nodes.%s 含未知键 %s，已忽略",
                path, node_code, sorted(unknown))
        nodes_view[node_code] = {
            "llm": _validate_app_llm(
                ncfg.get("llm"), f"app 配置 {path} nodes.{node_code}.llm"),
            "loop": _validate_app_loop(
                ncfg.get("loop"), _APP_NODE_LOOP_FIELDS,
                f"app 配置 {path} nodes.{node_code}.loop"),
        }

    skills = raw.get("skills") or {}
    if not isinstance(skills, dict):
        raise ValueError(f"app 配置 {path} skills 应为字典")
    unknown = set(skills.keys()) - {"dir"}
    if unknown:
        logging.getLogger(__name__).warning(
            "app 配置 %s skills 含未知键 %s，已忽略", path, sorted(unknown))
    skills_view = {"dir": str(skills.get("dir") or "")} if skills else {}

    config = raw.get("config")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError(f"app 配置 {path} config 应为字典")

    return {
        "pattern": pattern_code,
        "llm": _validate_app_llm(raw.get("llm"), f"app 配置 {path} llm"),
        "nodes": nodes_view,
        "loop": _validate_app_loop(
            raw.get("loop"), _APP_LOOP_FIELDS, f"app 配置 {path} loop"),
        "compression": _validate_app_compression(
            raw.get("compression"), f"app 配置 {path} compression"),
        "guardrails": _validate_app_guardrails(
            raw.get("guardrails"), f"app 配置 {path} guardrails"),
        "skills": skills_view,
        "config": config,
    }


# resolved apps dir -> (fingerprint, {pattern_code: normalized view}); same
# lock and policy as _CONFIG_CACHE (parse outside the lock, only the dict
# swap is atomic; parse failures do not populate the cache)
_APP_CONFIGS_CACHE: Dict[str, Tuple[Tuple[Tuple[str, int, int], ...],
                                    Dict[str, Dict[str, Any]]]] = {}

# Tests / nested deployments can repoint the anchor via env var (default
# anchors with discovery: the repo-root apps/ of
# nexus/registry/patterns.py)
_APPS_DIR_ENV = "NEXUS_APPS_DIR"


def _get_apps_dir() -> Path:
    """App config scan root: ``$NEXUS_APPS_DIR`` > repo-root apps/ (same
    anchoring as discover_builtin_patterns; settings.py sits one directory
    shallower so it is parents[1])."""
    env = os.environ.get(_APPS_DIR_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "apps"


def _load_app_configs() -> Dict[str, Dict[str, Any]]:
    """Scan ``apps/*/config.yaml`` → ``{pattern_code: normalized view}``.

    Whole-table (mtime_ns, size) fingerprint cache (≤ 8 files, a whole-table
    fingerprint is enough; the file set itself is encoded into the
    fingerprint — deleting / swapping a file necessarily invalidates it).
    No files = empty dict (unconfigured apps run unchanged on defaults).
    Two files bound to the same pattern code: fail fast. Re-read every turn,
    matching the global yaml's hot-reload semantics.

    Raises:
        ValueError: any file structurally illegal / missing pattern /
            containing connection fields / duplicate binding.
    """
    apps_dir = _get_apps_dir()
    files = (sorted(apps_dir.glob("*/config.yaml"))
             if apps_dir.is_dir() else [])
    resolved = str(apps_dir.resolve())
    fingerprint = tuple(
        (str(p.relative_to(apps_dir)), st.st_mtime_ns, st.st_size)
        for p in files
        for st in (p.stat(),)
    )

    with _CONFIG_CACHE_LOCK:
        cached = _APP_CONFIGS_CACHE.get(resolved)
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])

    configs: Dict[str, Dict[str, Any]] = {}
    source: Dict[str, Path] = {}
    for path in files:
        view = _parse_app_config_file(path)
        code = view["pattern"]
        if code in configs:
            raise ValueError(
                f"pattern '{code}' 被多份 app 配置绑定: "
                f"{source[code]} 与 {path}（一个 pattern 一份配置）")
        configs[code] = view
        source[code] = path

    with _CONFIG_CACHE_LOCK:
        _APP_CONFIGS_CACHE[resolved] = (fingerprint, copy.deepcopy(configs))
    return configs


def _get_app_config(pattern_code: str) -> Optional[Dict[str, Any]]:
    """App config view by pattern code; empty code / unbound → None (fall
    back to global)."""
    if not pattern_code:
        return None
    return _load_app_configs().get(pattern_code)


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
        ``loop`` / ``session_db_path`` keys. A legacy ``llm:`` node is
        converted automatically at load time into ``llm_providers`` +
        ``llm_default``. **Returns a deep copy**: caller-side rewrites
        cannot pollute the cache.

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
    """Drop the config cache (all of it, or one resolved path's entry) —
    plus the whole app-configs table (``apps/*/config.yaml`` fingerprints),
    so ``/api/v1/system/reload`` and ``host.reload.reload_all`` cover both
    layers with their existing call sites.

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
        _APP_CONFIGS_CACHE.clear()


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

    # The deprecated per-pattern model pick (pattern_llm, pre-app-config era):
    # silently dropped after the migration — an existing deployment upgrading
    # with real content would quietly fall back to llm_default; this warns
    # loudly, turning the silent fallback into an actionable migration signal
    if raw.get("pattern_llm"):
        logging.getLogger(__name__).warning(
            "配置文件 %s 的顶层 'pattern_llm' 已废弃且不再生效(模型选择请"
            "迁移到 apps/<name>/config.yaml 的 llm/nodes 段)，当前按 "
            "llm_default 回退处理", path)

    # Extract LLM config: new structure (llm_providers/llm_default) or legacy llm node
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

    loop_cfg = raw.get("loop") or {}
    if not isinstance(loop_cfg, dict):
        raise ValueError("loop 应为字典")
    _loop_unknown = set(loop_cfg.keys()) - {"max_tool_rounds"}
    if _loop_unknown:
        logging.getLogger(__name__).warning(
            "loop 段含未知键 %s，已忽略", sorted(_loop_unknown))

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

    skills_cfg = raw.get("skills") or {}
    if not isinstance(skills_cfg, dict):
        raise ValueError("skills 应为字典")
    # Unknown judge-override fields only warn and get dropped — a typo'd key
    # (e.g. modle) silently flowing into build_provider would have its
    # failure swallowed by the review layer's DEBUG logging: the judge never
    # runs and nobody knows
    _tg_llm_unknown = set(tool_guard_llm.keys()) - _LLM_ALL_FIELDS
    if _tg_llm_unknown:
        logging.getLogger(__name__).warning(
            "tool_guard.llm 包含未知字段将被忽略: %s", sorted(_tg_llm_unknown)
        )
        tool_guard_llm = {k: v for k, v in tool_guard_llm.items()
                          if k in _LLM_ALL_FIELDS}

    return {
        "llm_providers": llm_providers,
        "llm_default": llm_default,
        # MCP servers (optional; empty dict = the whole MCP chain is a no-op).
        # Validated + normalized by _validate_mcp_servers (fail-fast on
        # transport/required-field errors)
        "mcp_servers": _validate_mcp_servers(raw.get("mcp_servers")),
        # ReAct loop budgets (optional; max_tool_rounds default 10, illegal
        # values warn + fall back — app configs refine per pattern/node)
        "loop": {
            "max_tool_rounds": _positive_int(
                loop_cfg.get("max_tool_rounds"),
                DEFAULT_LOOP_MAX_TOOL_ROUNDS, "loop.max_tool_rounds"),
        },
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
        # Skill-asset scan root (optional; default skills dir, consumed by
        # nexus/skills.py)
        "skills": {
            "dir": str(skills_cfg.get("dir", DEFAULT_SKILLS_DIR) or ""),
        },
        # Tool guard (optional; defaults see DEFAULT_TOOL_GUARD_* constants).
        # Boolean fields go through _strict_bool — a hand-written yaml
        # "false" string is truthy under bool(...) and would silently turn a
        # guard / review the operator meant to disable back on
        "tool_guard": {
            "enabled": _strict_bool(tool_guard.get("enabled"),
                                    DEFAULT_TOOL_GUARD_ENABLED,
                                    "tool_guard.enabled"),
            "llm_fallback": _strict_bool(tool_guard.get("llm_fallback"),
                                         DEFAULT_TOOL_GUARD_LLM_FALLBACK,
                                         "tool_guard.llm_fallback"),
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


def _strict_bool(value: Any, default: bool, field: str) -> bool:
    """Boolean config coercion: a real bool passes through; common
    "true"/"false"/"yes"/"no"/"on"/"off"/1/0 strings and ints convert by
    semantics; anything else warns and falls back to the default —
    bool("false") is truthy and a bare cast would silently turn a switch
    the operator meant to disable back on."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    if value is not None:
        import logging
        logging.getLogger(__name__).warning(
            "%s 应为布尔，收到 %r，按默认 %r 处理", field, value, default)
    return default


def _merge_connection(orch: Dict[str, Any], providers: Dict[str, Any]) -> Dict[str, Any]:
    """Orchestration result ⊕ the provider connection section for its code
    (empty section when absent, falling back to registry defaults)."""
    return {**providers.get(orch.get("code", ""), {}), **orch}


def _resolve_layered(cfg: Dict[str, Any], pattern_code: str,
                     node_code: str) -> Dict[str, Any]:
    """llm_default ⊕ app.llm ⊕ app.nodes[node_code].llm shallow-merged layer
    by layer (deeper wins, absent fields inherit the shallower layer).

    Unconfigured / unknown codes silently fall back to the shallower layer:
    a pattern without an app config is the normal case (debug), a configured
    pattern missing one node still warns.
    """
    merged = dict(cfg["llm_default"])
    app = _load_app_configs().get(pattern_code) if pattern_code else None
    if pattern_code and app is None:
        logging.getLogger(__name__).debug(
            "pattern '%s' 未绑定 app 配置，回退全局默认", pattern_code)
    if app:
        merged.update(app["llm"])
        if node_code:
            node = app["nodes"].get(node_code)
            if node is None:
                logging.getLogger(__name__).warning(
                    "pattern '%s' 未配置 node '%s'，回退更浅层",
                    pattern_code, node_code)
            else:
                merged.update(node["llm"])
    return merged


def get_llm_config(pattern_code: str = "", node_code: str = "",
                   override: Optional[Dict[str, Any]] = None,
                   config_path: str = "") -> Dict[str, Any]:
    """Resolve the LLM config for the current position (layered merge).

    override not None (``cxt.metadata["llm_override"]`` — the caller's /
    tests' explicit pick; it sits ABOVE the whole layered chain): skip the
    layered resolution and only try to merge in the
    llm_providers[override.code] connection section; a yaml load failure
    silently degrades to an empty connection section (keeps offline tests
    sealed). Otherwise: merge the layers (llm_default ⊕ app.llm ⊕
    app.nodes[node_code].llm, per-app config in apps/<name>/config.yaml),
    then merge in the connection layer; a yaml load failure raises as usual.
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
            # When the caller picks "keep the config as-is", override carries
            # only code; a missing model would make the loop executor raise
            # KeyError on llm_config["model"], so backfill it just like code
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


def get_loop_limits(pattern_code: str = "", node_code: str = "") -> Dict[str, Any]:
    """ReAct tool-round budget, three-layer resolution: global ``loop``
    section → app pattern level → app node level (deeper wins; an unbound
    pattern_code is just the global value).

    Returns:
        ``{"max_tool_rounds": N}`` — a dict shape so future budget
        dimensions can be added.
    """
    cfg = load_config()
    rounds = cfg["loop"]["max_tool_rounds"]
    app = _get_app_config(pattern_code)
    if app:
        pattern_level = app["loop"].get("max_tool_rounds")
        if pattern_level is not None:
            rounds = pattern_level
        if node_code:
            node = app["nodes"].get(node_code)
            node_level = (node or {}).get("loop", {}).get("max_tool_rounds")
            if node_level is not None:
                rounds = node_level
    return {"max_tool_rounds": rounds}


def resolve_max_steps(pattern) -> int:
    """Graph-level step budget: app ``loop.max_steps`` wins, falling back to
    ``pattern.max_steps`` (the code declaration is the default, the app yaml
    is the deployer's override — neither owes the other).

    ``pattern`` is a ``nexus.model.pattern.Pattern`` object (duck-typed for
    ``code`` / ``max_steps``; the type is deliberately not imported here to
    keep the registry import chain free of cycles).
    """
    app = _get_app_config(getattr(pattern, "code", "") or "")
    if app is not None and app["loop"].get("max_steps") is not None:
        return app["loop"]["max_steps"]
    return pattern.max_steps


def resolve_max_fanout(pattern) -> int:
    """Runtime fan-out width cap: app ``loop.max_fanout`` wins, falling back
    to ``pattern.max_fanout`` (same semantics as resolve_max_steps)."""
    app = _get_app_config(getattr(pattern, "code", "") or "")
    if app is not None and app["loop"].get("max_fanout") is not None:
        return app["loop"]["max_fanout"]
    return pattern.max_fanout


def get_session_compress_config(pattern_code: str = "",
                                config_path: str = "") -> tuple:
    """Convenience method: return (compression token threshold, retain count); threshold 0 = off.

    The app ``compression`` fields override the global ``session_compress_*``
    field by field (write only the fields to change in the app file; the rest
    inherit global).
    """
    cfg = load_config(config_path)
    threshold = cfg["session_compress_token_threshold"]
    retain_count = cfg["session_compress_retain_count"]
    app = _get_app_config(pattern_code)
    if app:
        comp = app["compression"]
        threshold = comp.get("threshold", threshold)
        retain_count = comp.get("retain_count", retain_count)
    return (threshold, retain_count)


def _guardrail_section(section: str, pattern_code: str,
                       config_path: str) -> Dict[str, Any]:
    """Global guardrail section ⊕ the app guardrails section of the same name
    (field-level merge, app fields win; direction-free — an app config is the
    deployer's will, the security boundary lives in tool authorization and
    the args-only-lower invariant, not here)."""
    merged = dict(load_config(config_path)[section])
    app = _get_app_config(pattern_code)
    if app:
        merged.update(app["guardrails"].get(section) or {})
    return merged


def get_subagent_tool_config(pattern_code: str = "",
                             config_path: str = "") -> Dict[str, Any]:
    """Return the ``delegate_task`` sub-agent guardrails.

    Global ``subagent_tool`` section ⊕ the app ``guardrails.subagent_tool``
    overlay (field-level, app wins) — ``{"timeout_seconds", "max_rounds",
    "max_result_chars"}``, all optional in the config file (defaults see the
    DEFAULT_SUBAGENT_* constants).
    """
    return _guardrail_section("subagent_tool", pattern_code, config_path)


def get_workflow_tool_config(pattern_code: str = "",
                             config_path: str = "") -> Dict[str, Any]:
    """Return the ``run_workflow`` topology guardrails.

    Global section ⊕ the app ``guardrails.workflow_tool`` overlay (field-level,
    app wins) — ``{"timeout_seconds", "max_rounds", "max_width", "max_cycles",
    "max_iterations", "max_result_chars", "trace_cap"}``, all optional in
    the config file (defaults see the DEFAULT_WORKFLOW_* constants).
    """
    return _guardrail_section("workflow_tool", pattern_code, config_path)


def get_shell_tool_config(pattern_code: str = "",
                          config_path: str = "") -> Dict[str, Any]:
    """Return the ``bash`` / ``run_python`` guardrails.

    Global section ⊕ the app ``guardrails.shell_tool`` overlay (field-level,
    app wins) — ``{"timeout_seconds", "max_output_chars"}``, all optional in
    the config file (defaults see the DEFAULT_SHELL_* constants).
    """
    return _guardrail_section("shell_tool", pattern_code, config_path)


def get_file_tool_config(pattern_code: str = "",
                         config_path: str = "") -> Dict[str, Any]:
    """Return the file tools guardrails.

    Global section ⊕ the app ``guardrails.file_tool`` overlay (field-level,
    app wins) — ``{"max_read_chars", "max_write_chars", "max_list_entries",
    "max_matches", "max_edit_chars", "max_find_results"}``, all optional in
    the config file (defaults see the DEFAULT_FILE_* constants).
    """
    return _guardrail_section("file_tool", pattern_code, config_path)


def get_tasks_tool_config(pattern_code: str = "",
                          config_path: str = "") -> Dict[str, Any]:
    """Return the session task-list guardrails.

    Global section ⊕ the app ``guardrails.tasks_tool`` overlay (field-level,
    app wins) — ``{"max_tasks", "max_task_chars"}``, all optional in the
    config file (defaults see the DEFAULT_TASKS_* constants).
    """
    return _guardrail_section("tasks_tool", pattern_code, config_path)


def get_cron_tool_config(pattern_code: str = "",
                         config_path: str = "") -> Dict[str, Any]:
    """Return the cron scheduler guardrails.

    Global section ⊕ the app ``guardrails.cron_tool`` overlay (field-level,
    app wins) — ``{"max_jobs", "fire_timeout_seconds", "max_rounds",
    "history_cap", "max_input_chars", "max_result_chars"}``, all optional in
    the config file (defaults see DEFAULT_CRON_* constants).
    ``jobs_path`` / ``tick_seconds`` are process-level infrastructure (jobs
    repo path / scheduler tick) and are deliberately NOT app-overridable —
    app configs writing them get warn+dropped; configure them in the global
    ``cron_tool`` section only (see _GUARDRAIL_FIELD_COERCERS).
    """
    return _guardrail_section("cron_tool", pattern_code, config_path)


def get_skills_config(config_path: str = "") -> Dict[str, Any]:
    """Return the skill-asset scan config (``{"dir": ...}``).

    Equivalent to ``load_config(config_path)["skills"]`` — the scan root
    consumed by nexus/skills.py (pattern ``config.skills_dir`` overrides it).
    """
    return load_config(config_path)["skills"]


def get_pattern_custom_config(pattern_code: str = "") -> Dict[str, Any]:
    """The app ``config`` free bag (home of executor custom params, e.g.
    archify's author_rounds / workspace_root); unbound app → empty dict.

    Deliberately NOT merged with ``pattern.config``: code-level defaults stay
    in the executor (``.get`` fallback), the yaml value wins here — a single
    override source, never a key half-written in both layers.
    """
    app = _get_app_config(pattern_code)
    return dict(app["config"]) if app else {}


def get_tool_guard_config(config_path: str = "") -> Dict[str, Any]:
    """Return the tool guard config (P4 dangerous-op announce).

    Equivalent to ``load_config(config_path)["tool_guard"]`` —
    ``{"enabled", "llm_fallback", "llm_max_input_chars", "llm_max_queue",
    "llm_timeout_seconds", "llm"}``, all optional in the config file
    (defaults see DEFAULT_TOOL_GUARD_* constants). ``llm`` is a passthrough
    dict of judge-model overrides merged over the ambient connection.
    """
    return load_config(config_path)["tool_guard"]
