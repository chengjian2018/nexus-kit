"""The four-station executors of the browser_agent pattern — one class per
node (plugin code = node code; route.py binds via plugins={"loop": ...}).

    ba_plan ──> ba_run ──┬──成功──────────────────────> ba_report (is_end)
     plan LLM call  deterministic  │        ↑
                    single-attempt │        ├── selector/capability → ba_repair
                    orchestrator   │        ├── dep/setup missing → next engine
                    subprocess +   │        ├── network/timeout → self retry
                    receipt        │        └── engines exhausted → ba_report
                    routing        └── login/captcha → wait_human (suspend AT
                                       this node; resume retries the same
                                       engine once, then switches — the source
                                       skill's 恢复同一引擎一次 rule)

Station inventory:

    PLAN    one tool-less LLM call → plan JSON (url + action chain +
            optional platform/engine_pref hints; fault-tolerant extraction +
            one self-correct retry). Deterministic post-pass: plan structure
            validation (action whitelist + per-type required fields), URL
            domain → platform detection, platform → engine order resolution
            (the embedded orchestrator's PLATFORM_PRIORITY table — 小红书
            browser-act first, AI platforms cloak>playwright, else default).
            Unparseable output degrades to a minimal capture plan built from
            any URL found in the request (marked degraded); no URL at all →
            honest bail to REPORT.
    RUN     deterministic, no LLM: ONE engine attempt per visit. The current
            plan is written to <workspace>/plan.json, then the embedded
            orchestrator CLI runs as a subprocess via the bash tool
            (--engine-order <one engine> --max-attempts-per-engine 1 — the
            graph itself is the fallback loop, finer-grained than the source
            CLI script: a broken selector goes to repair instead of burning
            every engine's attempts on it). The EngineResult receipt (last
            JSON line of stdout) lands on the attempts trail and a
            deterministic routing table decides the next move — never the
            model. Login/captcha suspends the graph with visible-browser
            takeover guidance as the turn reply.
    REPAIR  one tool-less LLM call → revised plan JSON, carrying the FULL
            attempt trail + repair_log (anti-replay: round 3 must not
            re-propose round 1's failed selector). Deterministic validation;
            an unusable repair keeps the old plan and advances the engine
            cursor (repair is not allowed to wedge the loop).
    REPORT  deterministic, no LLM: assembled from receipts — success states
            the winning engine, outputs summary and ABSOLUTE artifact paths;
            failure lists every attempt (engine/kind/error) with install
            hints, never dressed up. Records platform experience (source
            skill's 经验沉淀协议) on fallback-success / repair-success /
            all-engines-failed outcomes into
            data/browser_agent/experience/<platform>.md (cross-session,
            deduped, append-only).

Inter-station state travels via ``cxt.graph_state["browser_agent_state"]``
(cleared by the runtime at graph termination); the final trace goes to
``cxt.metadata["browser_agent"]``.

Runaway protection (three independent dimensions): graph steps
(route.py config.max_steps=20) × repair_rounds cap (default 2, app config
bag) × per-engine attempt cap (default 2, app config bag); human takeovers
are capped too (default 2). The engine cursor (engine_idx / attempt_no) is
never reset by repair; a repaired plan retries the SAME engine with
attempt_no back to 1 (a new plan deserves fresh attempts, bounded globally
by repair_rounds).
"""

import json
import logging
import re
import shlex
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from atoms.executors.loop_executor import _emit_round, _stream_round

from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.loop import TurnResult, _execute_tool
from nexus.engine.messages import build_agent_messages
from nexus.engine.tool_context import tool_call_context
from nexus.llm.resolve import build_provider
from nexus.settings import get_pattern_custom_config

from apps.browser_agent.orchestrator import (
    DEFAULT_ORDER,
    ENGINE_REGISTRY,
    PLATFORM_PRIORITY,
    classify_error,
)
from apps.browser_agent.prompts import (
    PLAN_PHASE_PROMPT,
    PLAN_RETRY_PROMPT,
    REPAIR_PHASE_TMPL,
    REPAIR_RETRY_PROMPT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Station codes (= plugin codes; route.py binds via plugins={"loop": ...})
# ---------------------------------------------------------------------------

BA_PLAN_CODE = "ba_plan"
BA_RUN_CODE = "ba_run"
BA_REPAIR_CODE = "ba_repair"
BA_REPORT_CODE = "ba_report"

_STATE_KEY = "browser_agent_state"

_ORCHESTRATOR_CLI = Path(__file__).resolve().parent / "orchestrator.py"

# Failure-kind routing table (deterministic; kinds are the embedded
# orchestrator's stable labels — see references/engine-contract.md).
_HUMAN_KINDS = {"login_required", "captcha_required"}      # wait_human takeover
_REPAIR_KINDS = {"selector_drift", "engine_capability_gap", "unknown"}
_SKIP_KINDS = {"dependency_missing", "setup_required"}     # next engine, no retry
# network_or_timeout (everything else): same-engine retry up to the cap.

# Code defaults — the app config bag (apps/browser_agent/config.yaml
# `config:` section, via get_pattern_custom_config) overrides by the same
# keys; no bound app config → empty bag → these defaults (offline tests are
# exactly this shape).
_DEFAULT_WORKSPACE_ROOT = "data/browser_agent"
_DEFAULT_MAX_ATTEMPTS = 2     # attempts per engine (source skill default)
_DEFAULT_REPAIR_ROUNDS = 2    # global repair budget per run
_DEFAULT_PLAN_RETRIES = 1     # plan JSON parse-failure self-correction retries
_DEFAULT_TAKEOVER_CAP = 2     # wait_human takeovers per run

_FORCE_CLOSE_REPLY = (
    "(浏览器自动化流程被步数预算截断，未能完成；已产出的证据与部分结果"
    "见运行目录，未完成的尝试如实标注。)"
)

# URL domain → platform (deterministic; the source skill's platform table).
_PLATFORM_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "xhs": ("xiaohongshu.com", "xhslink.com", "xhs.link"),
    "bilibili": ("bilibili.com", "b23.tv", "biliapi.net"),
    "douyin": ("douyin.com", "iesdouyin.com"),
    "weibo": ("weibo.com", "weibo.cn"),
    "ai": ("gemini.google.com", "aistudio.google.com", "doubao.com",
           "chatgpt.com", "chat.openai.com"),
}

# LLM platform hint aliases → canonical platform key.
_PLATFORM_ALIASES: Dict[str, str] = {
    "xhs": "xhs", "xiaohongshu": "xhs", "小红书": "xhs",
    "bilibili": "bilibili", "b站": "bilibili", "bili": "bilibili",
    "douyin": "douyin", "dy": "douyin", "抖音": "douyin",
    "weibo": "weibo", "微博": "weibo",
    "ai": "ai", "ai_platform": "ai", "gemini": "ai", "doubao": "ai",
    "豆包": "ai", "chatgpt": "ai", "gpt": "ai",
}

# Action-chain contract (source skill's plan JSON schema).
_ACTION_TYPES = {
    "goto", "wait", "click", "fill", "press", "scroll",
    "evaluate", "extract_text", "screenshot",
}
_ACTION_REQUIRED = {
    "goto": ("url",),
    "click": ("selector",),
    "fill": ("selector",),
    "press": ("key",),
    "evaluate": ("script",),
}


# ---------------------------------------------------------------------------
# Runtime settings / state board
# ---------------------------------------------------------------------------

def _runtime_settings() -> Dict[str, Any]:
    """Single read point for budgets / deployment paths (app config bag over
    the code defaults)."""
    bag = get_pattern_custom_config("browser_agent")

    def limit(key: str, default: int) -> int:
        raw = bag.get(key)
        try:
            value = int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default
        return value if value >= 1 else default

    return {
        "workspace_root": str(bag.get("workspace_root") or _DEFAULT_WORKSPACE_ROOT),
        "max_attempts_per_engine": limit("max_attempts_per_engine", _DEFAULT_MAX_ATTEMPTS),
        "repair_rounds": limit("repair_rounds", _DEFAULT_REPAIR_ROUNDS),
        "plan_retries": limit("plan_retries", _DEFAULT_PLAN_RETRIES),
        "max_human_takeovers": limit("max_human_takeovers", _DEFAULT_TAKEOVER_CAP),
        "install_missing": bool(bag.get("install_missing", False)),
    }


def _absolutize(raw: str) -> Path:
    """Pin a declared root to an absolute path (relative roots resolve against
    the service startup directory). Paths on the state board must be absolute:
    file tools and bash workdirs resolve relative strings differently."""
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def _safe_session(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(session_id)) or "s"


def _workspace_root() -> Path:
    return _absolutize(_runtime_settings()["workspace_root"])


def _new_state(cxt, request: str) -> Dict[str, Any]:
    """Fresh run state (ba_plan initializes; per-session workspace)."""
    workspace = _workspace_root() / _safe_session(cxt.session_id)
    return {
        "request": request,
        "workspace": str(workspace),
        "plan": {},                 # current action chain (dict)
        "plan_path": str(workspace / "plan.json"),
        "platform": "",
        "engine_order": [],
        "engine_idx": 0,            # cursor: which engine is being tried
        "attempt_no": 1,            # cursor: attempt within the engine
        "attempts": [],             # full receipt trail (the loop's memory)
        "repair_log": [],           # anti-replay: applied fixes so far
        "repair_rounds": 0,
        "human_takeovers": 0,
        "wait_reason": "",          # set while suspended for login/captcha
        "success": False,
        "selected_engine": "",
        "outputs": {},
        "artifacts": [],
        "done_reason": "",          # pass | engines_exhausted | login_wall_* |
                                    # plan_unparseable_no_url | state_lost
        "degraded": False,
    }


def _load_state(cxt) -> Optional[Dict[str, Any]]:
    state = (cxt.graph_state or {}).get(_STATE_KEY)
    return state if isinstance(state, dict) else None


def _save_state(cxt, state: Dict[str, Any]) -> None:
    cxt.graph_state[_STATE_KEY] = state


def _current_user_query(cxt) -> str:
    for m in reversed(cxt.history or []):
        if getattr(m, "role", "") == "user":
            return getattr(m, "content", "") or ""
    return ""


def _bail(cxt, state: Dict[str, Any], reason: str) -> TurnResult:
    """Honest early exit to the report station with a stated reason."""
    state["done_reason"] = reason
    _save_state(cxt, state)
    return TurnResult(content="", next=BA_REPORT_CODE)


# ---------------------------------------------------------------------------
# Plan helpers (validation / platform / engine order / minimal fallback)
# ---------------------------------------------------------------------------

def _validate_plan(plan: Any) -> Tuple[bool, str]:
    """Structural validation of an action chain (action whitelist + per-type
    required fields + reachable URL) — deterministic, never model-judged."""
    if not isinstance(plan, dict):
        return False, "plan 顶层不是 JSON 对象"
    url = plan.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://", url.strip()):
        return False, "url 缺失或不是 http(s) 链接"
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        return False, "actions 缺失或为空"
    for i, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            return False, f"actions[{i}] 不是对象"
        kind = action.get("type")
        if kind not in _ACTION_TYPES:
            return False, f"actions[{i}].type 非法: {kind!r}"
        for field in _ACTION_REQUIRED.get(kind, ()):  # type: ignore[arg-type]
            if not action.get(field):
                return False, f"actions[{i}] ({kind}) 缺少必填字段 {field}"
    return True, ""


def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only contract fields (unknown keys dropped; headless defaults to
    visible — the source skill's rule for external sites / login takeover)."""
    return {
        "url": str(plan.get("url", "")).strip(),
        "headless": bool(plan.get("headless", False)),
        "actions": plan.get("actions") or [],
        "profile_dir": plan.get("profile_dir") or "~/.cloakbrowser-profile",
        "viewport": plan.get("viewport") or {"width": 1366, "height": 768},
        "timeout": plan.get("timeout", 30000),
    }


def _detect_platform(url: str) -> str:
    """Deterministic domain → platform (empty when unknown)."""
    low = (url or "").lower()
    for platform, domains in _PLATFORM_DOMAINS.items():
        if any(domain in low for domain in domains):
            return platform
    return ""


def _resolve_engine_order(platform: str, engine_pref: str) -> List[str]:
    """Explicit engine_pref > platform table > default chain (the source
    skill's resolution precedence; the table lives in the embedded
    orchestrator so the CLI and the app share one source of truth)."""
    pref = (engine_pref or "").strip().lower()
    if pref in ENGINE_REGISTRY:
        return [pref]
    key = (platform or "").strip().lower()
    if key in PLATFORM_PRIORITY:
        return list(PLATFORM_PRIORITY[key])
    return list(DEFAULT_ORDER)


def _minimal_plan(request: str) -> Optional[Dict[str, Any]]:
    """Honest degraded fallback: any URL in the request + fixed capture
    chain. None when the request carries no URL at all."""
    match = re.search(r"https?://[^\s，。；'\"]+", request or "")
    if not match:
        return None
    return {
        "url": match.group(0).rstrip(".,);"),
        "headless": False,
        "actions": [
            {"type": "wait", "seconds": 1},
            {"type": "evaluate", "name": "title",
             "script": "document.title"},
            {"type": "extract_text", "name": "body", "selector": "body"},
            {"type": "screenshot", "path": "smoke.png"},
        ],
    }


def _write_plan_file(state: Dict[str, Any]) -> bool:
    """Placement belongs to code: the model returns the plan, the executor
    lands it (absolute path pinned on the state board)."""
    try:
        path = Path(state["plan_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state["plan"], ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("[browser_agent] plan 写盘失败: %s", exc)
        return False


def _extract_json_object(content: str) -> Tuple[Dict[str, Any], str]:
    """First balanced {...} block → parse (the archify JSON-protocol idiom)."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {}, "输出中找不到 JSON 对象（缺少 {...}）"
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, f"JSON 语法错误: {exc}"
    if not isinstance(data, dict):
        return {}, "JSON 顶层不是对象"
    return data, ""


async def _one_llm_call(ec, user_content: str) -> str:
    """One tool-less LLM call in the station's own workspace framing (station
    chatter streams as thinking only — it is never the user-visible reply)."""
    cxt = ec.cxt
    llm_config = cxt.llm_config or {}
    provider = build_provider(llm_config)
    messages = build_agent_messages(ec.node, cxt, pattern=ec.pattern)
    messages.append({"role": "user", "content": user_content})
    result = await _stream_round(
        provider, messages,
        llm_config.get("model", "default"),
        llm_config.get("temperature", 0.7),
        llm_config.get("max_tokens", 2048), ec.stream,
        forward_text=False)
    return (result.get("content", "") or "").strip()


# ---------------------------------------------------------------------------
# Orchestrator dispatch (bash tool → embedded CLI → EngineResult receipt)
# ---------------------------------------------------------------------------

async def _run_orchestrator(ec, state: Dict[str, Any], engine: str,
                            attempt: int) -> Dict[str, Any]:
    """Run ONE engine attempt as a subprocess of the embedded orchestrator
    CLI via the bash tool, and normalize the EngineResult receipt.

    Every failure mode (tool error, timeout kill, non-JSON stdout) degrades
    to an honest receipt — never a fabricated pass. The bash tool's shell
    guardrail bounds each attempt (app config loosens it to 300s).
    """
    seq = len(state["attempts"]) + 1
    run_dir = Path(state["workspace"]) / f"run_{seq:02d}_{engine}"
    plan_path = Path(state["plan_path"])
    command = " ".join([
        shlex.quote(sys.executable),
        shlex.quote(str(_ORCHESTRATOR_CLI)),
        "run",
        "--plan", shlex.quote(str(plan_path)),
        "--output-dir", shlex.quote(str(run_dir)),
        "--engine-order", shlex.quote(engine),
        "--max-attempts-per-engine", "1",
    ] + (["--install-missing"] if _runtime_settings()["install_missing"] else []))

    # Publish the station's position for guardrail overlays (same idiom as
    # archify's _station_tool_context — pattern_code keys the app overlay).
    with tool_call_context(
        (getattr(ec.cxt, "llm_config", None) or {}),
        getattr(ec.pattern, "allow_toolset", None) or [],
        session_id=getattr(ec.cxt, "session_id", "") or "",
        pattern_code=getattr(ec.pattern, "code", "") or "",
    ):
        raw = await _execute_tool(
            "bash", {"command": command, "workdir": str(Path(state["workspace"]))})

    def _receipt(**overrides: Any) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "ok": False, "engine": engine, "attempt": attempt,
            "url": state["plan"].get("url"), "current_url": None,
            "outputs": {}, "artifacts": [], "error": "", "failure_kind": "",
        }
        base.update(overrides)
        return base

    try:
        shell = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return _receipt(error=f"bash 工具返回无法解析: {str(raw)[:200]}",
                        failure_kind="unknown")
    if "error" in shell:
        return _receipt(error=f"bash 工具执行失败: {str(shell['error'])[:300]}",
                        failure_kind="unknown")
    if shell.get("timed_out"):
        return _receipt(
            error=f"引擎尝试超时被终止（shell 预算 {shell.get('elapsed_seconds')}s）",
            failure_kind="network_or_timeout")
    receipt = _last_receipt_line(str(shell.get("stdout") or ""))
    if receipt is not None:
        receipt.setdefault("engine", engine)
        receipt.setdefault("attempt", attempt)
        return receipt
    stderr = str(shell.get("stderr") or "")
    return _receipt(
        error=(f"退出码 {shell.get('exit_code')}，stdout 无回执行: "
               f"{str(shell.get('stdout') or '')[:200]} | stderr: {stderr[:200]}"),
        failure_kind=classify_error(stderr[:400]) or "unknown")


def _last_receipt_line(stdout: str) -> Optional[Dict[str, Any]]:
    """The orchestrator prints one JSON line per attempt; with
    --max-attempts-per-engine 1 the last parsable line IS the final
    EngineResult (resilient to non-JSON noise lines)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and "ok" in data:
            return data
    return None


def _compact_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Trail-sized copy (bounded error text — the board carries the whole
    history across loop rounds)."""
    return {
        "engine": receipt.get("engine"),
        "attempt": receipt.get("attempt"),
        "ok": bool(receipt.get("ok")),
        "failure_kind": receipt.get("failure_kind") or "",
        "error": str(receipt.get("error") or "")[:400],
        "current_url": receipt.get("current_url"),
        "artifacts": list(receipt.get("artifacts") or []),
    }


# ---------------------------------------------------------------------------
# Experience recording (source skill's 经验沉淀协议, deterministic)
# ---------------------------------------------------------------------------

def _record_lesson(platform: str, lesson: str) -> Optional[str]:
    """Append a deduped, dated lesson to the cross-session platform file
    (data/<app>/experience/<platform>.md). Returns the written path."""
    if not platform or not lesson.strip():
        return None
    root = _workspace_root() / "experience"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{platform}.md"
    bullet = f"- {date.today().isoformat()} / source: browser_agent / {lesson.strip()}"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        text = ""
    if bullet in text:
        return None
    try:
        with path.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write(bullet + "\n")
    except OSError as exc:
        logger.warning("[browser_agent] 经验写盘失败: %s", exc)
        return None
    return str(path)


def _record_experience(state: Dict[str, Any]) -> List[str]:
    """Deterministic lessons: engine-fallback hit, selector repair that
    worked, all-engines-failed signal (the source skill's record triggers,
    minus anything needing semantic judgment)."""
    recorded: List[str] = []
    platform = state.get("platform") or "general"
    order = state.get("engine_order") or []
    if state.get("success"):
        if order and state.get("selected_engine") and \
                state["selected_engine"] != order[0]:
            trail = " → ".join(f"{a.get('engine')}({a.get('failure_kind')})"
                               for a in state["attempts"] if not a.get("ok"))
            recorded.append(
                f"engine fallback 命中: {trail} → {state['selected_engine']} 成功")
        if state.get("repair_log"):
            recorded.append(
                "selector 修复有效: " + "; ".join(state["repair_log"][-1:]))
    elif state.get("attempts"):
        kinds = sorted({a.get("failure_kind") or "?" for a in state["attempts"]})
        recorded.append(
            f"全部引擎失败: kinds={','.join(kinds)}; "
            f"计划动作数={len((state.get('plan') or {}).get('actions') or [])}")
    written = []
    for lesson in recorded:
        path = _record_lesson(platform, lesson)
        if path:
            written.append(path)
    return written


# ============================================================================
# The four station executors
# ============================================================================

class BaPlanExecutor(NodeExecutor):
    """ba_plan: task understanding → action chain (one LLM call, JSON
    protocol with self-correct retry, honest minimal-plan degradation) +
    deterministic platform/engine-order resolution."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _new_state(cxt, _current_user_query(cxt))
        _save_state(cxt, state)

        prompt = PLAN_PHASE_PROMPT
        plan_raw: Dict[str, Any] = {}
        for attempt in range(1 + _runtime_settings()["plan_retries"]):
            content = await _one_llm_call(ec, prompt)
            plan_raw, err = _extract_json_object(content)
            ok, verr = _validate_plan(plan_raw)
            if ok:
                break
            plan_raw = {}
            logger.warning("[browser_agent] PLAN 解析/校验失败(第 %d 次): %s%s",
                           attempt + 1, err, f"; {verr}" if verr else "")
            if attempt < _runtime_settings()["plan_retries"]:
                prompt = PLAN_RETRY_PROMPT.format(error=err or verr)

        if not plan_raw:
            plan_raw = _minimal_plan(state["request"]) or {}
            ok, _ = _validate_plan(plan_raw)
            if not ok:
                return _bail(cxt, state, "plan_unparseable_no_url")
            state["degraded"] = True

        # Platform: deterministic domain detection first, LLM hint second
        # (normalized), engine order resolved from the shared table.
        hint = str(plan_raw.get("platform") or "").strip()
        platform = _detect_platform(str(plan_raw.get("url"))) or \
            _PLATFORM_ALIASES.get(hint.lower(), "")
        engine_pref = str(plan_raw.get("engine_pref") or "")
        state["plan"] = _normalize_plan(plan_raw)
        state["platform"] = platform
        state["engine_order"] = _resolve_engine_order(platform, engine_pref)
        if not _write_plan_file(state):
            return _bail(cxt, state, "plan_write_failed")
        _emit_round(ec.stream, "plan", 0)
        _save_state(cxt, state)
        return TurnResult(content="", next=BA_RUN_CODE)


class BaRunExecutor(NodeExecutor):
    """ba_run: ONE deterministic engine attempt per visit + the routing table
    (failure_kind → next move, never model-decided). Login/captcha suspends
    the graph AT this node (wait_human); resume retries the same engine once,
    then switches — idempotent across re-execution via the wait_reason latch."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            return _bail(cxt, _new_state(cxt, ""), "state_lost")

        # wait_human resume: the user answered the takeover prompt — retry
        # the SAME engine once (cursor untouched; the takeover counter was
        # already consumed at suspension time).
        if state.get("wait_reason"):
            state["wait_reason"] = ""

        order = state.get("engine_order") or []
        engine_idx = int(state.get("engine_idx") or 0)
        if engine_idx >= len(order):
            return _bail(cxt, state, "engines_exhausted")
        engine = order[engine_idx]
        attempt = int(state.get("attempt_no") or 1)

        receipt = await _run_orchestrator(ec, state, engine, attempt)
        state["attempts"].append(_compact_receipt(receipt))
        _emit_round(ec.stream, f"run:{engine}", attempt - 1)

        def _advance_engine() -> str:
            state["engine_idx"] = engine_idx + 1
            state["attempt_no"] = 1
            return BA_RUN_CODE

        if receipt.get("ok"):
            state["success"] = True
            state["selected_engine"] = engine
            state["outputs"] = receipt.get("outputs") or {}
            state["artifacts"] = [str(a) for a in receipt.get("artifacts") or []]
            state["done_reason"] = "pass"
            _save_state(cxt, state)
            return TurnResult(content="", next=BA_REPORT_CODE)

        kind = str(receipt.get("failure_kind") or "unknown")
        settings = _runtime_settings()

        if kind in _HUMAN_KINDS:
            if int(state.get("human_takeovers") or 0) < settings["max_human_takeovers"]:
                state["human_takeovers"] = int(state.get("human_takeovers") or 0) + 1
                state["wait_reason"] = kind
                _save_state(cxt, state)
                return TurnResult(
                    content=(
                        f"遇到{'登录墙' if kind == 'login_required' else '验证码'}"
                        f"（引擎 {engine}）。我已把浏览器保持为可见模式——请在弹出"
                        "的浏览器窗口里完成登录/验证，然后回复我（任意内容）继续；"
                        "如果暂时无法完成，也可以回复让我换一种引擎尝试。"
                    ),
                    wait_human=True,
                    extra={"wait_message": f"{kind} 人工接管（{engine}）"},
                )
            state["done_reason"] = "login_wall_after_takeovers"
            nxt = _advance_engine()
            _save_state(cxt, state)
            if state["engine_idx"] >= len(order):
                state["done_reason"] = "login_wall_no_engine"
                _save_state(cxt, state)
                return TurnResult(content="", next=BA_REPORT_CODE)
            return TurnResult(content="", next=nxt)

        if kind in _SKIP_KINDS:
            nxt = _advance_engine()
            _save_state(cxt, state)
            if state["engine_idx"] >= len(order):
                state["done_reason"] = "dependency_gap_all_engines" \
                    if kind == "dependency_missing" else "setup_gap_all_engines"
                _save_state(cxt, state)
                return TurnResult(content="", next=BA_REPORT_CODE)
            return TurnResult(content="", next=nxt)

        if kind in _REPAIR_KINDS:
            if int(state.get("repair_rounds") or 0) < settings["repair_rounds"]:
                _save_state(cxt, state)
                return TurnResult(content="", next=BA_REPAIR_CODE)
            nxt = _advance_engine()
            _save_state(cxt, state)
            if state["engine_idx"] >= len(order):
                state["done_reason"] = "engines_exhausted"
                _save_state(cxt, state)
                return TurnResult(content="", next=BA_REPORT_CODE)
            return TurnResult(content="", next=nxt)

        # network_or_timeout (and any residual label): same-engine retry
        if attempt < settings["max_attempts_per_engine"]:
            state["attempt_no"] = attempt + 1
            _save_state(cxt, state)
            return TurnResult(content="", next=BA_RUN_CODE)  # self-edge retry
        nxt = _advance_engine()
        _save_state(cxt, state)
        if state["engine_idx"] >= len(order):
            state["done_reason"] = "engines_exhausted"
            _save_state(cxt, state)
            return TurnResult(content="", next=BA_REPORT_CODE)
        return TurnResult(content="", next=nxt)


class BaRepairExecutor(NodeExecutor):
    """ba_repair: THE experience-inheritance station. The prompt carries the
    request, the current plan, the failing receipt and the FULL repair_log —
    without them round 3 happily replays round 1's failed selector. An
    unusable repair keeps the old plan and advances the engine cursor (repair
    may not wedge the loop)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            return _bail(cxt, _new_state(cxt, ""), "state_lost")

        failed = next((a for a in reversed(state["attempts"]) if not a.get("ok")), {})
        repair_log = "\n".join(
            f"- {entry}" for entry in state["repair_log"]) or "(尚无修复记录)"
        prompt = REPAIR_PHASE_TMPL.format(
            request=state.get("request", ""),
            plan_json=json.dumps(state["plan"], ensure_ascii=False, indent=2),
            engine=failed.get("engine") or "?",
            attempt=failed.get("attempt") or "?",
            failure_kind=failed.get("failure_kind") or "unknown",
            error=(failed.get("error") or "(无错误文本)")[:400],
            current_url=failed.get("current_url") or "(未知)",
            artifacts=", ".join(failed.get("artifacts") or []) or "(无)",
            repair_log=repair_log,
        )

        revised: Dict[str, Any] = {}
        for attempt in range(2):  # initial + one self-correct retry
            content = await _one_llm_call(ec, prompt)
            revised, err = _extract_json_object(content)
            ok, verr = _validate_plan(revised)
            if ok:
                break
            revised = {}
            logger.warning("[browser_agent] REPAIR 解析/校验失败(第 %d 次): %s%s",
                           attempt + 1, err, f"; {verr}" if verr else "")
            if attempt == 0:
                prompt = REPAIR_RETRY_PROMPT.format(error=err or verr)

        state["repair_rounds"] = int(state.get("repair_rounds") or 0) + 1
        if revised:
            state["plan"] = _normalize_plan(revised)
            state["attempt_no"] = 1  # a new plan earns fresh attempts
            if not _write_plan_file(state):
                return _bail(cxt, state, "plan_write_failed")
            n_actions = len(state["plan"]["actions"])
            notes = str(revised.get("notes") or "").strip()
            state["repair_log"].append(
                f"第{state['repair_rounds']}轮（{failed.get('engine')}/"
                f"{failed.get('failure_kind')}）: 修订计划共 {n_actions} 步"
                + (f"——{notes[:120]}" if notes else ""))
        else:
            # Unusable repair: keep the old plan, advance the engine cursor
            # (different engine, different fingerprint — the plan is not the
            # only suspect), never wedge the loop on a broken station.
            state["repair_log"].append(
                f"第{state['repair_rounds']}轮（{failed.get('engine')}/"
                f"{failed.get('failure_kind')}）: 修复输出不可用，保留旧计划并换引擎")
            state["engine_idx"] = int(state.get("engine_idx") or 0) + 1
            state["attempt_no"] = 1
        _emit_round(ec.stream, "repair", state["repair_rounds"] - 1)
        _save_state(cxt, state)
        return TurnResult(content="", next=BA_RUN_CODE)


class BaReportExecutor(NodeExecutor):
    """ba_report: deterministic assembly (no LLM) — success states the
    winning engine, outputs and ABSOLUTE artifact paths; failure lists every
    attempt honestly with install hints. Records platform experience. The
    only station whose content becomes the user-visible reply."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt) or _new_state(cxt, "")

        written = _record_experience(state)
        order = state.get("engine_order") or []
        lines: List[str] = []

        if state.get("success"):
            lines.append(f"✅ 完成：引擎 {state['selected_engine']}（"
                         f"{len(state['attempts'])} 次尝试，修复 "
                         f"{state['repair_rounds']} 轮"
                         + ("，计划为降级最小计划" if state.get("degraded") else "")
                         + "）。")
            outputs = state.get("outputs") or {}
            if outputs:
                keys = ", ".join(list(outputs)[:10])
                lines.append(f"抓取输出字段：{keys}")
                for name, value in list(outputs.items())[:3]:
                    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                    lines.append(f"· {name}: {text[:200]}")
            artifacts = state.get("artifacts") or []
            if artifacts:
                lines.append("产物文件（绝对路径）：")
                lines.extend(f"· {a}" for a in artifacts[:8])
        else:
            reason = state.get("done_reason") or "interrupted"
            lines.append(f"❌ 未能完成（{reason}）。尝试轨迹：")
            for a in state.get("attempts") or []:
                err = str(a.get("error") or "")[:160]
                lines.append(f"· {a.get('engine')} 第{a.get('attempt')}次 "
                             f"[{a.get('failure_kind')}] {err}".rstrip())
            if any((a.get("failure_kind") == "dependency_missing")
                   for a in state.get("attempts") or []):
                lines.append(
                    "提示：缺少浏览器引擎依赖。可按需安装其一："
                    "`pip install cloakbrowser && python -m cloakbrowser install`"
                    "（反检测，默认首选）/ `uv tool install browser-act-cli "
                    "--python 3.12`（云端池，小红书优先）/ `pip install "
                    "playwright && python -m playwright install chromium`"
                    "（确定性兜底）；或在应用配置里开启 install_missing。")
            if not state.get("attempts"):
                lines.append("（没有任何引擎尝试被记录——计划阶段即告失败）")

        if written:
            lines.append(f"已沉淀平台经验 → {', '.join(dict.fromkeys(written))}")

        # Final trace for observability (the engine picks it into the
        # persisted trail as an app_trace event).
        cxt.metadata["browser_agent"] = {
            "success": state.get("success", False),
            "selected_engine": state.get("selected_engine"),
            "platform": state.get("platform"),
            "engine_order": order,
            "attempts": state.get("attempts", []),
            "repair_rounds": state.get("repair_rounds", 0),
            "done_reason": state.get("done_reason", ""),
            "artifacts": state.get("artifacts", []),
        }
        return TurnResult(content="\n".join(lines))


# ============================================================================
# Self-registration (plugin code = node code; route.py's bottom import
# triggers this module — the archify / toy-app convention)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", BA_PLAN_CODE, BaPlanExecutor)
plugin_registry.register("executor", BA_RUN_CODE, BaRunExecutor)
plugin_registry.register("executor", BA_REPAIR_CODE, BaRepairExecutor)
plugin_registry.register("executor", BA_REPORT_CODE, BaReportExecutor)
