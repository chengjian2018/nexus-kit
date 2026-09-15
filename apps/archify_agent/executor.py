"""The nine-station graph executors of the archify pattern — one class per
node (plugin code = node code; route.py's nodes bind via plugins={"loop": ...}):

    af_route ──> af_author ──┬───────────────> af_validate ──┬─> af_deliver
     类型路由     产物优先写作   │                   ↑  │       │    │
                    │         │             修复回路│  │未过    │    │
                    v         │                   │  v        v    v
             af_update_probe  │              af_repair ──> af_visual_check
              更新探针(侧枝)──┘                 │              │
                                                │连续五轮无改进   │附图评审
                                                │(诚实出口)      v
                                                +──────> af_percept ──> af_report
                                                  三级分离    图像能力评审   汇报(is_end)

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

# 代码默认值:app config bag(apps/archify_agent/config.yaml 的 config: 段,
# get_pattern_custom_config("archify"))按同键覆盖;未绑定 app 配置 → 空bag,
# 全部走这些默认(离线测试即此形态)。
_DEFAULT_AUTHOR_ROUNDS = 10  # af_author 工具轮次上限(find 示例 + 读 schema×3
                             # + 写候选 + 收口 + 一次容错;studio 实跑证明
                             # 6 轮在 find_files 加入后零余量,一次磕绊即耗尽)
_DEFAULT_REPAIR_ROUNDS = 3   # af_repair 每次访问的内部微循环轮次上限
                             # (改 → 自验 validate → 再改;图级宏观回路之外
                             # 的站内收敛余量;stale-5 止损仍是外层守卫)
_DEFAULT_ROUTE_RETRIES = 1   # af_route JSON 解析失败自纠重试次数
_DEFAULT_STALE_LIMIT = 5     # 连续未刷新错误数下限的轮数 → 诚实出口
_DEFAULT_PERCEPT_RETRIES = 1  # af_percept 判定 JSON 解析失败自纠重试次数
_PERCEPT_MAX_SHOTS = 8        # 感知评审附图上限(visual-check 标配 4 张,
                              # 防御性封顶;超出部分在提示词里如实标注未附)
_CANDIDATE_SNIPPET_CHARS = 20000  # 修复提示词内嵌候选内容的上限(超限头尾
                              # 截断并明示——旧 8000 会把中段的 connections
                              # 整段裁掉,修复站对着残缺候选"盲修";典型 12
                              # 节点候选 10-20K,20000 覆盖绝大多数全貌)
_DIAGNOSTIC_CHARS = 2400     # 单条诊断进提示词的截断上限("Suggested fix"
                              # 常在长消息尾部,截太狠会把答案裁掉)


def _runtime_settings() -> Dict[str, Any]:
    """预算 / 部署路径的收口读取点:app config bag 覆盖代码默认值。

    四个轮次预算 + skill_dir / workspace_root 全部经此函数解析,站点里
    不要散写 get_pattern_custom_config。int 键要求 ≥ 1(yaml 写坏 → warn +
    回默认,与全局配置同姿态);路径键缺席回落模块默认常量(测试经
    monkeypatch _DEFAULT_* 即打点)。"""
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
        # 绝对化:CLI 在 skill_dir 下运行,相对根会解析到技能目录而非仓库
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
_DEFAULT_SKILL_DIR = "~/.claude/skills/archify"
_DEFAULT_WORKSPACE_ROOT = "data/archify"
# 仓库证据核验根(archify CLI --repo-root,仅 architecture 声明 sources/
# meta.repository 时拼进命令):默认取服务启动目录——宿主通常就在被
# 文档化的仓库根上运行;部署在别处时经 app config bag 的 repo_root 覆盖
_DEFAULT_REPO_ROOT = str(Path.cwd())

_FORCE_CLOSE_REPLY = "(图表工程流程被步数预算截断,未能完成;已产出的回执见汇报,未完成步骤如实标注。)"


# ============================================================================
# State board helpers
# ============================================================================

def _new_state(cxt, request: str, skill_dir: str) -> Dict[str, Any]:
    """Fresh run state (af_route initializes; per-session workspace).

    workspace 先按缺省根取绝对值兜底;各站随即经 _safe_workspace_dir
    按 app config bag 覆盖重算(两个入口都保证绝对路径)。"""
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
        "val_history": [],       # 每次 validate 访问的客观错误数
        "last_receipt": {},      # 最近一次验证回执摘要(诊断供修复)
        "design_notes": "",      # 创作站收口的设计备忘(修复站的创作上下文)
        "repair_log": [],        # 每次修复访问的动作摘要(防重复已败动作)
        "solver_tried": [],      # 求解器跨访失败挪移键(防重演已败几何)
        "frozen": False,
        "repair_rounds": 0,
        "author_rounds": 0,
        "best_checkpoint": {},   # 错误数下限刷新时的候选字节+回执快照(回归守卫)
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
    """Pin a declared root to an absolute path (相对根按服务启动目录解析,
    与 file 工具的相对路径语义一致).

    状态板里的路径必须绝对:file 工具(write_text/edit_file)按服务启动
    目录解析相对路径,而 archify CLI 经 bash 以 workdir=skill_dir 运行、
    按技能目录解析——同一相对串在两个上下文解析到不同文件,验证站会
    ENOENT、修复站会修到验证永远看不到的文件(studio 实跑踩中)。
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
    """``--repo-root`` 拼装:仅 architecture 且候选声明了仓库证据
    (meta.repository 或任一组件 sources)时传递。

    CLI 对非 architecture 拒绝该旗标、无证据时核验器直接跳过,所以按
    候选内容条件化;不传时声明证据的候选会死锁在
    repository-evidence/root-required(修复站改 JSON 救不了——那是命令
    旗标问题)。传了之后核验器用真 git 对照本地 checkout 裁决
    url/revision/文件行号,产出的诊断(origin-mismatch / file-missing
    等)才都带着 supportedFixes、可被修复回路真正修掉。"""
    if state.get("diagram_type") != "architecture":
        return ""
    try:
        data = json.loads(
            Path(state.get("candidate_path") or "").read_text(
                encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return ""  # 候选缺失/损坏:validate 会如实记录,这里不抢戏
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
    return f" --repo-root \"{root}\"" if root else ""


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

    诊断保留 skill 修复契约点名的四要素:stable code、精确 subject、
    supportedFixes(message 承载 evidence 文本)、结构化 evidence——原实现
    只留 message,修复站等于拿着"症状描述"却丢了"处方"在修;evidence 是
    标签避让求解器的精确几何源(labelRect/线段/minimumPx)。"""
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
    """诊断的结构化 evidence 原样进摘要(超长/不可序列化的降级为空串)。

    evidence 是求解器与修复提示词的精确几何源;消息文本的正则兜底只在
    evidence 缺失时启用,不是等价替代。"""
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

# 求解器预算(代码常量,不进 app config bag:内部安全界,非部署调参)
_SOLVER_PAD = 2.0         # 超出 minimumPx 的额外安全边
_SOLVER_MAX_NUDGE = 60.0  # 单次挪移上限——更大的挪移=标签飞离自己的边,
                          # 那是布局级问题,归 LLM 的 row/col/pos 杠杆
_SOLVER_MAX_TRIALS = 8    # 每次修复访问求解器最多消耗的 validate 次数
_SOLVER_MAX_LABELS = 4    # 每次访问最多处理的标签诊断数


def _clearance_moves(rect: Tuple[float, float, float, float],
                     seg: Tuple[float, float, float, float],
                     minpx: float) -> List[Dict[str, float]]:
    """label-route-clearance 的四向挪移候选(按 |delta| 升序,就近优先)。

    竖直线段:左右横移让出段的 x,或上下纵移出段的 y 覆盖;水平线段对称。
    每向算出恰好净空 minpx+pad 的最小 delta。升序的理由:最小挪动最不易
    引发新碰撞(studio 实跑的那次 48px 标签挤压,正确解 +12px 恰好第一)。
    超出 _SOLVER_MAX_NUDGE 的丢弃。"""
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
    """从最近回执提取求解器可处理的标签诊断与各自的挪移候选。

    两类形态(其余归 LLM 微循环):
    1. composition/label-route-clearance(architecture/workflow/dataflow/
       lifecycle 共用):结构化 evidence(labelRect/线段/minimumPx)算
       四向挪移,消息文本正则兜底;
    2. layout/constraint 的「Label "X" overlaps component」:渲染器
       Suggested fix 给出的 below/above 两个 labelAt 绝对点,按建议序验证。
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
    """把一次挪移落进目标 connection:绝对点写 labelAt;增量优先折进已有
    labelAt,否则累加 labelDx/labelDy(与验证器的 supportedFixes 语义一致)。

    成败由调用方跑真验证器裁决;这里只做机械落位。"""
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
    """零 LLM 的确定性标签避让:按诊断几何算挪移、真验证器裁决、劣化回滚。

    studio 实跑回归(label-route-clearance 无建议坐标):48px 标签挤在
    组件右缘与竖直路由段之间,LLM 六轮做不出像素级避让——左移撞组件、
    原地打转,直到 stale-5 诚实退出。几何归工具:每个候选挪移落盘后跑
    validate,客观错误数严格下降才保留,否则字节回滚;跨访失败动作记入
    solver_tried 防重演。返回(进修复履历的摘要,是否已达 showcase 验收)。
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
        return "", False  # 候选缺失/损坏:写候选是 LLM 微循环的职责
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
                # 与闸门同源的同旗标验证:声明证据的候选若不带 --repo-root,
                # 求解器永远看到 root-required(1 错),对 1 错基线判"无
                # 改进"→ 全部回滚并记履历,几何可解的 label-clearance
                # 死锁交给 LLM(studio 会话 f2cae679 实跑:[1,18,16,1])
                "node bin/archify.mjs validate "
                f"{state['diagram_type']} \"{where}\" "
                f"--quality showcase{_repo_root_flag(state)} --json",
                state["skill_dir"], ec)
            trials += 1
            # 真验证守卫:bash 失败/回执损坏既不算改进也不进履历(防瞬时
            # 故障毒化——count=1 的错误回执会伪装成对基线 3 的"改进")
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
                cand.write_text(best_bytes, encoding="utf-8")  # 回滚
            except OSError:
                break
        if not resolved:
            anchor_key = _solver_move_key(target, target["moves"][0]).split("|")[0]
            if any(key.startswith(anchor_key + "|") for key in tried):
                notes.append(f"{anchor}: 就近挪移均未更优(已记履历,交布局级杠杆)")

    if best_count < _receipt_error_count(state.get("last_receipt") or {}):
        state["last_receipt"] = _receipt_summary(best_receipt)
    tried[:] = tried[-64:]  # 防御性封顶(正常远达不到)
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
    """候选落地判定 + 模型路径漂移收编(studio 实跑回归)。

    优先 candidate_path 本体;若缺失/损坏,从本轮 write_text 调用里倒序
    找最后一个内容可解析为带 diagram_type 的 JSON 对象者,由执行器钉回
    candidate_path——内容归模型、落位归执行器(模型自选路径写候选是
    实跑观测到的真实失败形态)。都不成立才返回 False(诚实交修复回路)。
    """
    cand = Path(state["candidate_path"])
    if cand.exists():
        try:
            if isinstance(json.loads(cand.read_text(encoding="utf-8")), dict):
                return True
        except (json.JSONDecodeError, ValueError, OSError):
            pass  # 落了但损坏 → 继续尝试收编更早的有效写
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
# The eight station executors
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
                forward_text=False)  # 路由是 JSON 协议:只流思考,不流正文
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
            example_path=skill / "examples",  # 目录:模型经 find_files 挑一个匹配示例
            example_glob=dtype,               # glob 即类型名(如 *workflow*)
            candidate_path=state["candidate_path"],
        )
        workspace: List[Dict[str, Any]] = [
            {"role": "system", "content": ARCHIFY_BASE_PROMPT},
            {"role": "user", "content": framing},
        ]

        provider = build_provider(cxt.llm_config or {})
        llm_config = cxt.llm_config or {}
        writes: List[Dict[str, str]] = []
        closing = ""  # 最后一次非空回复(契约上是收口+设计备忘)

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
                forward_text=False)  # 中间工作轮:思考上屏,正文不上屏
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
            if Path(state["candidate_path"]).exists():
                _emit_round(ec.stream, "author", round_idx)

        candidate_ok = _ensure_candidate(state, writes)
        # 创作上下文带去修复站:原 skill 里修复发生在同一会话(模型记得自己
        # 的布局意图与标签取舍);图配方把创作/修复拆成了两个失忆的工作区,
        # 这份收口备忘就是跨越失忆的那座桥
        state["design_notes"] = closing.strip()[:600]
        state["phases"].append("author" if candidate_ok else "author_failed")
        _save_state(cxt, state)
        _emit_round(ec.stream, "author", state["author_rounds"])
        # 契约是"首个候选存在后"探一次:候选未落地不消耗探针这步,直接验证
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
            status = "silent"  # 检查器不可运行:契约规定继续且不提及
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
            if event_key:
                # 呈现后确认 eventKey(尽力而为,结果不影响主线)
                await _run_cli(
                    f'node scripts/check-update.mjs --ack "{event_key}"',
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
            # 已冻结(防御性重入):不再重复验证
            return TurnResult(content="", next=AF_DELIVER_CODE)

        candidate = Path(state["candidate_path"])
        if not candidate.exists():
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
                f"{state['diagram_type']} \"{candidate}\" "
                f"--quality showcase{_repo_root_flag(state)} --json",
                state["skill_dir"], ec)

        errors = _receipt_error_count(receipt)
        prev_min = min(state["val_history"]) if state["val_history"] else None
        state["val_history"].append(errors)
        state["last_receipt"] = _receipt_summary(receipt)

        # 最优检查点:错误数刷新历史下限(或首次/清零)时快照候选字节与
        # 回执摘要——修复站回归守卫的回滚源(LLM 编辑无条件落盘,唯闸门
        # 验证是客观裁决点)
        if errors == 0 or prev_min is None or errors < prev_min:
            try:
                state["best_checkpoint"] = {
                    "bytes": candidate.read_text(encoding="utf-8"),
                    "receipt": dict(state["last_receipt"]),
                }
            except OSError:
                pass  # 候选缺失(author/missing-candidate 路径):无可快照

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

        # ---- 回归守卫(deterministic,先于收敛闸门) --------------------
        # 求解器有"严格更优才保留"的字节回滚,LLM 微循环没有——它的编辑
        # 无条件落盘。上一轮验证比历史最优更差时,先回滚到最优检查点
        # (字节+回执摘要)再修:studio 实跑观测到修复站把候选从 1 错修到
        # 13 错(val_history [1,1,1,13]),带伤进入下一访会把盲目修补复合
        # 放大;诚实出口路径也因此带着退化最深的候选收场。
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

        # ---- 收敛闸门(在一切 LLM 之前, deterministic) ----------------
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

        # ---- 确定性标签避让求解器(零 LLM,真验证器裁决) ----------------
        # 两类标签诊断的修法是矩形避让算术,不是语义判断:组件重叠(建议
        # 坐标 below/above)与 label-route-clearance(labelRect+线段证据的
        # 四向就近挪移)。求解器严格更优才保留、否则字节回滚;清零即直达
        # 验证闸门,不消耗 LLM 轮次(LLM 修不动像素几何——studio 实跑六轮
        # 打转到 stale-5 诚实退出)。
        solver_note = ""
        solver_passed = False
        try:
            solver_note, solver_passed = await _solve_label_clearance(state, ec)
        except Exception as e:  # 求解器绝不阻断修复主线
            logger.warning("[archify] 标签避让求解器异常(跳过): %s", e)
        if solver_note:
            logger.info("[archify] 标签避让求解器: %s", solver_note)

        if solver_passed:
            # 求解器已达 showcase 验收:不再开 LLM 工作区,交验证闸门裁决
            state["repair_rounds"] += 1
            state["repair_log"].append(
                {"round": state["repair_rounds"],
                 "summary": solver_note[:400]})
            state["phases"].append(f"repair_{state['repair_rounds']}")
            _save_state(cxt, state)
            _emit_round(ec.stream, "repair", state["repair_rounds"])
            return TurnResult(content="", next=AF_VALIDATE_CODE)

        # ---- 聚焦修复微循环(write_text/read_text/edit_file/bash) ------
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

        # 创作上下文与修复履历(原 skill 里修复与创作同会话,模型记得设计
        # 意图与已试动作;图配方拆站后由状态板替它记)
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
        closing = ""  # 最后一次非空回复(契约上是"修了什么"的收口摘要)

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
                forward_text=False)  # 中间工作轮:思考上屏,正文不上屏
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

        # 候选缺失场景:修复站以 write_text 重写候选,同样做漂移收编
        if writes:
            _ensure_candidate(state, writes)

        state["repair_rounds"] += 1
        # 动作履历供下一次访问防重演:图配方每访都开新工作区,没有这条,
        # 修复站会把上一访已失败的动作原样再试一遍(等于浪费整轮预算);
        # 求解器动作前缀在本访 LLM 轮次之前发生,同条记录
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
            f"{state['diagram_type']} \"{state['candidate_path']}\" "
            f"\"{state['output_html']}\" "
            f"--quality showcase{_repo_root_flag(state)} --json",
            state["skill_dir"], ec)
        state["deliver_receipt"] = _receipt_summary(receipt)

        if receipt.get("ok") is True:
            state["phases"].append("deliver")
            _save_state(cxt, state)
            _emit_round(ec.stream, "deliver", 0)
            return TurnResult(content="", next=AF_VISUAL_CODE)

        # 非零退出绝不称为成功:失败保真走汇报站(不跑浏览器检查)
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
            f"\"{state['output_html']}\" --json",
            state["skill_dir"], ec)
        captures = (receipt.get("captures") or {})
        state["visual_receipt"] = {
            "ok": bool(receipt.get("ok")),
            "status": str(receipt.get("status") or ""),
            "error": str(receipt.get("error") or "")[:300],
            "evidence_kind": str(receipt.get("evidenceKind") or ""),
            "diagnostics": len(receipt.get("diagnostics") or []),
            # 截图侧车基名(感知评审站的原料;skipped/运行时失败时为空)
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
            # 评审站的任何失败形态都是诚实回执,绝不编造通过(同 af_route
            # 降级姿态);交付已成功,评审失败不拖垮汇报
            state["percept_receipt"] = {
                "status": "skipped", "reason": reason[:300],
                "reviewer": reviewer, "images": images,
                "correction_rounds": 0,
            }
            state["phases"].append("percept")
            _save_state(cxt, state)
            _emit_round(ec.stream, "percept", 0)
            return TurnResult(content="", next=AF_REPORT_CODE)

        # ---- 原料守卫:visual-check 截图侧车 ------------------------------
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

        # ---- 能力守卫:已知无图像能力的评审模型不发送图像 ------------------
        if vision_status(reviewer["code"], reviewer["model"]) is False:
            return _skip(
                f"评审模型无图像能力({reviewer['code']}/{reviewer['model']}),"
                "不向纯文本模型发送图像")

        # ---- 附图清单(未附的如实进提示词:未附不评) ----------------------
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
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": ARCHIFY_BASE_PROMPT},
            # 多模态 content parts:文本 + image_url(data URL),随消息体
            # 原样透传到视觉模型;本站私有工作区,不进会话历史
            {"role": "user",
             "content": multimodal_user_content(framing, attached)},
        ]

        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(node, pattern)
        verdict: Dict[str, Any] = {}
        try:
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
                    forward_text=False)  # 判定是 JSON 协议:只流思考,不流正文
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
        except Exception as e:  # 评审调用失败:交付已成功,汇报必须继续
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
            "correction_rounds": 0,  # 首版只如实上报,不带自动修复回路
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

    # ---- 验证层 ------------------------------------------------------
    hist = state.get("val_history") or []
    if not hist:
        lines.append("验证: 未执行")
    elif state.get("frozen"):
        lines.append(f"验证: showcase 验收通过(第 {len(hist)} 轮,9 项检查全过、"
                     f"0 错 0 警;候选已冻结)")
    else:
        lines.append(f"验证: 未通过(各轮错误数 {hist})")

    # ---- 修复层 ------------------------------------------------------
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

    # ---- 交付层(确定性产物检查) --------------------------------------
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

    # ---- 浏览器证据层 --------------------------------------------------
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

    # ---- 感知审查层(按评审回执如实;未到达评审站时如实标注) --------------
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
        # 措辞避开"交付"字样:修复诚实出口的汇报里不得出现任何交付声明
        lines.append("感知审查: 未执行(未产生浏览器证据,无从评审;机器测量"
                     "不证明感知质量,需人工或图像能力评审另行执行)")

    # ---- 探针通知(信息非许可) ------------------------------------------
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
