"""The nine-station graph executors of the archify pattern — one class per
node (plugin code = node code; route.py's nodes bind via plugins={"loop": ...}):

    af_route ──> af_author ──┬───────────────> af_validate ──┬─> af_deliver
     type routing   artifact-first writing │              ↑  │       │    │
                    │         │             repair loop│  │ fail  │    │
                    v         │                   │  v        v    v
             af_update_probe  │              af_repair ──> af_visual_check
              update probe (side branch)──┘       │              │
                                                │ 5 stale rounds   │ shot review
                                                │ (honest exit)    v
                                                +──────> af_percept ──> af_report
                                                 three-level separation   image review   report (is_end)

Station inventory (the skill's discipline translated to graph stations):

    ROUTE      one tool-less LLM call → {diagram_type, is_mermaid,
               output_name} JSON (fault-tolerant extraction + one
               self-correct retry; final failure degrades to workflow,
               marked degraded); initializes the run state board
    AUTHOR     a bounded tool-carrying workspace (read_text the one
               matching schema + common + example, then write_text the
               candidate — artifact first); ≤ author-rounds budget
               (default 10, app config bag author_rounds); a missing/
               unparseable candidate is NOT fabricated — the honest path
               goes to VALIDATE, which records it as an objective error
               for the repair loop; the closing reply is kept as
               state.design_notes — the repair station's only memory of
               the authoring intent
    UPDATE_PROBE  deterministic, no LLM: runs the packaged check-update
               once after the first candidate (probe_done latch); silent
               → no notice; update_available → the compact fixed-local
               notice lands in state (information, never permission) and
               the eventKey is acked best-effort
    VALIDATE   deterministic, no LLM: `validate --quality showcase --json`
               via the bash tool; showcase acceptance = ok AND exactly 9
               checks AND all pass AND no warnings → frozen latch, on to
               DELIVER; otherwise the objective error count appends to
               val_history and the run goes to REPAIR
    REPAIR     convergence gate FIRST (deterministic, never
               model-decided): two consecutive validate rounds without a
               new minimum → honest exit to REPORT with the unresolved
               diagnostics; else a zero-LLM label-clearance solver runs
               (component-overlap suggested points + label-route-clearance
               four-way nudges from the diagnostic geometry; each candidate
               is verified by a real validate run, kept only on strict
               improvement, byte-rolled-back otherwise; failed moves are
               remembered across visits) — reaching showcase acceptance
               short-circuits straight back to VALIDATE; otherwise ONE
               focused LLM round with read_text/edit_file/bash — the graph
               itself is the loop. The framing carries the original
               request, the author's design memo, the repair log + error
               trajectory, and the type's placement discipline (the skill
               repairs inside the authoring conversation; the graph splits
               author/repair into two amnesiac workspaces — the state
               board bridges them). Every LLM round streams its thinking
               to the UI via ec.stream (forward_text=False: station text
               is protocol / work chatter, never the user-visible reply)
    DELIVER    deterministic: `deliver ... --json`; success → VISUAL_CHECK;
               failure → the declared bail-out edge straight to REPORT
               (a failed delivery preserves the previous output — the
               visual-check path must never run on it)
    VISUAL_CHECK  deterministic: `visual-check --json` evidence collection
               without touching the delivered HTML; any outcome (pass /
               fail / environment error) travels to REPORT truthfully
    PERCEPT    one tool-less multimodal LLM call: an image-capable
               reviewer judges the delivered artifact from the
               visual-check PNG sidecars (both themes × viewports) against
               the skill's perceptual checklist; verdicts come only from
               the attached screenshots and every degradation (no
               evidence, non-vision reviewer, unparseable output,
               transport error) is an honest skipped receipt — a pass is
               never fabricated, and the verdict never overwrites the
               deterministic tiers
    REPORT     deterministic, no LLM: the final report is ASSEMBLED from
               the collected receipts, never model prose — deliver proves
               artifact checks, visual-check proves bounded browser
               behavior, perceptual review is the percept station's
               receipt (passed / failed / skipped), the three tiers
               stated separately

Runaway protection (three independent dimensions, mirroring the skill's
own budget): graph steps (route.py config.max_steps=20; app yaml
loop.max_steps may override) × repair visits (stale-5 honest exit,
semantic) × per-visit LLM rounds (author ≤10, repair ≤3 — the macro
repair loop lives in the graph, not the executor; these budgets read the
app config bag over the code defaults, see _runtime_settings).

Inter-station state travels via ``cxt.graph_state["archify_state"]``
(cleared by the runtime at graph termination); the final trace goes to
``cxt.metadata["archify"]`` — consumed by the engine at turn end (picked
into the persisted trace trail as an ``app_trace`` event, key popped) and
returned via ``TurnResult.extra`` for same-turn observability.
"""

import json
import logging
import re
import shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from atoms.executors.loop_executor import _emit_round, _stream_round

from nexus.engine.agent_hooks import (
    AgentEndEvent,
    AgentStartEvent,
    LLMCallEvent,
    LLMResponseEvent,
    ToolCallEvent,
    ToolResultEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
    rewrite_tool_call,
    rewrite_tool_result,
)
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.loop import TurnResult, _execute_tool, _parse_args, _resolve_tools
from nexus.engine.messages import build_agent_messages
from nexus.engine.tool_context import tool_call_context
from nexus.llm.resolve import build_provider
from nexus.llm.vision import multimodal_user_content, vision_status
from nexus.settings import get_pattern_custom_config
from apps.archify_agent.prompts import (
    ARCHIFY_BASE_PROMPT,
    AUTHOR_ANCHOR,
    AUTHOR_PHASE_TMPL,
    PERCEPT_PHASE_TMPL,
    PERCEPT_RETRY_PROMPT,
    PLACEMENT_HINTS,
    REPAIR_ANCHOR,
    REPAIR_PHASE_TMPL,
    ROUTE_ANCHOR,
    ROUTE_PHASE_PROMPT,
    ROUTE_RETRY_PROMPT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Budgets / latches
# ---------------------------------------------------------------------------

# Code defaults: the app config bag (the config: section of
# apps/archify_agent/config.yaml, via get_pattern_custom_config("archify"))
# overrides by the same keys; no bound app config → empty bag, everything
# takes these defaults (offline tests are exactly this shape).
_DEFAULT_AUTHOR_ROUNDS = 10  # af_author tool-round cap (find an exemplar +
                             # read schema ×3 + write the candidate + wrap
                             # up + one retry; a studio live run proved 6
                             # rounds have zero headroom once find_files
                             # joined — one stumble exhausts them)
_DEFAULT_REPAIR_ROUNDS = 3   # af_repair's internal micro-loop round cap per
                             # visit (edit → self-validate → edit again; the
                             # in-station convergence headroom beyond the
                             # graph-level macro loop; stale-5 stays the
                             # outer guard)
_DEFAULT_ROUTE_RETRIES = 1   # af_route JSON parse-failure self-correction retries
_DEFAULT_STALE_LIMIT = 5     # rounds with the error count never refreshed below the floor → honest exit
_DEFAULT_PERCEPT_RETRIES = 1  # af_percept verdict JSON parse-failure self-correction retries
_PERCEPT_MAX_SHOTS = 8        # cap on screenshots attached for perceptual review (visual-check
                              # ships 4 by default; a defensive ceiling; any
                              # overflow is honestly flagged as unattached
                              # in the prompt)
_CANDIDATE_SNIPPET_CHARS = 20000  # cap on the candidate content embedded in the repair prompt (over-limit
                              # head+tail truncation, explicitly flagged — the
                              # old 8000 cut whole middle sections of
                              # connections, leaving the repair station
                              # "blind-patching" a maimed candidate; a typical
                              # 12-node candidate is 10-20K, so 20000 covers
                              # most full pictures)
_DIAGNOSTIC_CHARS = 2400     # per-diagnostic truncation cap entering the prompt (the "Suggested fix"
                              # usually sits at the tail of a long message; over-trimming cuts the answer off)


def _runtime_settings() -> Dict[str, Any]:
    """The single read point for budgets / deployment paths: the app config bag overrides the code defaults.

    The four round budgets + skill_dir / workspace_root all resolve through
    this function — stations must not sprinkle get_pattern_custom_config.
    int keys require ≥ 1 (a broken yaml value → warn + default, the same
    stance as global config); missing path keys fall back to the module
    default constants (tests patch by monkeypatching _DEFAULT_*)."""
    bag = get_pattern_custom_config("archify")

    def limit(key: str, default: int) -> int:
        raw = bag.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = -1
        if isinstance(raw, bool) or value < 1:
            logger.warning("[archify] config.%s 应为 int ≥ 1,实际为 %r,"
                           "按默认 %r 处理", key, raw, default)
            return default
        return value

    return {
        "author_rounds": limit("author_rounds", _DEFAULT_AUTHOR_ROUNDS),
        "repair_rounds": limit("repair_rounds", _DEFAULT_REPAIR_ROUNDS),
        "route_retries": limit("route_retries", _DEFAULT_ROUTE_RETRIES),
        "stale_limit": limit("stale_limit", _DEFAULT_STALE_LIMIT),
        "percept_retries": limit("percept_retries", _DEFAULT_PERCEPT_RETRIES),
        "skill_dir": str(bag.get("skill_dir") or _DEFAULT_SKILL_DIR),
        "workspace_root": str(bag.get("workspace_root")
                              or _DEFAULT_WORKSPACE_ROOT),
        # Absolutize: the CLI runs under skill_dir, so a relative root would resolve
# into the skill directory instead of the repo
        "repo_root": str(_absolutize(str(bag.get("repo_root")
                                         or _DEFAULT_REPO_ROOT))),
    }


# ---------------------------------------------------------------------------
# Station codes (= plugin codes; route.py binds accordingly)
# ---------------------------------------------------------------------------

AF_ROUTE_CODE = "af_route"
AF_AUTHOR_CODE = "af_author"
AF_PROBE_CODE = "af_update_probe"
AF_VALIDATE_CODE = "af_validate"
AF_REPAIR_CODE = "af_repair"
AF_DELIVER_CODE = "af_deliver"
AF_VISUAL_CODE = "af_visual_check"
AF_PERCEPT_CODE = "af_percept"
AF_REPORT_CODE = "af_report"

DIAGRAM_TYPES = ("architecture", "workflow", "sequence", "dataflow", "lifecycle")

# In-flight run state / final trace keys (same placement as deep_research)
_STATE_KEY = "archify_state"
_TRACE_KEY = "archify"

# Deployment defaults (overridable via the app config bag: skill_dir /
# workspace_root / repo_root — apps/archify_agent/config.yaml's config:
# section; the pattern.config free-dict no longer carries them, see
# _runtime_settings)
# Defaults to the skill copy shipped with this repo (skills/archify, the
# same source as archify_skill_agent); standalone deployments override it
# via the app config bag's skill_dir. ~/.claude/... exists only on the
# author's machine and cannot be the out-of-box default.
_DEFAULT_SKILL_DIR = "skills/archify"
_DEFAULT_WORKSPACE_ROOT = "data/archify"
# Repo-evidence verification root (the archify CLI's --repo-root, appended
# to the command only when an architecture declares sources/meta.repository):
# defaults to the service startup directory — the host usually runs at the
# root of the repo being documented; deployments elsewhere override it via
# the app config bag's repo_root
_DEFAULT_REPO_ROOT = str(Path.cwd())

_FORCE_CLOSE_REPLY = "(图表工程流程被步数预算截断,未能完成;已产出的回执见汇报,未完成步骤如实标注。)"


# ============================================================================
# State board helpers
# ============================================================================

def _new_state(cxt, request: str, skill_dir: str) -> Dict[str, Any]:
    """Fresh run state (af_route initializes; per-session workspace).

    The workspace first falls back to an absolute path under the default
    root; each station then recomputes it via _safe_workspace_dir with
    overrides from the app config bag (both entry points guarantee an
    absolute path)."""
    safe_session = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cxt.session_id)) or "s"
    workspace = str(_absolutize(_DEFAULT_WORKSPACE_ROOT) / safe_session)
    return {
        "request": request,
        "skill_dir": skill_dir,
        "workspace": workspace,
        "diagram_type": "workflow",
        "is_mermaid": False,
        "output_name": "diagram",
        "candidate_path": "",
        "output_html": "",
        "probe_done": False,
        "update_notice": "",
        "val_history": [],       # objective error count per validate visit
        "last_receipt": {},      # latest validation receipt summary (diagnostics feed repair)
        "design_notes": "",      # author station's closing design memo (repair station's authoring context)
        "repair_log": [],        # per-repair-visit action summary (prevents replaying failed moves)
        "solver_tried": [],      # solver's failed-nudge keys across visits (prevents replaying failed geometry)
        "frozen": False,
        "repair_rounds": 0,
        "author_rounds": 0,
        "best_checkpoint": {},   # candidate-bytes + receipt snapshot refreshed at each new error minimum (regression guard)
        "deliver_failed": False,
        "deliver_receipt": {},
        "visual_receipt": {},
        "percept_receipt": {},
        "honest_exit": False,
        "phases": [],
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


def _skill_dir(ec) -> str:
    declared = _runtime_settings()["skill_dir"]
    return str(_absolutize(str(declared or _DEFAULT_SKILL_DIR)))


def _absolutize(raw: str) -> Path:
    """Pin a declared root to an absolute path (relative roots resolve
    against the service startup directory, matching the file tools'
    relative-path semantics).

    Paths in the state board must be absolute: the file tools
    (write_text/edit_file) resolve relative paths against the service
    startup directory, while the archify CLI runs via bash with
    workdir=skill_dir and resolves against the skill directory — the same
    relative string resolves to different files in the two contexts, so the
    validate station would ENOENT and the repair station would edit a file
    the validator never sees (hit in a real studio run).
    """
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def _workspace_root(ec) -> Path:
    declared = _runtime_settings()["workspace_root"]
    return _absolutize(str(declared or _DEFAULT_WORKSPACE_ROOT))


# ============================================================================
# Receipt helpers (bash tool → archify CLI receipt)
# ============================================================================

@contextmanager
def _station_tool_context(ec):
    """Publish the station's position around raw ``_execute_tool`` dispatches.

    The custom stations bypass the default loop executor, which normally
    publishes this context — without it their tool calls run detached and
    the guardrail handlers can only fall back to the global sections.
    ``pattern_code`` is the load-bearing field here: it keys the app
    guardrails overlay (apps/archify_agent/config.yaml ``guardrails:``,
    e.g. the shell 120s loosening for the archify CLI calls)."""
    with tool_call_context(
        (getattr(ec.cxt, "llm_config", None) or {}),
        getattr(ec.pattern, "allow_toolset", None) or [],
        session_id=getattr(ec.cxt, "session_id", "") or "",
        pattern_code=getattr(ec.pattern, "code", "") or "",
    ):
        yield


async def _run_cli(command: str, workdir: str, ec) -> Dict[str, Any]:
    """Run one archify CLI command through the bash tool and parse the
    machine-readable receipt off stdout.

    Returns the parsed receipt dict; every failure mode (tool error,
    non-JSON stdout, parse error) degrades to an honest
    ``{"ok": False, "error": ...}`` receipt — never a fabricated pass.
    """
    with _station_tool_context(ec):
        raw = await _execute_tool(
            "bash", {"command": command, "workdir": workdir})
    try:
        shell = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        shell = {"error": f"bash 工具返回无法解析: {str(raw)[:200]}"}
    if "error" in shell:
        return {"ok": False, "error": f"bash 工具执行失败: {shell['error']}"}
    stdout = str(shell.get("stdout") or "")
    try:
        receipt = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {
            "ok": False,
            "error": (f"退出码 {shell.get('exit_code')},stdout 非_JSON:"
                      f" {stdout[:300]}"),
            "exit_code": shell.get("exit_code"),
            "stderr": str(shell.get("stderr") or "")[:300],
        }
    if not isinstance(receipt, dict):
        return {"ok": False, "error": f"回执顶层不是对象: {str(receipt)[:200]}"}
    receipt.setdefault("exit_code", shell.get("exit_code"))
    return receipt


def _repo_root_flag(state: Dict[str, Any]) -> str:
    """Assemble ``--repo-root``: pass it only for architecture when the
    candidate declares repository evidence (meta.repository or any
    component's sources).

    The CLI rejects the flag for non-architecture, and the verifier skips
    it when no evidence is declared, so it is conditioned on candidate
    content; without it, candidates declaring evidence deadlock at
    repository-evidence/root-required (the repair station can't fix that by
    editing JSON — it is a command-flag problem). With it passed, the
    verifier adjudicates url/revision/file line numbers against the local
    checkout with real git, so the resulting diagnostics (origin-mismatch /
    file-missing etc.) all carry supportedFixes and can actually be repaired
    by the repair loop."""
    if state.get("diagram_type") != "architecture":
        return ""
    try:
        data = json.loads(
            Path(state.get("candidate_path") or "").read_text(
                encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return ""  # candidate missing/corrupt: validate records it honestly; don't jump in here
    if not isinstance(data, dict):
        return ""
    has_evidence = bool((data.get("meta") or {}).get("repository"))
    components = data.get("components")
    if isinstance(components, list):
        has_evidence = has_evidence or any(
            isinstance(c, dict) and c.get("sources") for c in components)
    if not has_evidence:
        return ""
    root = _runtime_settings()["repo_root"]
    return f" --repo-root {shlex.quote(root)}" if root else ""


def _receipt_error_count(receipt: Dict[str, Any]) -> int:
    """The objective error metric driving the stale rule (relative
    comparison only — monotone-ish is enough: 0 on ok, else a count of
    concrete problems, floor 1)."""
    if receipt.get("ok"):
        return 0
    diags = [d for d in (receipt.get("diagnostics") or [])
             if isinstance(d, dict)
             and d.get("severity") not in ("warning", "info")]
    if diags:
        return len(diags)
    checks = [c for c in (receipt.get("checks") or [])
              if isinstance(c, dict) and not c.get("ok")]
    if checks:
        return len(checks)
    return 1


def _is_showcase_pass(receipt: Dict[str, Any]) -> bool:
    """Showcase acceptance = ok AND all 9 artifact checks pass AND no
    warnings (a 4-check receipt is basic validation, never acceptance)."""
    checks = [c for c in (receipt.get("checks") or []) if isinstance(c, dict)]
    return (receipt.get("ok") is True
            and len(checks) == 9
            and all(c.get("ok") for c in checks)
            and not (receipt.get("warnings") or []))


def _receipt_summary(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """The storable summary for state/trace (full receipts can be huge).

    Diagnostics keep the four elements the skill's repair contract names:
    stable code, precise subject, supportedFixes (the message carries the
    evidence text), structured evidence — the original implementation kept
    only the message, leaving the repair station fixing with a "symptom
    description" while the "prescription" was lost; evidence is the
    label-clearance solver's precise geometry source
    (labelRect/segments/minimumPx)."""
    return {
        "ok": bool(receipt.get("ok")),
        "error": str(receipt.get("error") or "")[:300],
        "checks_total": len([c for c in (receipt.get("checks") or [])
                             if isinstance(c, dict)]),
        "warnings": len(receipt.get("warnings") or []),
        "diagnostics": [
            {"code": str(d.get("code", "")),
             "severity": str(d.get("severity", "error")),
             "message": str(d.get("message", ""))[:_DIAGNOSTIC_CHARS],
             "subject": (d["subject"]
                         if isinstance(d.get("subject"), (str, dict)) else ""),
             "supported_fixes": [str(f)[:200]
                                 for f in (d.get("supportedFixes") or [])
                                 if f][:4],
             "evidence": _bounded_evidence(d.get("evidence"))}
            for d in (receipt.get("diagnostics") or [])
            if isinstance(d, dict)
        ][:12],
        "exit_code": receipt.get("exit_code"),
    }


def _bounded_evidence(ev: Any) -> Any:
    """The diagnostic's structured evidence goes into the summary as-is (over-length / non-serializable degrades to an empty string).

    evidence is the precise geometry source for the solver and repair
    prompts; the message-text regex backstop only kicks in when evidence is
    missing — it is not an equivalent substitute."""
    if not isinstance(ev, dict) or not ev:
        return ""
    try:
        text = json.dumps(ev, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return ev if len(text) <= 2400 else ""


def _trailing_stale(history: List[int]) -> int:
    """Consecutive trailing validate rounds that failed to set a new
    minimum (the skill's five-round stop rule, computed never guessed;
    the first entry is the baseline and never counts as stale):

        [5] → 0;  [5,5] → 1;  [5]*6 → 5 (stop);
        [5,3,4] → 1 (4 ≥ min(5,3));  [5,3,4,2] → 0 (new minimum resets)
    """
    stale = 0
    seen: List[int] = []
    for e in history:
        if seen and e >= min(seen):
            stale += 1
        else:
            stale = 0
        seen.append(e)
    return stale


_SUGGESTED_POINT = re.compile(
    r'labelAt \[(-?[0-9]+(?:\.[0-9]+)?), (-?[0-9]+(?:\.[0-9]+)?)\]')
_OVERLAP_LABEL = re.compile(r'Label "([^"]+)" overlaps component')
_CLEAR_RECT = re.compile(
    r'label rect \[(-?[0-9.]+), (-?[0-9.]+), (-?[0-9.]+), (-?[0-9.]+)\]')
_CLEAR_SEGMENT = re.compile(
    r'segment \d+ \[(-?[0-9.]+), (-?[0-9.]+)\] -> \[(-?[0-9.]+), (-?[0-9.]+)\]')
_CLEAR_MINIMUM = re.compile(r'minimum (\d+)px')

# Solver budgets (code constants, not in the app config bag: internal safety bounds, not deployment tuning)
_SOLVER_PAD = 2.0         # extra safety margin beyond minimumPx
_SOLVER_MAX_NUDGE = 60.0  # per-move nudge cap — a bigger move = the label flying off its own edge;
                          # that is a layout-level problem, left to the LLM's row/col/pos levers
_SOLVER_MAX_TRIALS = 8    # validate runs the solver may consume per repair visit, at most
_SOLVER_MAX_LABELS = 4    # label diagnostics handled per visit, at most


def _clearance_moves(rect: Tuple[float, float, float, float],
                     seg: Tuple[float, float, float, float],
                     minpx: float) -> List[Dict[str, float]]:
    """Four-way nudge candidates for label-route-clearance (ascending by
    |delta| — nearest move first).

    Vertical segment: shift left/right to clear the segment's x, or up/down
    out of the segment's y span; horizontal segments are symmetric. Each
    direction computes the smallest delta that clears exactly minpx+pad.
    Why ascending: the smallest move is least likely to spark new
    collisions (in that 48px label squeeze from the studio run, the correct
    +12px solution happened to come first). Moves beyond _SOLVER_MAX_NUDGE
    are dropped."""
    x, y, w, h = rect
    x0, y0, x1, y1 = seg
    clear = minpx + _SOLVER_PAD
    if x0 == x1:
        raw = [("dx", (x0 - clear) - (x + w)),
               ("dx", (x0 + clear) - x),
               ("dy", (min(y0, y1) - clear) - (y + h)),
               ("dy", (max(y0, y1) + clear) - y)]
    else:
        raw = [("dy", (y0 - clear) - (y + h)),
               ("dy", (y0 + clear) - y),
               ("dx", (min(x0, x1) - clear) - (x + w)),
               ("dx", (max(x0, x1) + clear) - x)]
    return [{axis: round(delta, 1)} for axis, delta
            in sorted(raw, key=lambda t: abs(t[1]))
            if 1 <= abs(delta) <= _SOLVER_MAX_NUDGE]


def _label_rect_from(ev: Dict[str, Any],
                     msg: str) -> Optional[Tuple[float, float, float, float]]:
    rect = ev.get("labelRect")
    if isinstance(rect, dict):
        try:
            return (float(rect["x"]), float(rect["y"]),
                    float(rect["width"]), float(rect["height"]))
        except (KeyError, TypeError, ValueError):
            pass
    m = _CLEAR_RECT.search(msg)
    if m:
        return tuple(float(v) for v in m.groups())  # type: ignore[return-value]
    return None


def _segment_from(ev: Dict[str, Any],
                  msg: str) -> Optional[Tuple[float, float, float, float]]:
    src_from, src_to = ev.get("from"), ev.get("to")
    if isinstance(src_from, (list, tuple)) and isinstance(src_to, (list, tuple)):
        try:
            return (float(src_from[0]), float(src_from[1]),
                    float(src_to[0]), float(src_to[1]))
        except (TypeError, ValueError, IndexError):
            pass
    m = _CLEAR_SEGMENT.search(msg)
    if m:
        return tuple(float(v) for v in m.groups())  # type: ignore[return-value]
    return None


def _subject_conn_index(subject: Dict[str, Any],
                        ev: Dict[str, Any]) -> Optional[int]:
    for raw in (subject.get("index"),
                (ev.get("labelRect") or {}).get("relationIndex")
                if isinstance(ev.get("labelRect"), dict) else None):
        if isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _label_solver_targets(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract solver-handleable label diagnostics from the latest receipt, each with its move candidates.

    Two shapes (everything else belongs to the LLM micro-loop):
    1. composition/label-route-clearance (shared by architecture/workflow/
       dataflow/lifecycle): structured evidence (labelRect / segment /
       minimumPx) computes four-direction moves, with a message-text regex
       backstop;
    2. layout/constraint's "Label 'X' overlaps component": the renderer
       Suggested fix's below/above labelAt absolute points, validated in the
       suggested order.
    """
    diags = (state.get("last_receipt") or {}).get("diagnostics") or []
    targets: List[Dict[str, Any]] = []
    for d in diags:
        if not isinstance(d, dict):
            continue
        code = str(d.get("code") or "")
        msg = str(d.get("message") or "")
        ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
        subject = (d.get("subject")
                   if isinstance(d.get("subject"), dict) else {})
        if code == "composition/label-route-clearance":
            rect = _label_rect_from(ev, msg)
            seg = _segment_from(ev, msg)
            minpx = ev.get("minimumPx")
            if minpx is None:
                mm = _CLEAR_MINIMUM.search(msg)
                minpx = int(mm.group(1)) if mm else 4
            if not (rect and seg):
                continue
            rect_ev = ev.get("labelRect")
            label = str((rect_ev or {}).get("label") if
                        isinstance(rect_ev, dict) else
                        ev.get("label") or subject.get("label") or "")
            moves = _clearance_moves(rect, seg, float(minpx))
            if moves:
                targets.append({"code": code,
                                "index": _subject_conn_index(subject, ev),
                                "label": label, "moves": moves})
        elif "overlaps component" in msg:
            lm = _OVERLAP_LABEL.search(msg)
            if not lm:
                continue
            points = [(float(px), float(py)) for px, py in
                      _SUGGESTED_POINT.findall(msg.split("Suggested fix")[-1])]
            if not points:
                continue
            targets.append({"code": code,
                            "index": _subject_conn_index(subject, ev),
                            "label": lm.group(1),
                            "moves": [{"abs": [px, py]} for px, py in points]})
    return targets[:_SOLVER_MAX_LABELS]


def _resolve_conn(conns: List[Any],
                  target: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    index = target.get("index")
    if isinstance(index, int) and 0 <= index < len(conns):
        conn = conns[index]
        if isinstance(conn, dict):
            return conn
    label = str(target.get("label") or "")
    if label:
        for conn in conns:
            if isinstance(conn, dict) and conn.get("label") == label:
                return conn
    return None


def _apply_label_move(conns: List[Any], target: Dict[str, Any],
                      move: Dict[str, Any]) -> bool:
    """Land one move onto the target connection: an absolute point writes labelAt; an increment folds into an
    existing labelAt first, otherwise accumulates labelDx/labelDy (the same
    semantics as the validator's supportedFixes).

    Success is adjudicated by the caller running the real validator; this
    only does the mechanical placement."""
    conn = _resolve_conn(conns, target)
    if conn is None:
        return False
    if "abs" in move:
        conn["labelAt"] = [float(move["abs"][0]), float(move["abs"][1])]
        return True
    if isinstance(conn.get("labelAt"), list) and len(conn["labelAt"]) == 2:
        try:
            if "dx" in move:
                conn["labelAt"][0] = round(
                    float(conn["labelAt"][0]) + move["dx"], 1)
            if "dy" in move:
                conn["labelAt"][1] = round(
                    float(conn["labelAt"][1]) + move["dy"], 1)
            return True
        except (TypeError, ValueError):
            return False
    if "dx" in move:
        conn["labelDx"] = round(float(conn.get("labelDx", 0)) + move["dx"], 1)
    if "dy" in move:
        conn["labelDy"] = round(float(conn.get("labelDy", 0)) + move["dy"], 1)
    return True


def _solver_move_key(target: Dict[str, Any], move: Dict[str, Any]) -> str:
    anchor = target.get("index")
    anchor = anchor if isinstance(anchor, int) else (target.get("label") or "?")
    detail = ("at=%s" % move["abs"] if "abs" in move else
              ",".join(f"{k}={v}" for k, v in sorted(move.items())))
    return f"{target['code']}#{anchor}|{detail}"


async def _solve_label_clearance(state: Dict[str, Any],
                                 ec) -> Tuple[str, bool]:
    """Zero-LLM deterministic label avoidance: compute moves from the diagnostic geometry, adjudicate with the real validator, roll back degradations.

    A studio live-run regression (label-route-clearance with no suggested
    coordinates): a 48px label squeezed between a component's right edge and
    a vertical route segment — six LLM rounds could not produce a
    pixel-level avoidance (moving left hit the component, spinning in place)
    until stale-5 exited honestly. Geometry belongs to tools: every candidate
    move is persisted then validated, kept only when the objective error
    count strictly decreases, otherwise rolled back byte-for-byte; failing
    moves across visits are recorded in solver_tried to prevent repeats.
    Returns (the summary entering the repair history, whether showcase
    acceptance is reached).
    """
    targets = _label_solver_targets(state)
    if not targets:
        return "", False
    cand = Path(state["candidate_path"])
    try:
        best_bytes = cand.read_text(encoding="utf-8")
        data = json.loads(best_bytes)
        if not isinstance(data, dict):
            return "", False
    except (json.JSONDecodeError, ValueError, OSError):
        return "", False  # candidate missing/corrupt: writing the candidate is the LLM micro-loop's duty
    rel = "connections" if "connections" in data else "edges"
    conns = data.get(rel)
    if not isinstance(conns, list):
        return "", False

    best_count = _receipt_error_count(state.get("last_receipt") or {})
    best_receipt: Dict[str, Any] = dict(state.get("last_receipt") or {})
    tried = state.setdefault("solver_tried", [])
    notes: List[str] = []
    trials = 0
    showcase = False
    where = state["candidate_path"]

    for target in targets:
        anchor = (f"{target['code']} {rel}[{target['index']}]"
                  if isinstance(target.get("index"), int) else
                  f"{target['code']} 标签\"{target['label']}\"")
        resolved = False
        for move in target["moves"]:
            if trials >= _SOLVER_MAX_TRIALS:
                break
            key = _solver_move_key(target, move)
            if key in tried:
                continue
            try:
                trial = json.loads(best_bytes)
            except (json.JSONDecodeError, ValueError):
                break
            trial_conns = trial.get(rel)
            if not isinstance(trial_conns, list) or \
                    not _apply_label_move(trial_conns, target, move):
                break
            try:
                cand.write_text(json.dumps(trial, ensure_ascii=False,
                                           indent=2), encoding="utf-8")
            except OSError:
                break
            receipt = await _run_cli(
                # Same-flag validation as the gate: for a candidate that
                # declares evidence but runs without --repo-root, the solver
                # would forever see root-required (1 error), judge "no
                # improvement" against the 1-error baseline → roll everything
                # back and log it, and geometrically solvable label-clearance
                # deadlock goes to the LLM (studio session f2cae679 live run: [1,18,16,1])
                "node bin/archify.mjs validate "
                f"{state['diagram_type']} {shlex.quote(str(where))} "
                f"--quality showcase{_repo_root_flag(state)} --json",
                state["skill_dir"], ec)
            trials += 1
            # Real-validation guard: a bash failure / corrupted receipt is
            # neither an improvement nor enters the history (guarding against
            # transient-fault poisoning — an error receipt with count=1
            # would masquerade as an "improvement" over a baseline of 3)
            real_validate = bool(receipt.get("diagnostics")
                                 or receipt.get("checks"))
            count = _receipt_error_count(receipt)
            if real_validate and count < best_count:
                best_bytes = cand.read_text(encoding="utf-8")
                best_count = count
                best_receipt = receipt
                applied = ", ".join(
                    f"labelAt={move['abs']}" if "abs" in move else
                    f"label{k.capitalize()} "
                    f"{'+' if v >= 0 else ''}{v:g}"
                    for k, v in move.items())
                notes.append(f"{anchor}: 应用 {applied}"
                             f"(客观错误 →{count})")
                resolved = True
                if _is_showcase_pass(receipt):
                    showcase = True
                break
            if real_validate:
                tried.append(key)
            try:
                cand.write_text(best_bytes, encoding="utf-8")  # rollback
            except OSError:
                break
        if not resolved:
            anchor_key = _solver_move_key(target, target["moves"][0]).split("|")[0]
            if any(key.startswith(anchor_key + "|") for key in tried):
                notes.append(f"{anchor}: 就近挪移均未更优(已记履历,交布局级杠杆)")

    if best_count < _receipt_error_count(state.get("last_receipt") or {}):
        state["last_receipt"] = _receipt_summary(best_receipt)
    tried[:] = tried[-64:]  # defensive cap (normally nowhere near reached)
    return "; ".join(notes), showcase


def _extract_json_object(content: str) -> Tuple[Dict[str, Any], str]:
    """First balanced ``{...}`` block → parse (fault-tolerant; the af_route
    protocol). Returns (obj, err) — obj empty on failure."""
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


def _ensure_candidate(state: Dict[str, Any],
                      writes: List[Dict[str, str]]) -> bool:
    """Candidate-landing adjudication + model path-drift adoption (a studio live-run regression).

    candidate_path itself wins; when missing/corrupt, scan this round's
    write_text calls in reverse for the last one whose content parses as a
    JSON object carrying diagram_type, and the executor pins it back to
    candidate_path — content belongs to the model, placement belongs to the
    executor (the model picking its own path to write the candidate is a
    real failure mode observed in live runs). Only when neither holds does
    this return False (honestly handing over to the repair loop).
    """
    cand = Path(state["candidate_path"])
    if cand.exists():
        try:
            if isinstance(json.loads(cand.read_text(encoding="utf-8")), dict):
                return True
        except (json.JSONDecodeError, ValueError, OSError):
            pass  # landed but corrupt → keep trying to adopt earlier valid writes
    for w in reversed(writes):
        try:
            args = json.loads(w.get("args") or "{}")
            content = str(args.get("content") or "")
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("diagram_type"):
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                cand.write_text(content, encoding="utf-8")
                logger.info("[archify] 创作写入路径漂移(%s),已收编至 %s",
                            args.get("path"), cand)
                return True
            except OSError as e:
                logger.warning("[archify] 收编候选写入失败: %s", e)
                return False
    return False


def _safe_workspace_dir(ec, state: Dict[str, Any]) -> None:
    """Workspace root honors the app config bag override (tests point the
    _DEFAULT_* constants at a tmp dir); idempotent."""
    root = _workspace_root(ec)
    safe_session = re.sub(r"[^A-Za-z0-9_-]+", "_", str(ec.cxt.session_id)) or "s"
    state["workspace"] = str(root / safe_session)


# ============================================================================
# Shared station helpers
# ============================================================================

async def _dispatch_tool_calls(messages: List[Dict[str, Any]],
                               tool_calls: List[Dict[str, Any]],
                               allowed_names: set, hooks, ec, round_idx: int
                               ) -> List[Dict[str, str]]:
    """Execute one LLM round's tool calls into the private workspace
    (same semantics as deep_research's dispatch, minus findings
    collection): P4 rewrite → allowed_names guard → _execute_tool → P5
    rewrite → append rows; returns [{name, args, result}] for callers
    (the author station detects the candidate write from it)."""
    cxt = ec.cxt
    node = ec.node
    _emit = getattr(ec.stream, "emit_trace", None)

    executed: List[Dict[str, str]] = []
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        call_id = tc.get("id", "")
        parsed_args = _parse_args(tc)

        # P4 chained rewrite (before the guard, same order as the kernel path)
        if hooks:
            event = ToolCallEvent(
                session_id=cxt.session_id, node_code=node.code,
                round_idx=round_idx, tool_name=name, args=parsed_args)
            name, parsed_args, _original = rewrite_tool_call(
                hooks, event, allowed_names)

        synthetic = False
        if name not in allowed_names:
            logger.warning("[archify] 工具 '%s' 不在本站可用集合,拦截不执行", name)
            synthetic = True
            result_content = json.dumps({
                "error": (f"工具 '{name}' 不存在或本站不可用。"
                          f"可用工具:{sorted(allowed_names)}。")
            }, ensure_ascii=False)
        else:
            if _emit is not None:
                _emit("tool_call", node_code=node.code, call_id=call_id,
                      tool_name=name, args=parsed_args, round_idx=round_idx)
            with _station_tool_context(ec):
                tool_result = await _execute_tool(name, parsed_args)
            if hooks:
                res_event = ToolResultEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=round_idx, tool_name=name,
                    tool_call_id=call_id, result=tool_result)
                tool_result, _orig = rewrite_tool_result(hooks, res_event)
            result_content = tool_result

        executed.append({"name": name, "args": json.dumps(
            parsed_args, ensure_ascii=False), "result": result_content})
        if _emit is not None:
            _emit("tool_result", node_code=node.code, call_id=call_id,
                  tool_name=name, result=result_content, round_idx=round_idx,
                  synthetic=synthetic)
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": result_content})
    return executed


def _force_close_result(state: Optional[Dict[str, Any]]) -> TurnResult:
    """Honest step-budget close (never claims success; partial receipts
    stay on the state board for the trace)."""
    return TurnResult(content=_FORCE_CLOSE_REPLY)


# ============================================================================
# The nine station executors
# ============================================================================

class AfRouteExecutor(NodeExecutor):
    """af_route: type routing (one tool-less LLM call, JSON protocol with
    self-correct retry, degraded fallback) + run-state initialization."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        pattern = ec.pattern
        if ec.force_close:
            return _force_close_result(None)

        hooks = resolve_agent_hooks(node, pattern)
        fragments = collect_fragments(
            hooks, AgentStartEvent(session_id=cxt.session_id,
                                   node_code=node.code, cxt=cxt),
        ) if hooks else []

        state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
        _safe_workspace_dir(ec, state)

        provider = build_provider(cxt.llm_config or {})
        llm_config = cxt.llm_config or {}
        messages = build_agent_messages(node, cxt, pattern=pattern,
                                        extra_blocks=fragments)
        messages.append({"role": "user", "content": ROUTE_PHASE_PROMPT})

        routing: Dict[str, Any] = {}
        route_retries = _runtime_settings()["route_retries"]
        for attempt in range(1 + route_retries):
            if hooks:
                fire(hooks, "on_llm_call", LLMCallEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=0, messages=messages,
                    model=llm_config.get("model", "")))
            result = await _stream_round(
                provider, messages, llm_config.get("model", "default"),
                llm_config.get("temperature", 0.7),
                llm_config.get("max_tokens", 2048), ec.stream,
                forward_text=False)  # routing is a JSON protocol: stream thinking only, never the body
            content = result.get("content", "") or ""
            if hooks:
                fire(hooks, "on_llm_response", LLMResponseEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=0, content=content, tool_calls=[]))
            routing, err = _extract_json_object(content)
            if routing.get("diagram_type") in DIAGRAM_TYPES:
                break
            routing = {}
            logger.warning("[archify] ROUTE JSON 解析失败(第 %d 次): %s",
                           attempt + 1, err)
            if attempt < route_retries:
                messages.append({"role": "assistant", "content": content or "(空输出)"})
                messages.append({"role": "user",
                                 "content": ROUTE_RETRY_PROMPT.replace("{error}", err)})

        if not routing:
            routing = {"diagram_type": "workflow", "is_mermaid": False,
                       "output_name": "diagram",
                       "notes": "路由降级:解析失败,按 workflow 处理"}
            state["degraded"] = True

        state.update({
            "diagram_type": routing["diagram_type"],
            "is_mermaid": bool(routing.get("is_mermaid")),
            "output_name": re.sub(
                r"[^A-Za-z0-9_-]+", "-",
                str(routing.get("output_name") or "diagram")).strip("-")
            or "diagram",
        })
        state["candidate_path"] = str(
            Path(state["workspace"]) / f"{state['output_name']}.json")
        state["output_html"] = str(
            Path(state["workspace"]) / f"{state['output_name']}.html")
        state["phases"].append("route")
        _save_state(cxt, state)
        _emit_round(ec.stream, "route", 0)
        return TurnResult(content="", next=AF_AUTHOR_CODE)


class AfAuthorExecutor(NodeExecutor):
    """af_author: the bounded artifact-first workspace (read schema →
    write candidate). A missing candidate is never fabricated — VALIDATE
    will record it as an objective error and REPAIR may still write it."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return _force_close_result(None)
        state = _load_state(cxt)
        if state is None:  # defensive: no in-flight state, bail honestly
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)
            state["degraded"] = True

        node, pattern = ec.node, ec.pattern
        hooks = resolve_agent_hooks(node, pattern)
        tools = _resolve_tools(node, pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}

        dtype = state["diagram_type"]
        skill = Path(state["skill_dir"])
        framing = AUTHOR_PHASE_TMPL.format(
            request=state.get("request", ""),
            schema_path=skill / "schemas" / f"{dtype}.schema.json",
            common_schema_path=skill / "schemas" / "common.schema.json",
            example_path=skill / "examples",  # directory: model picks one matching example via find_files
            example_glob=dtype,               # the glob is the type name (e.g. *workflow*)
            candidate_path=state["candidate_path"],
        )
        workspace: List[Dict[str, Any]] = [
            {"role": "system", "content": ARCHIFY_BASE_PROMPT},
            {"role": "user", "content": framing},
        ]

        provider = build_provider(cxt.llm_config or {})
        llm_config = cxt.llm_config or {}
        writes: List[Dict[str, str]] = []
        closing = ""  # last non-empty reply (contractually the closing + design memo)

        for round_idx in range(_runtime_settings()["author_rounds"]):
            state["author_rounds"] = round_idx + 1
            if hooks:
                fire(hooks, "on_llm_call", LLMCallEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=round_idx, messages=workspace,
                    model=llm_config.get("model", "")))
            result = await _stream_round(
                provider, workspace, llm_config.get("model", "default"),
                llm_config.get("temperature", 0.7),
                llm_config.get("max_tokens", 2048), ec.stream, tools=tools,
                forward_text=False)  # intermediate work rounds: thinking on screen, body withheld
            content = result.get("content", "") or ""
            if content.strip():
                closing = content
            tool_calls = result.get("tool_calls", []) or []
            if hooks:
                fire(hooks, "on_llm_response", LLMResponseEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=round_idx, content=content,
                    tool_calls=tool_calls))
            workspace.append({"role": "assistant", "content": content,
                              "tool_calls": tool_calls} if tool_calls
                             else {"role": "assistant", "content": content})
            if not tool_calls:
                break
            executed = await _dispatch_tool_calls(
                workspace, tool_calls, allowed_names, hooks, ec, round_idx)
            writes.extend(e for e in executed if e.get("name") == "write_text")
            if state["candidate_path"] and Path(
                    state["candidate_path"]).exists():
                _emit_round(ec.stream, "author", round_idx)

        candidate_ok = _ensure_candidate(state, writes)
        # Carry the authoring context to the repair station: in the original
        # skill, repair happened in the same session (the model remembered
        # its own layout intent and label trade-offs); the graph recipe
        # splits authoring/repair into two amnesiac workspaces — this
        # closing memo is the bridge across the amnesia
        state["design_notes"] = closing.strip()[:600]
        state["phases"].append("author" if candidate_ok else "author_failed")
        _save_state(cxt, state)
        _emit_round(ec.stream, "author", state["author_rounds"])
        # The contract is "probe once, after the first candidate exists": an
        # unlanded candidate does not consume the probe step — go validate
        # directly
        nxt = AF_PROBE_CODE if (candidate_ok and not state["probe_done"]) \
            else AF_VALIDATE_CODE
        return TurnResult(content="", next=nxt)


class AfUpdateProbeExecutor(NodeExecutor):
    """af_update_probe: deterministic one-shot update awareness (silent →
    no notice; update_available → fixed-local notice + best-effort ack).
    Information, never permission."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return _force_close_result(None)
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)

        notice = ""
        skill = Path(state["skill_dir"])
        receipt = await _run_cli(
            "node scripts/check-update.mjs", str(skill), ec)
        status = str(receipt.get("status") or "")
        if receipt.get("ok") is False and not status:
            status = "silent"  # checker not runnable: the contract says continue and never mention it
        if status == "update_available":
            installed = str(receipt.get("installed") or receipt.get("current")
                            or "未知")
            latest = str(receipt.get("latest") or receipt.get("version") or "未知")
            link = (receipt.get("releaseNotesUrl") or receipt.get("release_url")
                    or receipt.get("link") or receipt.get("url") or "")
            severity = str(receipt.get("severity") or "")
            notice = (f"archify skill 有新版本可用(已装 {installed} → 最新 "
                      f"{latest}"+(f",发布说明:{link}" if link else "")+")。")
            if severity == "security":
                notice = "[安全更新] " + notice
            notice += "已安装的 Skill 保持不变,是否更新及何时更新由你决定。"
            event_key = str(receipt.get("eventKey")
                            or receipt.get("event_key") or "")
            # eventKey comes from a remote manifest receipt (untrusted
            # data); only a whitelist-validating value may enter a shell
            # command; on a mismatch skip the ack — an honest degradation
            # that never touches the main line
            if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", event_key):
                # Ack the eventKey after presenting it (best effort; the outcome does not affect the main line)
                await _run_cli(
                    f'node scripts/check-update.mjs '
                    f'--ack {shlex.quote(event_key)}',
                    str(skill), ec)

        state["probe_done"] = True
        state["update_notice"] = notice
        state["phases"].append("probe")
        _save_state(cxt, state)
        _emit_round(ec.stream, "probe", 0)
        return TurnResult(content="", next=AF_VALIDATE_CODE)


class AfValidateExecutor(NodeExecutor):
    """af_validate: the deterministic showcase gate. Pass → frozen latch +
    DELIVER; fail → objective error count appends to val_history → REPAIR."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return _force_close_result(_load_state(cxt))
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)

        if state.get("frozen"):
            # Already frozen (defensive re-entry): never re-validate
            return TurnResult(content="", next=AF_DELIVER_CODE)

        # candidate_path is filled by af_route; on a lost state board /
        # restore anomaly it may still be the initial empty string —
        # Path("") IS the cwd whose exists() is always true, so the empty
        # string must be blocked first
        candidate_path = str(state.get("candidate_path") or "")
        candidate = Path(candidate_path)
        if not candidate_path or not candidate.is_file():
            receipt = {
                "ok": False,
                "error": f"候选规范文件不存在: {candidate}",
                "diagnostics": [{
                    "code": "author/missing-candidate",
                    "severity": "error",
                    "message": (f"候选规范文件不存在: {candidate} ——"
                                f"创作站未能写出候选;修复站需先用 "
                                f"write_text 把完整候选规范写到上述绝对"
                                f"路径(diagram_type="
                                f"{state.get('diagram_type', '?')},"
                                f"不要用 bash 写文件)"),
                }],
            }
        else:
            receipt = await _run_cli(
                "node bin/archify.mjs validate "
                f"{state['diagram_type']} {shlex.quote(str(candidate))} "
                f"--quality showcase{_repo_root_flag(state)} --json",
                state["skill_dir"], ec)

        errors = _receipt_error_count(receipt)
        prev_min = min(state["val_history"]) if state["val_history"] else None
        state["val_history"].append(errors)
        state["last_receipt"] = _receipt_summary(receipt)

        # Best checkpoint: when the error count refreshes the historical
        # floor (or first / zeroed), snapshot the candidate bytes + receipt
        # summary — the rollback source of the repair station's regression
        # guard (LLM edits land unconditionally; only the gate validation is
        # the objective adjudication point)
        if errors == 0 or prev_min is None or errors < prev_min:
            try:
                state["best_checkpoint"] = {
                    "bytes": candidate.read_text(encoding="utf-8"),
                    "receipt": dict(state["last_receipt"]),
                }
            except OSError:
                pass  # candidate missing (the author/missing-candidate path): nothing to snapshot

        if errors == 0 and _is_showcase_pass(receipt):
            state["frozen"] = True
            state["phases"].append("validate_pass")
            nxt = AF_DELIVER_CODE
        else:
            state["phases"].append("validate_fail")
            nxt = AF_REPAIR_CODE
        _save_state(cxt, state)
        _emit_round(ec.stream, "validate", len(state["val_history"]))
        return TurnResult(content="", next=nxt)


class AfRepairExecutor(NodeExecutor):
    """af_repair: a deterministic regression guard first (the previous gate
    validate regressed beyond the best-known checkpoint → roll the
    candidate bytes + receipt back — the solver keeps only strict
    improvements, the LLM micro-loop's edits land unconditionally, so the
    checkpoint is the only objective safety net), then the convergence
    gate (deterministic stale-5 honest exit — never model-decided), then
    a bounded internal micro-loop (edit → self-validate via bash → edit,
    ≤ repair-rounds budget LLM rounds per visit, default 3 / app config
    bag repair_rounds); the graph itself remains the macro repair loop
    (stale-5 across visits is the outer guard)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return _force_close_result(_load_state(cxt))
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)

        limits = _runtime_settings()
        stale_limit = limits["stale_limit"]
        repair_rounds = limits["repair_rounds"]

        # ---- Regression guard (deterministic, before the convergence gate) ----
        # The solver has "keep only strictly better" byte rollback; the LLM
        # micro-loop does not — its edits land unconditionally. When the last
        # validation is worse than the historical best, roll back to the best
        # checkpoint (bytes + receipt summary) before repairing: a studio
        # live run observed the repair station taking a candidate from 1
        # error to 13 (val_history [1,1,1,13]); entering the next visit
        # wounded would compound the blind patching, and even the honest-exit
        # path would finish carrying the most-degraded candidate.
        hist = state.get("val_history") or []
        checkpoint = state.get("best_checkpoint") or {}
        if hist and hist[-1] > min(hist) and checkpoint.get("bytes"):
            try:
                Path(state["candidate_path"]).write_text(
                    str(checkpoint["bytes"]), encoding="utf-8")
                state["last_receipt"] = dict(checkpoint.get("receipt") or {})
                state["repair_log"].append({
                    "round": int(state.get("repair_rounds") or 0) + 1,
                    "summary": (f"上一轮验证回归({hist[-1]} 错 > 历史最优 "
                                f"{min(hist)} 错),已回滚到最优检查点字节"
                                "——从最优状态重修,勿重演上一轮动作"),
                })
                logger.warning("[archify] 验证回归(%s > %s),候选已回滚到"
                               "最优检查点", hist[-1], min(hist))
            except OSError as e:
                logger.warning("[archify] 回滚到最优检查点失败: %s", e)

        # ---- Convergence gate (before any LLM, deterministic) ----------------
        if _trailing_stale(state.get("val_history") or []) >= stale_limit:
            state["honest_exit"] = True
            state["phases"].append("repair_stopped")
            _save_state(cxt, state)
            logger.info(
                "[archify] 连续 %d 轮未刷新错误数下限(val_history=%s),"
                "诚实出口——带未解决诊断汇报",
                stale_limit, state["val_history"])
            _emit_round(ec.stream, "repair_stop", state["repair_rounds"])
            return TurnResult(content="", next=AF_REPORT_CODE)

        # ---- Deterministic label-avoidance solver (zero LLM, real-validator adjudication) ----
        # Both label-diagnosis shapes are rectangle-avoidance arithmetic, not
        # semantic judgment: component overlap (suggested below/above
        # coordinates) and label-route-clearance (four-direction nearest
        # moves from labelRect+segment evidence). The solver keeps only
        # strictly-better results, otherwise rolls the bytes back; zeroing
        # goes straight to the validation gate without spending an LLM round
        # (LLMs cannot fix pixel geometry — a studio live run spun for six
        # rounds until stale-5 exited honestly).
        solver_note = ""
        solver_passed = False
        try:
            solver_note, solver_passed = await _solve_label_clearance(state, ec)
        except Exception as e:  # the solver never blocks the repair main line
            logger.warning("[archify] 标签避让求解器异常(跳过): %s", e)
        if solver_note:
            logger.info("[archify] 标签避让求解器: %s", solver_note)

        if solver_passed:
            # The solver already reached showcase acceptance: no LLM workspace is opened — hand the verdict to the validation gate
            state["repair_rounds"] += 1
            state["repair_log"].append(
                {"round": state["repair_rounds"],
                 "summary": solver_note[:400]})
            state["phases"].append(f"repair_{state['repair_rounds']}")
            _save_state(cxt, state)
            _emit_round(ec.stream, "repair", state["repair_rounds"])
            return TurnResult(content="", next=AF_VALIDATE_CODE)

        # ---- Focused repair micro-loop (write_text/read_text/edit_file/bash) ------
        node, pattern = ec.node, ec.pattern
        hooks = resolve_agent_hooks(node, pattern)
        tools = _resolve_tools(node, pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}

        candidate = Path(state["candidate_path"])
        try:
            candidate_content = candidate.read_text(encoding="utf-8")
        except OSError:
            candidate_content = "(候选文件尚不存在——用 write_text 把完整候选写到下述绝对路径)"
        truncated = len(candidate_content) > _CANDIDATE_SNIPPET_CHARS
        if truncated:
            candidate_content = (
                candidate_content[:_CANDIDATE_SNIPPET_CHARS // 2]
                + "\n...[超长截断:以上只是头尾,动手前先 read_text 取全文]...\n"
                + candidate_content[-_CANDIDATE_SNIPPET_CHARS // 2:])

        # Authoring context + repair history (in the original skill repair and
        # authoring shared a session, so the model remembered its design
        # intent and tried moves; after the graph recipe split the stations,
        # the state board keeps those records on its behalf)
        dtype = state.get("diagram_type", "workflow")
        skill = Path(state["skill_dir"])
        notes = str(state.get("design_notes") or "").strip() or "(创作站未留备忘)"
        hist = state.get("val_history") or []
        stale = _trailing_stale(hist)
        hist_lines: List[str] = []
        if hist:
            tail = ("——已连续 %d 轮未刷新下限,本轮是最后机会,优先布局级杠杆"
                    % stale) if stale >= stale_limit - 1 else ""
            hist_lines.append(f"客观错误数轨迹: {hist}{tail}")
        for entry in state.get("repair_log") or []:
            hist_lines.append("第 %s 轮已试: %s" % (
                entry.get("round", "?"), str(entry.get("summary", ""))[:400]))
        if not hist_lines:
            hist_lines.append("(首轮修复,无历史)")

        framing = REPAIR_PHASE_TMPL.format(
            request=str(state.get("request", ""))[:1000],
            design_notes=notes,
            repair_history="\n".join(hist_lines),
            placement_hint=PLACEMENT_HINTS.get(dtype, ""),
            candidate_path=state["candidate_path"],
            candidate_content=candidate_content,
            diagram_type=dtype,
            schema_path=skill / "schemas" / f"{dtype}.schema.json",
            contract_path=skill / "references" / "authoring-contract.md",
            repo_root_flag=_repo_root_flag(state),
            diagnostics_json=json.dumps(
                state.get("last_receipt", {}).get("diagnostics") or [],
                ensure_ascii=False, indent=1),
        )
        workspace: List[Dict[str, Any]] = [
            {"role": "system", "content": ARCHIFY_BASE_PROMPT},
            {"role": "user", "content": framing},
        ]

        provider = build_provider(cxt.llm_config or {})
        llm_config = cxt.llm_config or {}
        writes: List[Dict[str, str]] = []
        closing = ""  # the last non-empty reply (contractually the "what was fixed" closing summary)

        for round_idx in range(repair_rounds):
            if hooks:
                fire(hooks, "on_llm_call", LLMCallEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=round_idx, messages=workspace,
                    model=llm_config.get("model", "")))
            result = await _stream_round(
                provider, workspace, llm_config.get("model", "default"),
                llm_config.get("temperature", 0.7),
                llm_config.get("max_tokens", 2048), ec.stream, tools=tools,
                forward_text=False)  # intermediate work rounds: thinking streams, body text does not
            content = result.get("content", "") or ""
            if content.strip():
                closing = content
            tool_calls = result.get("tool_calls", []) or []
            if hooks:
                fire(hooks, "on_llm_response", LLMResponseEvent(
                    session_id=cxt.session_id, node_code=node.code,
                    round_idx=round_idx, content=content,
                    tool_calls=tool_calls))
            workspace.append({"role": "assistant", "content": content,
                              "tool_calls": tool_calls} if tool_calls
                             else {"role": "assistant", "content": content})
            if not tool_calls:
                logger.info(
                    "[archify] 修复站第 %d/%d 轮无工具调用,本访收束",
                    round_idx + 1, repair_rounds)
                break
            executed = await _dispatch_tool_calls(
                workspace, tool_calls, allowed_names, hooks, ec, round_idx)
            writes.extend(e for e in executed
                          if e.get("name") == "write_text")

        # Missing-candidate case: the repair station rewrites the candidate
        # via write_text — run the same drift adoption
        if writes:
            _ensure_candidate(state, writes)

        state["repair_rounds"] += 1
        # The action history prevents replay on the next visit: the graph
        # recipe opens a fresh workspace per visit, and without this the
        # repair station would retry last visit's failed moves verbatim
        # (burning a whole round's budget); solver actions happen before
        # this visit's LLM rounds and share the same record
        summary = closing.strip()
        if solver_note:
            summary = f"{solver_note};{summary}" if summary else solver_note
        state["repair_log"].append(
            {"round": state["repair_rounds"], "summary": summary[:400]})
        state["phases"].append(f"repair_{state['repair_rounds']}")
        _save_state(cxt, state)
        _emit_round(ec.stream, "repair", state["repair_rounds"])
        return TurnResult(content="", next=AF_VALIDATE_CODE)


class AfDeliverExecutor(NodeExecutor):
    """af_deliver: deterministic one-shot final acceptance. Success →
    VISUAL_CHECK; failure → the declared bail-out edge straight to REPORT
    (visual-check must never run on a failed delivery's stale output)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return _force_close_result(_load_state(cxt))
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)

        receipt = await _run_cli(
            "node bin/archify.mjs deliver "
            f"{state['diagram_type']} {shlex.quote(str(state['candidate_path']))} "
            f"{shlex.quote(str(state['output_html']))} "
            f"--quality showcase{_repo_root_flag(state)} --json",
            state["skill_dir"], ec)
        state["deliver_receipt"] = _receipt_summary(receipt)

        if receipt.get("ok") is True:
            state["phases"].append("deliver")
            _save_state(cxt, state)
            _emit_round(ec.stream, "deliver", 0)
            return TurnResult(content="", next=AF_VISUAL_CODE)

        # A non-zero exit is never called a success: failure preserves truth and goes to the report station (no browser check)
        state["deliver_failed"] = True
        state["phases"].append("deliver_failed")
        _save_state(cxt, state)
        logger.warning("[archify] deliver 非零退出,按契约不运行 "
                       "visual-check,直接进入诚实汇报")
        _emit_round(ec.stream, "deliver", 0)
        return TurnResult(content="", next=AF_REPORT_CODE)


class AfVisualCheckExecutor(NodeExecutor):
    """af_visual_check: deterministic bounded browser-evidence collection
    from the exact delivered HTML (never modified / rerendered here)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return _force_close_result(_load_state(cxt))
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)

        receipt = await _run_cli(
            "node bin/archify.mjs visual-check "
            f"{shlex.quote(str(state['output_html']))} --json",
            state["skill_dir"], ec)
        captures = (receipt.get("captures") or {})
        state["visual_receipt"] = {
            "ok": bool(receipt.get("ok")),
            "status": str(receipt.get("status") or ""),
            "error": str(receipt.get("error") or "")[:300],
            "evidence_kind": str(receipt.get("evidenceKind") or ""),
            "diagnostics": len(receipt.get("diagnostics") or []),
            # The screenshot sidecar's base name (the percept-review station's raw material; empty on skipped/runtime failure)
            "screenshots": [
                str(s.get("file") or "")
                for s in (captures.get("screenshots") or [])
                if isinstance(s, dict) and s.get("file")
            ][:_PERCEPT_MAX_SHOTS],
        }
        state["phases"].append("visual_check")
        _save_state(cxt, state)
        _emit_round(ec.stream, "visual_check", 0)
        return TurnResult(content="", next=AF_PERCEPT_CODE)


class AfPerceptExecutor(NodeExecutor):
    """af_percept: the perceptual review — one tool-less multimodal call;
    an image-capable reviewer judges the delivered artifact from the
    visual-check PNG sidecars against the skill's perceptual checklist.

    Verdicts come only from the attached screenshots; every degradation
    (no evidence, non-vision reviewer, unparseable output, transport
    error) is an honest skipped receipt — a pass is never fabricated
    (the skill's status vocabulary: passed / skipped / failed)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node, pattern = ec.node, ec.pattern
        if ec.force_close:
            return _force_close_result(_load_state(cxt))
        state = _load_state(cxt)
        if state is None:
            state = _new_state(cxt, _current_user_query(cxt), _skill_dir(ec))
            _safe_workspace_dir(ec, state)

        llm_config = cxt.llm_config or {}
        reviewer = {"code": str(llm_config.get("code") or ""),
                    "model": str(llm_config.get("model") or "")}

        def _skip(reason: str, images: int = 0) -> TurnResult:
            # Any failure shape of the review station is an honest receipt —
            # a pass is never fabricated (the same degradation stance as
            # af_route); delivery already succeeded, a failed review must not
            # drag the report down
            state["percept_receipt"] = {
                "status": "skipped", "reason": reason[:300],
                "reviewer": reviewer, "images": images,
                "correction_rounds": 0,
            }
            state["phases"].append("percept")
            _save_state(cxt, state)
            _emit_round(ec.stream, "percept", 0)
            return TurnResult(content="", next=AF_REPORT_CODE)

        # ---- Material guard: the visual-check screenshot sidecar ------------------------------
        visual = state.get("visual_receipt") or {}
        listed = [f for f in visual.get("screenshots") or [] if f]
        if not visual:
            return _skip("无浏览器证据(visual-check 未运行)")
        if not listed:
            return _skip("无浏览器证据截图(visual-check 未产出截图)")
        shot_dir = Path(state.get("output_html") or ".").parent
        shots = [shot_dir / f for f in listed]
        attached = [p for p in shots if p.is_file()][:_PERCEPT_MAX_SHOTS]
        if not attached:
            return _skip(f"截图文件缺失(回执列出 {len(listed)} 张,磁盘 0 张)")

        # ---- Capability guard: a reviewer model known to lack vision gets no images ------------------
        if vision_status(reviewer["code"], reviewer["model"]) is False:
            return _skip(
                f"评审模型无图像能力({reviewer['code']}/{reviewer['model']}),"
                "不向纯文本模型发送图像")

        # ---- Attachment inventory (unattached items are honestly listed in the prompt: unattached = unreviewed) ----------------------
        inventory = [f"- {p.name}" for p in attached]
        missing = [p.name for p in shots if not p.is_file()]
        if missing:
            inventory.append(f"(回执另列但文件缺失,未附: {', '.join(missing)})")
        over = [p.name for p in shots if p.is_file()][_PERCEPT_MAX_SHOTS:]
        if over:
            inventory.append(f"(超出附图上限,未附: {', '.join(over)})")
        framing = PERCEPT_PHASE_TMPL.format(
            request=state.get("request", ""),
            diagram_type=state.get("diagram_type", "?"),
            design_notes=str(state.get("design_notes") or "(无)"),
            shot_inventory="\n".join(inventory),
        )

        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(node, pattern)
        verdict: Dict[str, Any] = {}
        try:
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": ARCHIFY_BASE_PROMPT},
                # Multimodal content parts: text + image_url (data URLs),
                # passed through to the vision model with the message body
                # verbatim; this station's private workspace never enters
                # session history. The construction (including image reads
                # and size guards) must stay inside the try — a file
                # vanishing after the is_file() filter, or exceeding the
                # per-image cap, all degrade to an honest skipped; the
                # post-delivery review must never take the whole turn down
                {"role": "user",
                 "content": multimodal_user_content(framing, attached)},
            ]
            for attempt in range(1 + _runtime_settings()["percept_retries"]):
                if hooks:
                    fire(hooks, "on_llm_call", LLMCallEvent(
                        session_id=cxt.session_id, node_code=node.code,
                        round_idx=0, messages=messages,
                        model=llm_config.get("model", "")))
                result = await _stream_round(
                    provider, messages, llm_config.get("model", "default"),
                    llm_config.get("temperature", 0.7),
                    llm_config.get("max_tokens", 2048), ec.stream,
                    forward_text=False)  # the verdict is a JSON protocol: stream thinking only, never the body
                content = result.get("content", "") or ""
                if hooks:
                    fire(hooks, "on_llm_response", LLMResponseEvent(
                        session_id=cxt.session_id, node_code=node.code,
                        round_idx=0, content=content, tool_calls=[]))
                verdict, err = _extract_json_object(content)
                if (verdict.get("status") in ("passed", "failed")
                        and isinstance(verdict.get("defects"), list)):
                    break
                verdict = {}
                logger.warning("[archify] PERCEPT 判定解析失败(第 %d 次): %s",
                               attempt + 1, err)
                if attempt < _runtime_settings()["percept_retries"]:
                    messages.append({"role": "assistant",
                                     "content": content or "(空输出)"})
                    messages.append({"role": "user", "content":
                                     PERCEPT_RETRY_PROMPT.replace(
                                         "{error}", err)})
        except Exception as e:  # review call failed: delivery already succeeded, the report must continue
            return _skip(f"评审调用失败({type(e).__name__}: {e})",
                         len(attached))
        if not verdict:
            return _skip("评审输出不可解析为判定 JSON", len(attached))

        state["percept_receipt"] = {
            "status": verdict["status"],
            "defects": [
                {"viewport": str(d.get("viewport") or "?")[:24],
                 "theme": str(d.get("theme") or "?")[:12],
                 "issue": str(d.get("issue") or "")[:200]}
                for d in verdict.get("defects") or []
                if isinstance(d, dict)
            ][:_PERCEPT_MAX_SHOTS],
            "summary": str(verdict.get("summary") or "")[:300],
            "reviewer": reviewer,
            "images": len(attached),
            "correction_rounds": 0,  # v1 reports honestly only, with no auto-repair loop
        }
        state["phases"].append("percept")
        _save_state(cxt, state)
        _emit_round(ec.stream, "percept", 0)
        return TurnResult(content="", next=AF_REPORT_CODE)


class AfReportExecutor(NodeExecutor):
    """af_report: the terminal honest report — ASSEMBLED from collected
    receipts, never model prose (the anti-hallucination station). The
    three proof tiers are stated separately; perceptual review is the
    percept station's receipt (passed / failed / skipped), or honestly
    stated as not performed when the station was never reached."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node, pattern = ec.node, ec.pattern
        state = _load_state(cxt) or _new_state(
            cxt, _current_user_query(cxt), _skill_dir(ec))

        report = _compose_report(state)
        trace = {
            "request": state.get("request", ""),
            "diagram_type": state.get("diagram_type", ""),
            "is_mermaid": bool(state.get("is_mermaid")),
            "phases": list(state.get("phases") or []),
            "val_history": list(state.get("val_history") or []),
            "repair_log": list(state.get("repair_log") or []),
            "design_notes": str(state.get("design_notes") or "")[:600],
            "repair_rounds": int(state.get("repair_rounds") or 0),
            "author_rounds": int(state.get("author_rounds") or 0),
            "frozen": bool(state.get("frozen")),
            "honest_exit": bool(state.get("honest_exit")),
            "deliver_failed": bool(state.get("deliver_failed")),
            "degraded": bool(state.get("degraded")),
            "candidate_path": state.get("candidate_path", ""),
            "output_html": state.get("output_html", ""),
            "last_receipt": state.get("last_receipt", {}),
            "deliver_receipt": state.get("deliver_receipt", {}),
            "visual_receipt": state.get("visual_receipt", {}),
            "percept_receipt": state.get("percept_receipt", {}),
            "update_notice": state.get("update_notice", ""),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        cxt.metadata[_TRACE_KEY] = trace

        hooks = resolve_agent_hooks(node, pattern)
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id, node_code=node.code,
                rounds=trace["repair_rounds"] + trace["author_rounds"] + 2,
                outcome="reply", reply=report))

        _emit_round(ec.stream, "final", trace["repair_rounds"])
        return TurnResult(content=report, extra={_TRACE_KEY: trace})


# ============================================================================
# Report composing (deterministic, receipt-driven)
# ============================================================================

def _compose_report(state: Dict[str, Any]) -> str:
    """The three-tier honest report from collected receipts (the skill's
    Output contract, translated): paths / validation summary / receipts /
    browser evidence / truthful perceptual-review status."""
    dtype = state.get("diagram_type", "?")
    lines: List[str] = [f"【archify 图表工程汇报】(类型 {dtype})"]
    lines.append(f"规范: {state.get('candidate_path') or '(未写出)'}")
    if state.get("output_html"):
        lines.append(f"产物: {state['output_html']}")

    # ---- Validation layer ------------------------------------------------------
    hist = state.get("val_history") or []
    if not hist:
        lines.append("验证: 未执行")
    elif state.get("frozen"):
        lines.append(f"验证: showcase 验收通过(第 {len(hist)} 轮,9 项检查全过、"
                     f"0 错 0 警;候选已冻结)")
    else:
        lines.append(f"验证: 未通过(各轮错误数 {hist})")

    # ---- Repair layer ------------------------------------------------------
    rounds = int(state.get("repair_rounds") or 0)
    if state.get("honest_exit"):
        lines.append(f"修复: {rounds} 轮后按收敛契约停止——连续 "
                     f"{_runtime_settings()['stale_limit']} "
                     f"轮未刷新错误数下限,未解决诊断如实如下")
        for d in (state.get("last_receipt") or {}).get("diagnostics") or []:
            lines.append(f"  - [{d.get('code', '?')}] {d.get('message', '')}")
        err = (state.get("last_receipt") or {}).get("error") or ""
        if err:
            lines.append(f"  - (回执错误) {err}")
    elif rounds:
        lines.append(f"修复: {rounds} 轮聚焦修复")

    # ---- Delivery layer (deterministic artifact checks) --------------------------------------
    dr = state.get("deliver_receipt") or {}
    if state.get("deliver_failed"):
        lines.append("交付: 失败(非零退出,绝不称为成功;按契约未运行"
                     " visual-check——失败交付会保留旧产物,浏览器检查会查到"
                     "陈旧文件)")
        if dr.get("error"):
            lines.append(f"  - (回执) {dr['error']}")
        for d in dr.get("diagnostics") or []:
            lines.append(f"  - [{d.get('code', '?')}] {d.get('message', '')}")
    elif dr:
        lines.append("交付: 成功(规范字节冻结为同目录快照,原子提交 HTML,"
                     "回执含 SHA-256 与字节数——确定性产物证据)")

    # ---- Browser-evidence layer --------------------------------------------------
    vr = state.get("visual_receipt") or {}
    if state.get("deliver_failed"):
        lines.append("浏览器证据: 未收集(交付失败路径,按契约跳过)")
    elif vr:
        status = vr.get("status") or ("pass" if vr.get("ok") else "fail")
        lines.append(f"浏览器证据: {status}(有界真实浏览器行为;"
                     f"证据种类 {vr.get('evidence_kind') or 'automated-browser'})"
                     + (f";诊断 {vr['diagnostics']} 条" if vr.get("diagnostics")
                        else ""))
        if vr.get("error"):
            lines.append(f"  - (环境) {vr['error']}")
    else:
        lines.append("浏览器证据: 未收集")

    # ---- Perceptual-review layer (honest per the review receipt; honestly flagged when the review station was never reached) --------------
    pr = state.get("percept_receipt") or {}
    if pr.get("status") in ("passed", "failed"):
        reviewer = pr.get("reviewer") or {}
        lines.append(
            f"感知审查: {pr['status']}(图像能力评审 "
            f"{reviewer.get('code') or '?'}/{reviewer.get('model') or '?'},"
            f"{pr.get('images', 0)} 张截图;"
            f"correction_rounds {pr.get('correction_rounds', 0)})")
        for d in pr.get("defects") or []:
            lines.append(f"  - [{d.get('viewport')}/{d.get('theme')}] "
                         f"{d.get('issue')}")
        if pr.get("summary"):
            lines.append(f"  - (结论) {pr['summary']}")
    elif pr.get("status") == "skipped":
        lines.append(f"感知审查: skipped({pr.get('reason') or '原因未记录'})")
    elif state.get("deliver_failed"):
        lines.append("感知审查: 未执行(交付失败逃生路径,按契约跳过)")
    elif state.get("visual_receipt"):
        lines.append("感知审查: 未执行(未到达评审站;机器测量不证明感知质量,"
                     "需人工或图像能力评审另行执行)")
    else:
        # Wording avoids the word "deliver": an honest-exit repair report must not contain any delivery claim
        lines.append("感知审查: 未执行(未产生浏览器证据,无从评审;机器测量"
                     "不证明感知质量,需人工或图像能力评审另行执行)")

    # ---- Update-probe notice (information, never permission) ------------------------------------------
    if state.get("update_notice"):
        lines.append(f"【更新探针】{state['update_notice']}")

    return "\n".join(lines)


# ============================================================================
# Plugin registration — import side effect at the bottom (route.py imports
# this module at its end to complete registration)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", AF_ROUTE_CODE, AfRouteExecutor)
plugin_registry.register("executor", AF_AUTHOR_CODE, AfAuthorExecutor)
plugin_registry.register("executor", AF_PROBE_CODE, AfUpdateProbeExecutor)
plugin_registry.register("executor", AF_VALIDATE_CODE, AfValidateExecutor)
plugin_registry.register("executor", AF_REPAIR_CODE, AfRepairExecutor)
plugin_registry.register("executor", AF_DELIVER_CODE, AfDeliverExecutor)
plugin_registry.register("executor", AF_VISUAL_CODE, AfVisualCheckExecutor)
plugin_registry.register("executor", AF_PERCEPT_CODE, AfPerceptExecutor)
plugin_registry.register("executor", AF_REPORT_CODE, AfReportExecutor)
