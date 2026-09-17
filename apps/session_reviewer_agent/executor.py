"""The eight-station graph executors of the session_reviewer pattern — one
class per node (plugin code = node code; route.py binds via plugins={"loop": ...}):

    sr_route ──> sr_collect ──> sr_metrics ──> sr_review ──> sr_wait_human ──> sr_apply ──┬─> sr_report
     id 提取      只读取数+源码    规则指标       LLM 评审     wait_human 闸   备份+编辑+测试 │      (is_end)
                   │(逃生)          │(逃生)        │(拒绝/空建议)     └─> sr_fixloop ──┘
                   └──────────────> sr_report <─────────────────────────── 自修≤fix_rounds,耗尽回滚

Station inventory (semantic→model / deterministic→code / acceptance→receipts):

    ROUTE      deterministic, no LLM: session_id from task_info > keyword
               regex; missing/ambiguous → the turn ends with an ask (no
               route output = terminal); initializes the run state board
    COLLECT    deterministic, no LLM: read-only SQLite (sessions/messages/
               trace_events of the reviewed session), pattern_code → app dir
               (config.yaml binding > route.py declaration regex), target
               sources read; a bounded digest rides the state board, the
               fuller dump lands in workspace/input.json; DB missing / session
               absent → honest escape edge straight to REPORT
    METRICS    deterministic, no LLM: six computable signal families over the
               trimmed trail (turns / turn_error / tool calls+failures /
               clarify / node-revisit histogram / wait+resume); defensive on
               payload shapes — unknown shapes count 0 with an "approx" note
    REVIEW     one tool-less LLM call (quality-sensitive — deployments pin a
               stronger model via config.yaml nodes.sr_review): rule metrics
               + timeline digest + sources enter the prompt; 5-dim rubric
               JSON with one self-correct retry; unparseable → honest
               degraded escape to REPORT, never fabricated suggestions
    WAIT_HUMAN deterministic gate, no LLM: engine-native wait_human suspends
               AT this node with the suggestion listing as the turn reply;
               the next user message re-executes this node (resume_input) and
               a keyword parser decides approve (note kept) / reject
               (report-only) / unrecognized (brief re-ask, suspend again);
               config bag auto_approve=true passes the gate without asking;
               idempotent — the gate has no external side effects
    APPLY      backup first (per-file byte snapshot under workspace/
               backup_*/), then per-file LLM edit-pair drafting (old_string
               verbatim contract) with deterministic application (a miss is
               dropped, never fuzzy-landed), then the whitelisted pytest run
               (command assembled ONLY from a tests/ glob of the target app
               name — no free-form shell); nothing actionable / tests
               skipped → REPORT; failure → FIXLOOP
    FIXLOOP    experience-inheriting repair: pytest tail + fix_history
               (anti-replay — round N must not re-propose round 1's failed
               edits) enter the prompt; ≤ fix_rounds (default 2) rounds, the
               graph self-edge is the loop; exhausted → byte-snapshot
               rollback of every applied file (never git checkout — the
               user's worktree may be dirty) and an honest rolled-back report
    REPORT     deterministic, no LLM: the report is ASSEMBLED from receipts
               (metrics/suggestions/decision/edits+diffs/test receipts/fix
               history/rollback), lands at workspace/report_<ts>.md, the
               chat reply is a compact summary; applied-and-not-rolled-back
               → best-effort loopback hot reload (POST {reload_url}); an
               app-templates/<code> entry earns a stale reminder (remind,
               never sync)

Inter-station state travels via ``cxt.graph_state["session_reviewer_state"]``
(cleared by the runtime at graph termination); the final trace goes to
``cxt.metadata["session_reviewer"]`` — consumed by the engine at turn end
(picked into the persisted trace trail as an ``app_trace`` event) and returned
via ``TurnResult.extra`` for same-turn observability.

Runaway protection (three independent guards): graph steps (route.py
config.max_steps=16) × fix_rounds (semantic, default 2 — the self-loop's
cap) × the shell tool timeout (guardrails.shell_tool, the pytest backstop);
wait_human suspension/resume does not burn steps (the engine accounts
``__step__`` across suspensions).
"""

import asyncio
import contextlib
import difflib
import json
import logging
import re
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import yaml

from atoms.executors.loop_executor import _emit_round, _stream_round

from nexus.engine.agent_hooks import (
    AgentEndEvent,
    LLMCallEvent,
    LLMResponseEvent,
    fire,
    resolve_agent_hooks,
)
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.loop import TurnResult, _execute_tool
from nexus.engine.tool_context import tool_call_context
from nexus.llm.resolve import build_provider
from nexus.settings import get_pattern_custom_config, get_session_db_path

from apps.session_reviewer_agent.prompts import (
    REVIEW_RETRY_PROMPT,
    SESSION_REVIEWER_BASE_PROMPT,
    REVIEW_PHASE_TMPL,
    EDIT_PHASE_TMPL,
    FIX_PHASE_TMPL,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Budgets / caps (code defaults; the app config bag overrides by key)
# ---------------------------------------------------------------------------

_DEFAULT_FIX_ROUNDS = 2        # sr_fixloop self-loop rounds before rollback
_DEFAULT_REVIEW_RETRIES = 1    # sr_review JSON parse-failure self-correction retries
_DEFAULT_WORKSPACE_ROOT = "data/session_reviewer_agent"
_DEFAULT_RELOAD_URL = ""       # code default OFF (offline-safe); the repo
                               # config.yaml enables the loopback default
_DEFAULT_APPS_ROOT = str(Path(__file__).resolve().parents[2] / "apps")
_DEFAULT_TESTS_ROOT = str(Path(__file__).resolve().parents[2] / "tests")

# The writable surface (deny-by-default): ONLY these basenames inside the
# TARGET app dir may be edited by the apply/fix stations; route.py / tools.py
# are context-only (suggestion-level, never edited)
_EDITABLE_FILES = ("prompts.py", "config.yaml", "faq.py", "slots.py")
_CONTEXT_FILES = ("route.py", "tools.py")
_TARGET_KINDS_EDITABLE = ("prompt", "config", "rule")

_SOURCE_CHAR_CAP = 12000       # per editable file entering the state board
_CONTEXT_CHAR_CAP = 4000       # per context file (route.py/tools.py digest)
_EDIT_INPUT_CHAR_CAP = 50000   # per file entering an edit/fix prompt
_STATE_MSG_CHAR_CAP = 1200     # message content on the state board
_STATE_MSG_LIMIT = 120
_STATE_EVENT_PAYLOAD_CAP = 700
_STATE_EVENT_LIMIT = 300
_FILE_MSG_CHAR_CAP = 4000      # the fuller copies inside input.json
_FILE_EVENT_PAYLOAD_CAP = 2000
_TIMELINE_CHAR_BUDGET = 12000
_DIFF_CHAR_CAP = 6000
_SUGGESTION_CAP = 12
_EDIT_CAP = 8

_FORCE_CLOSE_REPLY = ("(会话评审流程被步数预算截断,已产出的回执见汇报,"
                      "未完成步骤如实标注。)")
_JSON_RETRY_PROMPT = ("上一次输出不是合法 JSON（{error}）。"
                      "重新只输出符合契约的 JSON 对象，不要任何解释文字。")

# Approval intent parsing (deterministic, never model-decided). Reject is
# tested FIRST — "不通过" contains "通过"; the reject list is explicit whole
# phrases (NOT a generic 不X class): a partial approval like "通过，但 S2
# 不改" must stay an approval whose note carries the exclusions.
_REJECT_RE = re.compile(
    r"不(通过|同意|确认|批准|要)|拒绝|取消|算了|保持现状|不用改|不需要"
    r"|不用了|reject|deny|cancel", re.IGNORECASE)
_APPROVE_RE = re.compile(
    r"通过|同意|确认|批准|approve|\bok\b|好的?|可以|就这样|应用|执行|改吧"
    r"|更新吧|安排", re.IGNORECASE)

_SESSION_ID_RE = re.compile(
    r"(?:session[_\- ]?id|会话|sess)\s*(?:id)?\s*(?:为|是|:|：|=|，|,|#)?\s*"
    r"([A-Za-z0-9][A-Za-z0-9._:\-]{3,127})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Station codes (= plugin codes; route.py binds accordingly)
# ---------------------------------------------------------------------------

SR_ROUTE_CODE = "sr_route"
SR_COLLECT_CODE = "sr_collect"
SR_METRICS_CODE = "sr_metrics"
SR_REVIEW_CODE = "sr_review"
SR_WAIT_CODE = "sr_wait_human"
SR_APPLY_CODE = "sr_apply"
SR_FIXLOOP_CODE = "sr_fixloop"
SR_REPORT_CODE = "sr_report"

PATTERN_CODE = "session_reviewer"
_STATE_KEY = "session_reviewer_state"
_TRACE_KEY = "session_reviewer"


def _runtime_settings() -> Dict[str, Any]:
    """The single read point for budgets / roots: the app config bag (the
    config: section of apps/session_reviewer_agent/config.yaml, via
    get_pattern_custom_config) overrides the code defaults. int keys accept
    ≥ minimum (a broken yaml value → warn + default); missing keys fall back
    to the module constants (tests patch the constants)."""
    bag = get_pattern_custom_config(PATTERN_CODE)

    def limit(key: str, default: int) -> int:
        raw = bag.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = -1
        if isinstance(raw, bool) or value < 0:
            logger.warning("[session_reviewer] config.%s 应为 int ≥ 0,实际为 %r,"
                           "按默认 %r 处理", key, raw, default)
            return default
        return value

    def text(key: str, default: str) -> str:
        raw = bag.get(key)
        return str(raw) if raw not in (None, "") else default

    def flag(key: str, default: bool) -> bool:
        raw = bag.get(key)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    return {
        "auto_approve": flag("auto_approve", False),
        "run_tests": flag("run_tests", True),
        "fix_rounds": limit("fix_rounds", _DEFAULT_FIX_ROUNDS),
        "review_retries": limit("review_retries", _DEFAULT_REVIEW_RETRIES),
        "workspace_root": text("workspace_root", _DEFAULT_WORKSPACE_ROOT),
        "apps_root": text("apps_root", _DEFAULT_APPS_ROOT),
        "tests_root": text("tests_root", _DEFAULT_TESTS_ROOT),
        "session_db_path": text("session_db_path", ""),
        "reload_url": text("reload_url", _DEFAULT_RELOAD_URL),
    }


def _db_path(settings: Dict[str, Any]) -> str:
    """The reviewed sessions DB: bag override > the global session DB."""
    declared = settings.get("session_db_path") or ""
    return declared or get_session_db_path()


# ---------------------------------------------------------------------------
# State board helpers
# ---------------------------------------------------------------------------

def _absolutize(raw: str) -> Path:
    """Pin a declared root to an absolute path (relative roots resolve
    against the service startup directory — the same semantics as the file
    tools; paths on the state board must be absolute)."""
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def _new_state(cxt, request: str, session_id: str) -> Dict[str, Any]:
    safe_session = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cxt.session_id)) or "s"
    workspace = str(_absolutize(_DEFAULT_WORKSPACE_ROOT) / safe_session)
    return {
        "request": request,
        "session_id": session_id,
        "session_id_source": "",
        "workspace": workspace,
        "data_error": "",
        "input_path": "",
        "session_summary": {},
        "messages": [],      # trimmed [{role, content, stage}]
        "events": [],        # trimmed [{turn_id, kind, payload}]
        "target": {"pattern_code": "", "app_name": "", "app_dir": "",
                   "editable": {}, "context": {}, "app_dir_missing": False},
        "metrics": {},
        "timeline": "",
        "suggestions": [],
        "review_summary": "",
        "review_degraded": False,
        "decision": {"approved": False, "note": "", "mode": ""},
        "backup_dir": "",
        "edits_log": [],
        "applied_files": [],
        "test_receipt": {},
        "fix_history": [],   # anti-replay: every fix round's edits + result
        "rolled_back": False,
        "report_path": "",
        "reload_receipt": {},
        "phases": [],
    }


def _load_state(cxt) -> Optional[Dict[str, Any]]:
    state = (cxt.graph_state or {}).get(_STATE_KEY)
    return state if isinstance(state, dict) else None


def _save_state(cxt, state: Dict[str, Any]) -> None:
    cxt.graph_state[_STATE_KEY] = state


def _ensure_workspace(ec, state: Dict[str, Any]) -> None:
    """Workspace root honors the app config bag override (tests patch the
    _DEFAULT_* constant at a tmp dir); idempotent."""
    root = _runtime_settings()["workspace_root"]
    safe_session = re.sub(r"[^A-Za-z0-9_-]+", "_",
                          str(ec.cxt.session_id)) or "s"
    state["workspace"] = str(_absolutize(root or _DEFAULT_WORKSPACE_ROOT)
                             / safe_session)


def _current_user_query(cxt) -> str:
    for m in reversed(cxt.history or []):
        if getattr(m, "role", "") == "user":
            return getattr(m, "content", "") or ""
    return ""


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@contextlib.contextmanager
def _station_tool_context(ec):
    """Publish the station's position around raw ``_execute_tool`` dispatches
    (the custom stations bypass the default loop executor, which normally
    publishes this context). ``pattern_code`` keys the app guardrails overlay
    (apps/session_reviewer_agent/config.yaml ``guardrails:``, e.g. the shell
    300s loosening for pytest regression runs)."""
    with tool_call_context(
        (getattr(ec.cxt, "llm_config", None) or {}),
        getattr(ec.pattern, "allow_toolset", None) or [],
        session_id=getattr(ec.cxt, "session_id", "") or "",
        pattern_code=getattr(ec.pattern, "code", "") or "",
    ):
        yield


# ---------------------------------------------------------------------------
# Deterministic helpers — extraction / collection / mapping
# ---------------------------------------------------------------------------

def _extract_session_id(cxt, query: str) -> Tuple[str, str]:
    """session_id resolution: launch task_info first, then a keyword regex
    over the user message. Returns (id, source) — empty id when absent."""
    task = cxt.metadata.get("task_info")
    if isinstance(task, dict):
        sid = str(task.get("session_id") or "").strip()
        if sid:
            return sid, "task_info"
    match = _SESSION_ID_RE.search(query or "")
    if match:
        sid = match.group(1).strip().rstrip("。，,；;！!？?）)】」\"' 的")
        if sid:
            return sid, "message"
    return "", ""


def _locate_app_dir(pattern_code: str, apps_root: Path) -> Optional[Path]:
    """pattern_code → apps/<dir>: config.yaml ``pattern:`` binding first
    (works for any file layout), route.py declaration regex as the fallback
    (apps without a config.yaml). None when not under apps/ (the review
    continues dialogue-only; apply is naturally skipped)."""
    if not pattern_code or not apps_root.is_dir():
        return None
    for cfg in sorted(apps_root.glob("*/config.yaml")):
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if isinstance(data, dict) and str(data.get("pattern") or "") == pattern_code:
            return cfg.parent
    decl = re.compile(
        r"Pattern\s*\(\s*code\s*=\s*[\"']" + re.escape(pattern_code) + r"[\"']")
    for route in sorted(apps_root.glob("*/route.py")):
        try:
            if decl.search(route.read_text(encoding="utf-8")):
                return route.parent
        except OSError:
            continue
    return None


def _read_target_sources(app_dir: Path) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """The target app's writable surface + structural context, capped."""
    editable: Dict[str, Dict] = {}
    for name in _EDITABLE_FILES:
        f = app_dir / name
        entry: Dict[str, Any] = {"exists": f.is_file()}
        if entry["exists"]:
            text = f.read_text(encoding="utf-8", errors="replace")
            entry["chars"] = len(text)
            entry["content"] = text[:_SOURCE_CHAR_CAP]
        editable[name] = entry
    context: Dict[str, Dict] = {}
    for name in _CONTEXT_FILES:
        f = app_dir / name
        entry = {"exists": f.is_file()}
        if entry["exists"]:
            text = f.read_text(encoding="utf-8", errors="replace")
            entry["chars"] = len(text)
            entry["content"] = text[:_CONTEXT_CHAR_CAP]
        context[name] = entry
    return editable, context


async def _collect_session_data(db_path: str,
                                session_id: str) -> Dict[str, Any]:
    """Read-only triple-table fetch from the sessions DB (URI mode=ro — the
    reviewer never writes the audited store). Every failure degrades to an
    honest ``{"ok": False, "error": ...}`` receipt."""
    p = Path(db_path)
    if not p.is_file():
        return {"ok": False, "error": f"会话库不存在: {p}"}
    try:
        conn = await aiosqlite.connect(f"{p.resolve().as_uri()}?mode=ro",
                                       uri=True)
    except Exception as e:  # noqa: BLE001 -- degrade honestly, never crash the turn
        return {"ok": False, "error": f"会话库打开失败: {e}"}
    try:
        conn.row_factory = aiosqlite.Row
        rows = await conn.execute_fetchall(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        if not rows:
            return {"ok": False,
                    "error": f"会话 {session_id} 不存在于会话库 {p}"}
        row = dict(rows[-1])
        messages = [dict(r) for r in await conn.execute_fetchall(
            """SELECT id, launch_epoch, role, content, stage, metadata, created_at
               FROM messages WHERE session_id = ? ORDER BY id LIMIT 600""",
            (session_id,))]
        events = [dict(r) for r in await conn.execute_fetchall(
            """SELECT id, turn_id, launch_epoch, kind, payload, truncated, created_at
               FROM trace_events WHERE session_id = ? ORDER BY id LIMIT 800""",
            (session_id,))]
        return {"ok": True, "session_row": row,
                "messages": messages, "events": events}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"会话库读取失败: {e}"}
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


def _trim_messages(rows: List[Dict[str, Any]], char_cap: int,
                   limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        out.append({
            "role": str(r.get("role") or ""),
            "stage": str(r.get("stage") or ""),
            "content": str(r.get("content") or "")[:char_cap],
        })
    return out


def _trim_events(rows: List[Dict[str, Any]], payload_cap: int,
                 limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        try:
            payload = json.loads(r.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {"_unparsable": str(r.get("payload"))[:256]}
        if not isinstance(payload, dict):
            payload = {"value": str(payload)[:payload_cap]}
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        if len(raw) > payload_cap:
            payload = {"_truncated": True, "_preview": raw[:payload_cap]}
        out.append({
            "turn_id": str(r.get("turn_id") or ""),
            "kind": str(r.get("kind") or ""),
            "payload": payload,
        })
    return out


# ---------------------------------------------------------------------------
# Deterministic helpers — metrics / timeline
# ---------------------------------------------------------------------------

def _payload_has_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    err = str(payload.get("error") or "").strip()
    return bool(err) and err.lower() not in ("none", "null", "0", "false")


def _compute_metrics(messages: List[Dict[str, Any]],
                     events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The six computable signal families. Defensive on payload shapes:
    unknown shapes count 0 (the review prompt sees the raw trail too, so the
    LLM layer can compensate — the metrics never fabricate)."""
    turns: List[str] = []
    for e in events:
        t = str(e.get("turn_id") or "")
        if t and t not in turns:
            turns.append(t)
    turn_errors = [e for e in events if e.get("kind") == "turn_error"]
    tool_calls = [e for e in events if e.get("kind") == "tool_call"]
    tool_results = [e for e in events if e.get("kind") == "tool_result"]
    tool_failures = [e for e in tool_results if _payload_has_error(e.get("payload"))]
    clarify = ([m for m in messages if "clarify" in str(m.get("stage") or "").lower()]
               + [e for e in events if "clarify" in str(e.get("kind") or "").lower()])
    node_counter: Counter = Counter()
    for e in events:
        if e.get("kind") == "node_start":
            payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            code = str(payload.get("node_code") or payload.get("node") or "")
            if code:
                node_counter[code] += 1
    return {
        "turns": len(turns) or (1 if messages else 0),
        "messages_total": len(messages),
        "user_messages": len([m for m in messages if m.get("role") == "user"]),
        "assistant_messages": len([m for m in messages if m.get("role") == "assistant"]),
        "turn_errors": len(turn_errors),
        "tool_calls": len(tool_calls),
        "tool_results": len(tool_results),
        "tool_failures": len(tool_failures),
        "clarify_total": len(clarify),
        "graph_waits": len([e for e in events if e.get("kind") == "graph_wait"]),
        "graph_resumes": len([e for e in events if e.get("kind") == "graph_resume"]),
        "node_visits": dict(node_counter),
        "revisited_nodes": {k: v for k, v in node_counter.items() if v > 1},
        "events_total": len(events),
    }


def _build_timeline(messages: List[Dict[str, Any]],
                    events: List[Dict[str, Any]],
                    char_budget: int = _TIMELINE_CHAR_BUDGET) -> str:
    """Two honest sections — messages carry no turn_id in the store, so the
    message flow and the per-turn node chains are stated separately (never a
    fabricated merge)."""
    lines: List[str] = ["[消息流]"]
    for i, m in enumerate(messages[:40]):
        role = str(m.get("role") or "?")
        stage = str(m.get("stage") or "")
        content = str(m.get("content") or "").replace("\n", " ")[:160]
        lines.append(f"[{i}] {role}{'(' + stage + ')' if stage else ''}: {content}")
    if len(messages) > 40:
        lines.append(f"(另有 {len(messages) - 40} 条消息省略)")

    lines.append("[轨迹流]")
    by_turn: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for e in events:
        t = str(e.get("turn_id") or "") or "(无turn_id)"
        if t not in by_turn:
            by_turn[t] = []
            order.append(t)
        by_turn[t].append(e)
    for t in order[:20]:
        chain: List[str] = []
        tools = 0
        errors = 0
        for e in by_turn[t]:
            kind = str(e.get("kind") or "")
            if kind == "node_start":
                payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                code = str(payload.get("node_code") or payload.get("node") or "?")
                chain.append(code)
            elif kind == "tool_call":
                tools += 1
            elif kind == "turn_error":
                errors += 1
        lines.append(f"turn {t}: {' → '.join(chain) or '(无节点事件)'}"
                     + (f" · 工具×{tools}" if tools else "")
                     + (f" · turn_error×{errors}" if errors else ""))
    if len(order) > 20:
        lines.append(f"(另有 {len(order) - 20} 轮省略)")

    text = "\n".join(lines)
    return text if len(text) <= char_budget else text[:char_budget] + "\n(时间线已截断)"


# ---------------------------------------------------------------------------
# Deterministic helpers — edits / tests / backup / reload
# ---------------------------------------------------------------------------

def _apply_edit_pairs(text: str,
                      edits: List[Dict[str, Any]]) -> Tuple[str, List[Dict], List[str]]:
    """Mechanical application: first-occurrence exact replace; a miss is
    dropped (never fuzzy-landed), empty/no-change edits are skipped."""
    applied: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for e in edits:
        old = str(e.get("old_string") or "")
        new = str(e.get("new_string") or "")
        if not old or old == new:
            skipped.append("空/无变化编辑")
            continue
        count = text.count(old)
        if count == 0:
            skipped.append(f"未命中原文: {old[:80]!r}")
            continue
        text = text.replace(old, new, 1)
        applied.append({
            "old_string": old[:160], "new_string": new[:160],
            "rationale": str(e.get("rationale") or "")[:200],
            "occurrences": count,
        })
    return text, applied, skipped


def _normalize_edits(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        old, new = str(e.get("old_string") or ""), str(e.get("new_string") or "")
        if old and new and old != new:
            out.append({"old_string": old, "new_string": new,
                        "rationale": str(e.get("rationale") or "")[:200]})
        if len(out) >= _EDIT_CAP:
            break
    return out


def _unified_diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=2))


def _pytest_plan(app_name: str, tests_root: Path) -> Optional[Tuple[str, List[str]]]:
    """The whitelisted regression command: file names come ONLY from a glob
    of the target app's name under tests/ — the model never touches the
    command line. None → no whitelisted tests (an honest skip receipt)."""
    if not app_name or not tests_root.is_dir():
        return None
    import shlex
    files = sorted(tests_root.glob(f"test*{app_name}*.py"))
    if not files:
        return None
    repo_root = tests_root.parent
    names = []
    for f in files:
        try:
            names.append(str(f.relative_to(repo_root)))
        except ValueError:
            names.append(str(f))
    cmd = "python -m pytest " + " ".join(shlex.quote(n) for n in names) + " -x -q"
    return cmd, names


async def _run_tests(ec, cmd: str, workdir: str) -> Dict[str, Any]:
    """One whitelisted pytest run through the bash tool (guardrail-wired,
    trace-visible). Non-zero exit / timeout is never called success."""
    with _station_tool_context(ec):
        raw = await _execute_tool("bash", {"command": cmd, "workdir": workdir})
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        env = {"error": f"bash 工具返回无法解析: {str(raw)[:200]}"}
    if "error" in env:
        return {"ok": False, "command": cmd, "exit_code": None, "timed_out": False,
                "stdout_tail": "", "stderr_tail": "",
                "error": str(env["error"])[:300]}
    exit_code = env.get("exit_code")
    timed_out = bool(env.get("timed_out"))
    return {
        "ok": exit_code == 0 and not timed_out,
        "command": cmd,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_tail": str(env.get("stdout") or "")[-4000:],
        "stderr_tail": str(env.get("stderr") or "")[-1500:],
    }


def _rollback_files(state: Dict[str, Any]) -> List[str]:
    """Byte-snapshot rollback of every applied file (never git checkout —
    the user's worktree may carry uncommitted work of their own)."""
    restored: List[str] = []
    backup_dir = Path(state.get("backup_dir") or "")
    app_dir = Path(str((state.get("target") or {}).get("app_dir") or ""))
    if not (backup_dir.is_dir() and app_dir):
        return restored
    for name in state.get("applied_files") or []:
        src = backup_dir / name
        if src.is_file():
            try:
                (app_dir / name).write_bytes(src.read_bytes())
                restored.append(name)
            except OSError as e:
                logger.warning("[session_reviewer] 回滚 %s 失败: %s", name, e)
    return restored


def _post_reload(url: str) -> Dict[str, Any]:
    """Best-effort loopback hot reload (the apps layer may not import host —
    the reload endpoint is called over HTTP; any failure is an honest
    receipt with a manual hint, never a blocker)."""
    try:
        req = urllib.request.Request(
            url, data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(2000).decode("utf-8", "replace")
            return {"ok": resp.status == 200, "status": resp.status,
                    "body": body[:300]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "hint": "请手动 POST /api/v1/reload（或重启进程）使改动生效"}


def _parse_approval(answer: str) -> str:
    """approve / reject / unknown (reject tested first — "不通过" contains
    "通过"; never model-decided)."""
    text = (answer or "").strip()
    if not text:
        return "unknown"
    if _REJECT_RE.search(text):
        return "reject"
    if _APPROVE_RE.search(text):
        return "approve"
    return "unknown"


# ---------------------------------------------------------------------------
# Shared LLM JSON helper (private workspace, never session history)
# ---------------------------------------------------------------------------

def _extract_json_object(content: str) -> Tuple[Dict[str, Any], str]:
    """First balanced ``{...}`` block → parse (fault-tolerant protocol)."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {}, "输出中找不到 JSON 对象(缺少 {...})"
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as e:
        return {}, f"JSON 语法错误: {e}"
    if not isinstance(data, dict):
        return {}, "JSON 顶层不是对象"
    return data, ""


async def _llm_json(ec, user_prompt: str, retry_prompt: str, retries: int,
                    validate) -> Tuple[Dict[str, Any], str]:
    """One JSON-protocol LLM call with bounded self-correct retries; the
    body never streams to the user (thinking only) — the station text is
    protocol chatter, the user-visible reply is the report station's."""
    cxt = ec.cxt
    node, pattern = ec.node, ec.pattern
    hooks = resolve_agent_hooks(node, pattern)
    provider = build_provider(cxt.llm_config or {})
    llm_config = cxt.llm_config or {}
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SESSION_REVIEWER_BASE_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    out: Dict[str, Any] = {}
    err = ""
    for attempt in range(1 + max(0, retries)):
        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=cxt.session_id, node_code=node.code, round_idx=0,
                messages=messages, model=llm_config.get("model", "")))
        result = await _stream_round(
            provider, messages, llm_config.get("model", "default"),
            llm_config.get("temperature", 0.2),
            llm_config.get("max_tokens", 4096), ec.stream, forward_text=False)
        content = result.get("content", "") or ""
        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=cxt.session_id, node_code=node.code, round_idx=0,
                content=content, tool_calls=[]))
        out, err = _extract_json_object(content)
        if out and validate(out):
            return out, ""
        if attempt < retries:
            messages.append({"role": "assistant", "content": content or "(空输出)"})
            messages.append({"role": "user",
                             "content": retry_prompt.replace("{error}", err)})
    return {}, err or "输出不可用"


def _validate_review(out: Dict[str, Any]) -> bool:
    return isinstance(out.get("suggestions"), list)


def _validate_edits(out: Dict[str, Any]) -> bool:
    return isinstance(out.get("edits"), list)


def _normalize_suggestions(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        problem = str(s.get("problem") or "").strip()
        suggestion = str(s.get("suggestion") or "").strip()
        if not problem or not suggestion:
            continue
        severity = str(s.get("severity") or "medium").lower()
        out.append({
            "id": str(s.get("id") or f"S{len(out) + 1}")[:8],
            "dimension": str(s.get("dimension") or "reply")[:16],
            "severity": severity if severity in ("high", "medium", "low") else "medium",
            "problem": problem[:400],
            "evidence": str(s.get("evidence") or "")[:300],
            "suggestion": suggestion[:500],
            "target_file": str(s.get("target_file") or "")[:32],
            "target_kind": str(s.get("target_kind") or "none")[:12],
        })
        if len(out) >= _SUGGESTION_CAP:
            break
    return out


def _gate_listing(state: Dict[str, Any]) -> str:
    """The suspension reply: the numbered suggestion listing + the edit
    boundary, phrased for a one-word human answer."""
    lines = ["【评审建议清单】请人工确认后回复：", ""]
    for s in state.get("suggestions") or []:
        mark = "可实施" if (s.get("target_kind") in _TARGET_KINDS_EDITABLE
                          and s.get("target_file") in _EDITABLE_FILES) else "仅建议"
        lines.append(
            f"{s['id']}. [{s['severity']}/{s['dimension']}/{mark}] "
            f"{s['problem']}")
        lines.append(f"   证据: {s['evidence'] or '(未引用)'}")
        lines.append(f"   建议: {s['suggestion']}"
                     + (f"（目标: {s['target_file']}）" if s.get("target_file") else ""))
    lines.append("")
    lines.append("回复「通过」（可附批注，如：通过，但 S2 不改）→ 对可实施项落编辑并跑"
                 "白名单测试；回复「取消/拒绝」→ 仅出报告不改代码。")
    lines.append("可实施面仅限目标应用的 prompts.py / config.yaml / faq.py / "
                 "slots.py；route.py / tools.py 仅建议。改动前有字节快照，测试"
                 "失败自动修复，仍失败自动回滚。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The eight station executors
# ---------------------------------------------------------------------------

class SrRouteExecutor(NodeExecutor):
    """sr_route: deterministic id extraction + state-board initialization;
    a missing id ends the turn with an ask (no route output = terminal —
    the next user message re-runs the graph from entry)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        query = _current_user_query(cxt)
        sid, source = _extract_session_id(cxt, query)
        if not sid:
            return TurnResult(content=(
                "请提供待评审的会话 ID（例如：评审会话 <session_id>），或在 "
                "launch 时通过 task_info.session_id 传入。"))
        state = _new_state(cxt, query, sid)
        state["session_id_source"] = source
        _ensure_workspace(ec, state)
        state["phases"].append("route")
        _save_state(cxt, state)
        _emit_round(ec.stream, "route", 0)
        return TurnResult(content="", next=SR_COLLECT_CODE)


class SrCollectExecutor(NodeExecutor):
    """sr_collect: read-only collection (DB triple-table + app mapping +
    sources); DB missing / session absent → honest escape edge to REPORT."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), "")
            _ensure_workspace(ec, state)

        settings = _runtime_settings()
        data = await _collect_session_data(
            _db_path(settings), str(state.get("session_id") or ""))
        if not data.get("ok"):
            state["data_error"] = str(data.get("error") or "取数失败")
            state["phases"].append("collect_failed")
            _save_state(cxt, state)
            logger.warning("[session_reviewer] 取数失败: %s", state["data_error"])
            return TurnResult(content="", next=SR_REPORT_CODE)

        row = data["session_row"]
        pattern_code = str(row.get("pattern_code") or "")
        state["target"]["pattern_code"] = pattern_code
        state["session_summary"] = {
            "session_id": row.get("session_id"),
            "pattern_code": pattern_code,
            "launch_epoch": row.get("launch_epoch"),
            "task_info": str(row.get("task_info") or "{}")[:800],
            "current_node_code": row.get("current_node_code"),
            "message_count": len(data["messages"]),
            "event_count": len(data["events"]),
        }
        state["messages"] = _trim_messages(
            data["messages"], _STATE_MSG_CHAR_CAP, _STATE_MSG_LIMIT)
        state["events"] = _trim_events(
            data["events"], _STATE_EVENT_PAYLOAD_CAP, _STATE_EVENT_LIMIT)

        app_dir = _locate_app_dir(
            pattern_code, _absolutize(settings["apps_root"]))
        if app_dir is not None:
            editable, context = _read_target_sources(app_dir)
            state["target"].update({
                "app_name": app_dir.name, "app_dir": str(app_dir),
                "editable": editable, "context": context,
            })
        else:
            state["target"]["app_dir_missing"] = True

        # The fuller dump lands in the workspace; the state board keeps the
        # bounded copies (snapshot size discipline)
        try:
            ws = Path(state["workspace"])
            ws.mkdir(parents=True, exist_ok=True)
            dump = {
                "session_row": {k: str(v)[:1500] for k, v in row.items()},
                "messages": _trim_messages(
                    data["messages"], _FILE_MSG_CHAR_CAP, 400),
                "events": _trim_events(
                    data["events"], _FILE_EVENT_PAYLOAD_CAP, 600),
            }
            input_path = ws / "input.json"
            input_path.write_text(
                json.dumps(dump, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8")
            state["input_path"] = str(input_path)
        except OSError as e:
            logger.warning("[session_reviewer] input.json 落盘失败(不影响评审): %s", e)

        state["phases"].append("collect")
        _save_state(cxt, state)
        _emit_round(ec.stream, "collect", 0)
        return TurnResult(content="", next=SR_METRICS_CODE)


class SrMetricsExecutor(NodeExecutor):
    """sr_metrics: the six deterministic signal families + the timeline
    digest the review prompt consumes."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), "")
            _ensure_workspace(ec, state)

        state["metrics"] = _compute_metrics(
            state.get("messages") or [], state.get("events") or [])
        state["timeline"] = _build_timeline(
            state.get("messages") or [], state.get("events") or [])
        state["phases"].append("metrics")
        _save_state(cxt, state)
        _emit_round(ec.stream, "metrics", 0)
        return TurnResult(content="", next=SR_REVIEW_CODE)


class SrReviewExecutor(NodeExecutor):
    """sr_review: one tool-less LLM call — metrics + timeline + sources into
    the 5-dim rubric; JSON with one self-correct retry; unparseable or empty
    → honest escape to REPORT (never fabricated)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), "")
            _ensure_workspace(ec, state)

        target = state.get("target") or {}
        parts: List[str] = []
        if target.get("app_dir"):
            parts.append(f"应用目录: {target['app_dir']}"
                         + ("（未在 apps/ 下定位，仅对话面评审）"
                            if target.get("app_dir_missing") else ""))
        for name in _EDITABLE_FILES:
            entry = (target.get("editable") or {}).get(name) or {}
            if entry.get("exists"):
                lang = "yaml" if name.endswith((".yaml", ".yml")) else "python"
                parts.append(f"### {name}（可编辑，{entry.get('chars', 0)} 字符）\n"
                             f"```{lang}\n{entry.get('content') or ''}\n```")
        for name in _CONTEXT_FILES:
            entry = (target.get("context") or {}).get(name) or {}
            if entry.get("exists"):
                parts.append(f"### {name}（结构摘要，仅建议面）\n"
                             f"```python\n{entry.get('content') or ''}\n```")
        sources = "\n\n".join(parts) or "(无应用源码可用)"

        user_prompt = REVIEW_PHASE_TMPL.format(
            metrics_json=json.dumps(state.get("metrics") or {},
                                    ensure_ascii=False, indent=1),
            timeline=state.get("timeline") or "(无轨迹)",
            sources=sources,
        )
        out, err = await _llm_json(
            ec, user_prompt, REVIEW_RETRY_PROMPT,
            _runtime_settings()["review_retries"], _validate_review)

        if not out:
            state["review_degraded"] = True
            state["review_summary"] = f"评审输出不可解析: {err[:200]}"
            state["phases"].append("review_degraded")
            _save_state(cxt, state)
            logger.warning("[session_reviewer] 评审 JSON 解析失败: %s", err)
            return TurnResult(content="", next=SR_REPORT_CODE)

        suggestions = _normalize_suggestions(out.get("suggestions"))
        state["suggestions"] = suggestions
        state["review_summary"] = str(out.get("summary") or "")[:600]
        if not suggestions:
            state["phases"].append("review_empty")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)
        state["phases"].append("review")
        _save_state(cxt, state)
        _emit_round(ec.stream, "review", len(suggestions))
        return TurnResult(content="", next=SR_WAIT_CODE)


class SrWaitHumanExecutor(NodeExecutor):
    """sr_wait_human: the deterministic approval gate. Fresh visit: suspend
    with the suggestion listing (or pass through on auto_approve). Resume
    visit: keyword-parse the human answer — approve (note kept, feeds the
    edit prompt) / reject (report-only) / unrecognized (brief re-ask,
    suspend again). No external side effects → idempotent across
    re-execution (the engine's documented executor responsibility)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), "")
            _ensure_workspace(ec, state)

        if ec.resume_input is None:
            if _runtime_settings()["auto_approve"]:
                state["decision"] = {"approved": True, "mode": "auto",
                                     "note": "(auto_approve=true 自动通过)"}
                state["phases"].append("gate_auto")
                _save_state(cxt, state)
                return TurnResult(content="", next=SR_APPLY_CODE)
            state["phases"].append("gate_wait")
            _save_state(cxt, state)
            return TurnResult(content=_gate_listing(state), wait_human=True,
                              extra={"wait_message": "评审建议等待人工确认"})

        answer = str(ec.resume_input).strip()
        verdict = _parse_approval(answer)
        if verdict == "approve":
            state["decision"] = {"approved": True, "mode": "human",
                                 "note": answer[:400]}
            state["phases"].append("gate_approved")
            _save_state(cxt, state)
            _emit_round(ec.stream, "gate", 0)
            return TurnResult(content="", next=SR_APPLY_CODE)
        if verdict == "reject":
            state["decision"] = {"approved": False, "mode": "human",
                                 "note": answer[:400]}
            state["test_receipt"] = {"skipped": True,
                                     "reason": "人工闸拒绝，未进入实施环节"}
            state["phases"].append("gate_rejected")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)
        return TurnResult(content=(
            "未识别你的意图。回复「通过」（可附批注，例如：通过，但 S2 不改）"
            "执行优化；回复「取消/拒绝」则仅出报告不改代码。"),
            wait_human=True)


class SrApplyExecutor(NodeExecutor):
    """sr_apply: byte snapshot → per-file edit pairs (LLM drafts, code
    applies) → whitelisted pytest. Nothing actionable / clean / skipped →
    REPORT; failure → FIXLOOP."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), "")
            _ensure_workspace(ec, state)

        settings = _runtime_settings()
        target = state.get("target") or {}
        app_dir = Path(str(target.get("app_dir") or ""))

        if not (state.get("decision") or {}).get("approved"):
            state["test_receipt"] = {"skipped": True, "reason": "人工闸未通过"}
            state["phases"].append("apply_skipped")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)
        if state.get("data_error") or not str(app_dir):
            state["test_receipt"] = {"skipped": True,
                                     "reason": "无可实施面（取数失败或应用未定位）"}
            state["phases"].append("apply_skipped")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)

        # Group approved suggestions by writable target (deny-by-default)
        actionable: Dict[str, List[Dict[str, Any]]] = {}
        for s in state.get("suggestions") or []:
            if (s.get("target_kind") in _TARGET_KINDS_EDITABLE
                    and s.get("target_file") in _EDITABLE_FILES):
                entry = (target.get("editable") or {}).get(s["target_file"]) or {}
                if entry.get("exists"):
                    actionable.setdefault(s["target_file"], []).append(s)
        if not actionable:
            state["test_receipt"] = {"skipped": True,
                                     "reason": "无可实施目标（建议均为仅建议级，或目标文件缺失）"}
            state["phases"].append("apply_noop")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)

        # Byte snapshot BEFORE any write — the sole rollback source
        backup_dir = Path(state["workspace"]) / f"backup_{_ts()}"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for name in actionable:
                (backup_dir / name).write_bytes(
                    (app_dir / name).read_bytes())
            state["backup_dir"] = str(backup_dir)
        except OSError as e:
            state["test_receipt"] = {"skipped": True,
                                     "reason": f"备份失败，未做任何改动: {e}"}
            state["phases"].append("apply_noop")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)

        note = str((state.get("decision") or {}).get("note") or "") or "(无)"
        edits_log: List[Dict[str, Any]] = []
        applied_files: List[str] = []
        for name, sugs in actionable.items():
            fpath = app_dir / name
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                edits_log.append({"file": name, "applied": [],
                                  "skipped": [f"读取失败: {e}"],
                                  "summary": "", "diff": ""})
                continue
            user_prompt = EDIT_PHASE_TMPL.format(
                file_path=str(fpath),
                file_content=text[:_EDIT_INPUT_CHAR_CAP],
                suggestions_json=json.dumps(sugs, ensure_ascii=False, indent=1),
                decision_note=note,
            )
            out, err = await _llm_json(ec, user_prompt, _JSON_RETRY_PROMPT, 1,
                                       _validate_edits)
            edits = _normalize_edits(out.get("edits")) if out else []
            new_text, applied, skipped = _apply_edit_pairs(text, edits)
            entry_log = {
                "file": name, "applied": applied, "skipped": skipped,
                "summary": str((out or {}).get("summary") or "")[:300]
                           or (f"解析失败: {err[:200]}" if err else ""),
                "diff": "",
            }
            if applied:
                try:
                    fpath.write_text(new_text, encoding="utf-8")
                    applied_files.append(name)
                    entry_log["diff"] = _unified_diff(
                        text, new_text, name)[:_DIFF_CHAR_CAP]
                except OSError as e:
                    entry_log["applied"] = []
                    entry_log["skipped"] = [f"写入失败(已弃): {e}"]
            edits_log.append(entry_log)

        state["edits_log"] = edits_log
        state["applied_files"] = applied_files
        if not applied_files:
            state["test_receipt"] = {"skipped": True,
                                     "reason": "编辑对为空或全部未命中原文，未改动任何文件"}
            state["phases"].append("apply_noop")
            _save_state(cxt, state)
            return TurnResult(content="", next=SR_REPORT_CODE)

        receipt: Dict[str, Any] = {"skipped": True, "reason": "run_tests=false"}
        if settings["run_tests"]:
            plan = _pytest_plan(str(target.get("app_name") or ""),
                                _absolutize(settings["tests_root"]))
            if plan:
                receipt = await _run_tests(
                    ec, plan[0], str(_absolutize(settings["tests_root"]).parent))
            else:
                receipt = {"skipped": True,
                           "reason": f"tests/ 下无 {target.get('app_name')} 的白名单测试文件"}
        state["test_receipt"] = receipt
        state["phases"].append("apply")
        _save_state(cxt, state)
        _emit_round(ec.stream, "apply", len(applied_files))

        if receipt.get("ok") or receipt.get("skipped"):
            return TurnResult(content="", next=SR_REPORT_CODE)
        return TurnResult(content="", next=SR_FIXLOOP_CODE)


class SrFixloopExecutor(NodeExecutor):
    """sr_fixloop: experience-inheriting test repair. fix_history (every
    round's edits + result) enters the prompt — round N must not re-propose
    round 1's failed edits; exhausted rounds → byte-snapshot rollback of
    every applied file and an honest rolled-back receipt."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            return TurnResult(content="", next=SR_REPORT_CODE)

        receipt = state.get("test_receipt") or {}
        if receipt.get("ok") or receipt.get("skipped"):
            return TurnResult(content="", next=SR_REPORT_CODE)  # defensive

        settings = _runtime_settings()
        rounds_done = len(state.get("fix_history") or [])
        if rounds_done >= settings["fix_rounds"]:
            restored = _rollback_files(state)
            state["rolled_back"] = True
            state["fix_history"].append(
                {"round": "rollback", "restored": restored,
                 "summary": f"自修 {rounds_done} 轮耗尽，已从字节快照回滚: "
                            f"{', '.join(restored) or '(无)'}"})
            state["phases"].append("fix_rollback")
            _save_state(cxt, state)
            logger.warning("[session_reviewer] 自修耗尽，已回滚: %s", restored)
            _emit_round(ec.stream, "fix_rollback", rounds_done)
            return TurnResult(content="", next=SR_REPORT_CODE)

        round_no = rounds_done + 1
        app_dir = Path(str((state.get("target") or {}).get("app_dir") or ""))
        hist = state.get("fix_history") or []
        hist_lines = [
            f"第 {h.get('round')} 轮: {str(h.get('summary') or '')[:300]} "
            f"(applied {h.get('edits_applied', 0)}, "
            f"{'通过' if h.get('ok') else '仍失败'})" for h in hist
        ] or ["(首轮修复,无历史)"]
        pytest_tail = str(receipt.get("stdout_tail")
                          or receipt.get("stderr_tail")
                          or receipt.get("error") or "")[-3000:]

        applied_this_round = 0
        summaries: List[str] = []
        for name in state.get("applied_files") or []:
            fpath = app_dir / name
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                summaries.append(f"{name}: 读取失败 {e}")
                continue
            user_prompt = FIX_PHASE_TMPL.format(
                file_path=str(fpath),
                file_content=text[:_EDIT_INPUT_CHAR_CAP],
                pytest_tail=pytest_tail,
                fix_history="\n".join(hist_lines),
            )
            out, err = await _llm_json(ec, user_prompt, _JSON_RETRY_PROMPT, 1,
                                       _validate_edits)
            edits = _normalize_edits(out.get("edits")) if out else []
            new_text, applied, _skipped = _apply_edit_pairs(text, edits)
            if applied:
                try:
                    fpath.write_text(new_text, encoding="utf-8")
                    applied_this_round += len(applied)
                    summaries.append(
                        f"{name}: {len(applied)} 处 ("
                        + "; ".join(a["rationale"][:60] for a in applied[:3]) + ")")
                except OSError as e:
                    summaries.append(f"{name}: 写入失败 {e}")
            else:
                summaries.append(f"{name}: 无可应用编辑"
                                 + (f"（解析失败: {err[:120]}）" if err else ""))

        new_receipt: Dict[str, Any] = {"skipped": True,
                                       "reason": "run_tests=false"}
        if settings["run_tests"]:
            plan = _pytest_plan(
                str((state.get("target") or {}).get("app_name") or ""),
                _absolutize(settings["tests_root"]))
            if plan:
                new_receipt = await _run_tests(
                    ec, plan[0], str(_absolutize(settings["tests_root"]).parent))
        state["test_receipt"] = new_receipt
        state["fix_history"].append({
            "round": round_no,
            "summary": "; ".join(summaries)[:400] or "(无编辑)",
            "edits_applied": applied_this_round,
            "ok": bool(new_receipt.get("ok")),
            "error_tail": str(new_receipt.get("stdout_tail")
                              or new_receipt.get("stderr_tail") or "")[-400:],
        })
        state["phases"].append(f"fix_{round_no}")
        _save_state(cxt, state)
        _emit_round(ec.stream, f"fix_{round_no}", round_no)

        if new_receipt.get("ok") or new_receipt.get("skipped"):
            return TurnResult(content="", next=SR_REPORT_CODE)
        return TurnResult(content="", next=SR_FIXLOOP_CODE)  # self-edge loop


class SrReportExecutor(NodeExecutor):
    """sr_report: the terminal honest report — ASSEMBLED from receipts,
    never model prose; lands at workspace/report_<ts>.md; applied-and-not-
    rolled-back → best-effort loopback reload; the chat reply is the compact
    summary."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node, pattern = ec.node, ec.pattern
        state = _load_state(cxt) or _new_state(
            cxt, _current_user_query(cxt), "")

        settings = _runtime_settings()
        report = _compose_report(state)
        report_path = ""
        try:
            ws = Path(state.get("workspace") or ".")
            ws.mkdir(parents=True, exist_ok=True)
            rp = ws / f"report_{_ts()}.md"
            rp.write_text(report, encoding="utf-8")
            report_path = str(rp)
            state["report_path"] = report_path
        except OSError as e:
            logger.warning("[session_reviewer] 报告落盘失败: %s", e)

        if state.get("applied_files") and not state.get("rolled_back"):
            url = settings["reload_url"]
            if url:
                state["reload_receipt"] = await asyncio.to_thread(
                    _post_reload, url)
            else:
                state["reload_receipt"] = {
                    "skipped": True,
                    "reason": "reload_url 未配置（启用见 config.yaml）"}
        else:
            state["reload_receipt"] = {
                "skipped": True,
                "reason": "已回滚" if state.get("rolled_back") else "未应用改动"}

        state["phases"].append("report")
        trace = {
            "reviewed_session_id": state.get("session_id", ""),
            "pattern_code": (state.get("target") or {}).get("pattern_code", ""),
            "app_name": (state.get("target") or {}).get("app_name", ""),
            "data_error": state.get("data_error", ""),
            "metrics": state.get("metrics", {}),
            "suggestions": len(state.get("suggestions") or []),
            "review_degraded": bool(state.get("review_degraded")),
            "decision": dict(state.get("decision") or {}),
            "applied_files": list(state.get("applied_files") or []),
            "rolled_back": bool(state.get("rolled_back")),
            "test_ok": bool((state.get("test_receipt") or {}).get("ok")),
            "test_skipped": bool((state.get("test_receipt") or {}).get("skipped")),
            "fix_rounds": len(state.get("fix_history") or []),
            "report_path": report_path,
            "reload": state.get("reload_receipt", {}),
            "phases": list(state.get("phases") or []),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        cxt.metadata[_TRACE_KEY] = trace

        hooks = resolve_agent_hooks(node, pattern)
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id, node_code=node.code,
                rounds=len(state.get("fix_history") or []) + 2,
                outcome="reply", reply=report))

        _emit_round(ec.stream, "final", len(state.get("suggestions") or []))
        return TurnResult(content=_compose_reply(state, report_path),
                          extra={_TRACE_KEY: trace})


# ---------------------------------------------------------------------------
# Report composing (deterministic, receipt-driven)
# ---------------------------------------------------------------------------

def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _template_stale_note(state: Dict[str, Any]) -> str:
    pattern_code = (state.get("target") or {}).get("pattern_code") or ""
    if not pattern_code:
        return ""
    entry = Path(_DEFAULT_APPS_ROOT).parent / "app-templates" / pattern_code
    return (f"app-templates/{pattern_code}/ 模板条目存在，本次代码已改动——模板"
            "已过期（按约定仅提醒，不自动同步）" if entry.is_dir() else "")


def _compose_report(state: Dict[str, Any]) -> str:
    target = state.get("target") or {}
    decision = state.get("decision") or {}
    metrics = state.get("metrics") or {}
    receipt = state.get("test_receipt") or {}
    lines: List[str] = ["# session_reviewer 评审报告", ""]
    lines.append(f"- 生成时间: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 评审会话: {state.get('session_id') or '?'}")
    lines.append(f"- 目标 pattern: {target.get('pattern_code') or '?'}"
                 f" / 应用目录: {target.get('app_dir') or '未定位（不在 apps/ 下）'}")
    gate = ("通过" if decision.get("approved") else
            "拒绝" if decision.get("mode") == "human" else "未设闸")
    lines.append(f"- 人工闸: {gate}（mode={decision.get('mode') or '-'}）"
                 + (f" 批注: {decision.get('note')}" if decision.get("note") else ""))
    if state.get("data_error"):
        lines.append(f"- ⚠ 取数失败: {state['data_error']}")

    lines += ["", "## 规则指标", "", "```json",
              json.dumps(metrics, ensure_ascii=False, indent=1), "```"]

    lines += ["", "## 评审结论", "",
              state.get("review_summary") or "(无)"]
    suggestions = state.get("suggestions") or []
    if suggestions:
        lines += ["", "| id | 维度 | 严重度 | 目标 | 问题 | 建议 |",
                  "|---|---|---|---|---|---|"]
        for s in suggestions:
            lines.append(
                f"| {s['id']} | {s['dimension']} | {s['severity']} "
                f"| {s['target_file'] or '-'}（{s['target_kind']}） "
                f"| {_md_escape(s['problem'])} | {_md_escape(s['suggestion'])} |")
        for s in suggestions:
            if s.get("evidence"):
                lines.append(f"- {s['id']} 证据: {s['evidence']}")

    if state.get("edits_log"):
        lines += ["", "## 实施记录"]
        for e in state["edits_log"]:
            lines += ["", f"### {e.get('file')}"]
            lines.append(f"意图: {e.get('summary') or '(无)'}")
            for a in e.get("applied") or []:
                lines.append(f"- ✅ applied: {a['old_string']!r} → "
                             f"{a['new_string']!r}（{a['rationale']}，"
                             f"原文出现 {a['occurrences']} 次）")
            for sk in e.get("skipped") or []:
                lines.append(f"- ⏭ skipped: {sk}")
            if e.get("diff"):
                lines += ["", "```diff", e["diff"], "```"]

    lines += ["", "## 测试回执", ""]
    if receipt.get("skipped"):
        lines.append(f"跳过: {receipt.get('reason')}")
    else:
        lines.append(f"命令: `{receipt.get('command')}`")
        lines.append(f"结果: {'通过' if receipt.get('ok') else '失败'}"
                     f"（exit={receipt.get('exit_code')}"
                     f"{'，超时' if receipt.get('timed_out') else ''}）")
        tail = str(receipt.get("stdout_tail") or receipt.get("stderr_tail")
                   or receipt.get("error") or "")
        if tail:
            lines += ["", "```", tail[-2000:], "```"]

    if state.get("fix_history"):
        lines += ["", "## 修复历史（防重放履历）"]
        for h in state["fix_history"]:
            lines.append(f"- 第 {h.get('round')} 轮: {h.get('summary')} "
                         f"({'通过' if h.get('ok') else '仍失败' if 'ok' in h else ''})")
    if state.get("rolled_back"):
        lines += ["", "## 回滚", "",
                  "自修轮数耗尽，全部已改文件已从字节快照回滚（未触碰 git 状态）。"]

    lines += ["", "## 热加载", ""]
    reload_receipt = state.get("reload_receipt") or {}
    if reload_receipt.get("skipped"):
        lines.append(f"跳过: {reload_receipt.get('reason')}")
    elif reload_receipt.get("ok"):
        lines.append(f"已触发目标 pattern 热加载（{reload_receipt.get('status')}）"
                     "——新会话即刻生效，在跑会话持旧对象跑完。")
    else:
        lines.append(f"失败: {reload_receipt.get('error') or '?'} "
                     f"{reload_receipt.get('hint', '')}")

    notes = [n for n in (
        "评审降级: " + state.get("review_summary", "") if state.get("review_degraded") else "",
        _template_stale_note(state),
    ) if n]
    if notes:
        lines += ["", "## 备注", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines)


def _compose_reply(state: Dict[str, Any], report_path: str) -> str:
    """The compact chat summary (the full report is the md file)."""
    target = state.get("target") or {}
    decision = state.get("decision") or {}
    metrics = state.get("metrics") or {}
    receipt = state.get("test_receipt") or {}
    lines = ["【session_reviewer 评审汇报】"]
    lines.append(f"- 目标: 会话 {state.get('session_id') or '?'} / "
                 f"pattern {target.get('pattern_code') or '?'} / "
                 f"应用 {target.get('app_name') or '未定位'}")
    if state.get("data_error"):
        lines.append(f"- ⚠ 取数失败: {state['data_error']}")
    else:
        lines.append(f"- 指标: {metrics.get('turns', 0)} 轮 · "
                     f"{metrics.get('messages_total', 0)} 消息 · "
                     f"turn_error {metrics.get('turn_errors', 0)} · "
                     f"工具 {metrics.get('tool_calls', 0)}"
                     f"（失败 {metrics.get('tool_failures', 0)}） · "
                     f"clarify {metrics.get('clarify_total', 0)}")
    if state.get("review_degraded"):
        lines.append(f"- 评审: 降级（{state.get('review_summary', '')[:120]}）")
    elif state.get("suggestions"):
        sev = Counter(s["severity"] for s in state["suggestions"])
        lines.append(f"- 评审: {state.get('review_summary', '')[:160]}"
                     f"（建议 {len(state['suggestions'])} 条: "
                     f"高 {sev.get('high', 0)} / 中 {sev.get('medium', 0)} / "
                     f"低 {sev.get('low', 0)}）")
    else:
        lines.append("- 评审: 无建议（未发现问题或输出为空）")
    if decision.get("mode"):
        lines.append(f"- 人工闸: {'通过' if decision.get('approved') else '拒绝'}"
                     f"（{decision.get('mode')}）")
    if state.get("applied_files"):
        outcome = "（已回滚）" if state.get("rolled_back") else ""
        lines.append(f"- 实施: 已修改 {', '.join(state['applied_files'])}{outcome}")
    if receipt.get("skipped"):
        lines.append(f"- 测试: 跳过（{receipt.get('reason')}）")
    elif receipt:
        lines.append(f"- 测试: {'通过' if receipt.get('ok') else '失败'}"
                     f"（exit={receipt.get('exit_code')}）")
    reload_receipt = state.get("reload_receipt") or {}
    if reload_receipt.get("ok"):
        lines.append("- 热加载: 已触发（新会话生效）")
    elif not reload_receipt.get("skipped"):
        lines.append(f"- 热加载: 失败（{str(reload_receipt.get('error'))[:120]}，"
                     "请手动 POST /api/v1/reload）")
    if state.get("fix_history"):
        lines.append(f"- 自修: {len(state['fix_history'])} 轮"
                     + ("，耗尽后已回滚" if state.get("rolled_back") else ""))
    if report_path:
        lines.append(f"- 报告: {report_path}")
    stale = _template_stale_note(state)
    if stale:
        lines.append(f"- 备注: {stale}")
    return "\n".join(lines)


# ============================================================================
# Plugin registration — import side effect at the bottom (route.py imports
# this module at its end to complete registration)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", SR_ROUTE_CODE, SrRouteExecutor)
plugin_registry.register("executor", SR_COLLECT_CODE, SrCollectExecutor)
plugin_registry.register("executor", SR_METRICS_CODE, SrMetricsExecutor)
plugin_registry.register("executor", SR_REVIEW_CODE, SrReviewExecutor)
plugin_registry.register("executor", SR_WAIT_CODE, SrWaitHumanExecutor)
plugin_registry.register("executor", SR_APPLY_CODE, SrApplyExecutor)
plugin_registry.register("executor", SR_FIXLOOP_CODE, SrFixloopExecutor)
plugin_registry.register("executor", SR_REPORT_CODE, SrReportExecutor)
