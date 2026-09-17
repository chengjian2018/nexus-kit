"""The four station executors of note_polish — the minimal archify-style
graph recipe (state board + convergence gate + experience inheritance),
stripped to what a first app actually needs.

Teaching map (each idiom cites its grown-up source in apps/archify_agent/):

- State board  : _new_state/_load_state/_save_state over the single
                 graph_state key "np_state" (archify _STATE_KEY idiom).
- LLM calls    : build_provider + build_agent_messages + _stream_round,
                 forward_text=False (station chatter is not the user reply).
- JSON protocol: first balanced {...} + ONE self-correct retry
                 (archify _extract_json_object / route_retries idiom).
- Convergence  : deterministic gate in np_review — pass / max_rounds /
                 stale (top issue unchanged twice) — never model-judged
                 (archify _trailing_stale idiom, miniaturized).
- Inheritance  : np_revise prompts from critique_log + revision_log —
                 the bridge across per-round amnesia (archify design_notes /
                 repair_log / solver_tried idiom).
- Honesty      : every degradation (empty draft, unparseable review, lost
                 state, force-close) exits to np_deliver with a stated
                 done_reason instead of fabricating a pass.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from atoms.executors.loop_executor import _stream_round

from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.messages import build_agent_messages
from nexus.engine.turn_result import TurnResult
from nexus.llm.resolve import build_provider
from nexus.settings import get_pattern_custom_config

from apps.note_polish_agent.prompts import (
    DRAFT_PHASE_PROMPT,
    REVIEW_PHASE_PROMPT,
    REVIEW_RETRY_PROMPT,
    REVISE_PHASE_TMPL,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Station codes (= plugin codes; route.py binds via plugins={"loop": ...})
# ---------------------------------------------------------------------------

NP_DRAFT_CODE = "np_draft"
NP_REVIEW_CODE = "np_review"
NP_REVISE_CODE = "np_revise"
NP_DELIVER_CODE = "np_deliver"

_STATE_KEY = "np_state"

# Code defaults — the app config bag (apps/note_polish_agent/config.yaml
# `config:` section, via get_pattern_custom_config) overrides by the same keys.
_DEFAULT_WORKSPACE_ROOT = "data/note_polish"
_DEFAULT_MAX_REVIEW_ROUNDS = 3
# Two consecutive review rounds with the same top issue → honest exit.
_STALE_LIMIT = 2

_FORCE_CLOSE_REPLY = "(笔记打磨流程被步数预算截断，未能完成；已产出的内容见下，未完成步骤如实标注。)"


def _runtime_settings() -> Dict[str, Any]:
    """The single read point for deployment knobs (app config bag over code defaults)."""
    bag = get_pattern_custom_config("note_polish")
    raw_rounds = bag.get("max_review_rounds")
    try:
        max_rounds = int(raw_rounds) if raw_rounds is not None else _DEFAULT_MAX_REVIEW_ROUNDS
    except (TypeError, ValueError):
        max_rounds = _DEFAULT_MAX_REVIEW_ROUNDS
    if max_rounds < 1:
        max_rounds = _DEFAULT_MAX_REVIEW_ROUNDS
    return {
        "workspace_root": str(bag.get("workspace_root") or _DEFAULT_WORKSPACE_ROOT),
        "max_review_rounds": max_rounds,
    }


def _absolutize(raw: str) -> Path:
    """Pin a declared root to an absolute path (relative roots resolve against
    the service startup directory — never leave relative paths on the state
    board; file tools and bash workdirs resolve them differently)."""
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def _safe_session(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(session_id)) or "s"


def _new_state(cxt, request: str) -> Dict[str, Any]:
    """Fresh run state (np_draft initializes; per-session workspace)."""
    workspace = _absolutize(_runtime_settings()["workspace_root"]) / _safe_session(cxt.session_id)
    return {
        "request": request,
        "workspace": str(workspace),
        "memo_path": str(workspace / "memo.md"),
        "round": 0,
        "critique_log": [],        # every review round's {round, verdict, issues}
        "revision_log": [],        # per-revise-visit summary (anti-replay memory)
        "last_issues_signature": "",
        "stale": 0,
        "done_reason": "",         # pass | max_rounds | stale | <honest bail codes>
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


def _read_memo(state: Dict[str, Any]) -> str:
    try:
        return Path(state["memo_path"]).read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_memo(state: Dict[str, Any], text: str) -> bool:
    """Placement belongs to code: the model returns text, the executor lands it."""
    try:
        path = Path(state["memo_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as e:
        logger.warning("[note_polish] 备忘写盘失败: %s", e)
        return False


async def _one_llm_call(ec, user_content: str) -> str:
    """One tool-less LLM call in the station's own workspace framing."""
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
        forward_text=False)  # station work is not the user-visible reply
    return (result.get("content", "") or "").strip()


def _extract_json_object(content: str) -> Tuple[Dict[str, Any], str]:
    """First balanced {...} block → parse. Returns (obj, err); obj empty on failure."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {}, "输出中找不到 JSON 对象（缺少 {...}）"
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as e:
        return {}, f"JSON 语法错误: {e}"
    if not isinstance(data, dict):
        return {}, "JSON 顶层不是对象"
    return data, ""


def _issues_signature(issues: list) -> str:
    """The stale rule's signal: the top issue, normalized (computed, never guessed)."""
    if not issues:
        return ""
    return re.sub(r"\s+", "", str(issues[0]).strip().lower())[:80]


def _bail(state: Dict[str, Any], cxt, reason: str) -> TurnResult:
    """Honest early exit to the deliver station with a stated reason."""
    state["done_reason"] = reason
    _save_state(cxt, state)
    return TurnResult(content="", next=NP_DELIVER_CODE)


# ============================================================================
# The four station executors
# ============================================================================

class NpDraftExecutor(NodeExecutor):
    """np_draft: init the state board, one LLM call for the draft memo,
    executor lands the file (an empty draft never fabricates — it bails)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _new_state(cxt, _current_user_query(cxt))
        _save_state(cxt, state)

        memo = await _one_llm_call(
            ec, DRAFT_PHASE_PROMPT.format(request=state["request"]))
        if not memo:
            return _bail(state, cxt, "empty_draft")
        if not _write_memo(state, memo):
            return _bail(state, cxt, "memo_write_failed")
        return TurnResult(content="", next=NP_REVIEW_CODE)


class NpReviewExecutor(NodeExecutor):
    """np_review: one LLM call (JSON protocol, one retry) + the DETERMINISTIC
    convergence gate — pass / max_review_rounds / stale. The model reports;
    code decides when to stop."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            return _bail(_new_state(cxt, ""), cxt, "state_lost")

        memo = _read_memo(state)
        if not memo:
            return _bail(state, cxt, "memo_missing")

        verdict: Dict[str, Any] = {}
        prompt = REVIEW_PHASE_PROMPT.format(memo=memo)
        for attempt in range(2):  # initial + one self-correct retry
            content = await _one_llm_call(ec, prompt)
            verdict, err = _extract_json_object(content)
            if verdict.get("verdict") in ("pass", "fail"):
                break
            verdict = {}
            logger.warning("[note_polish] 评审 JSON 解析失败(第 %d 次): %s",
                           attempt + 1, err)
            prompt = REVIEW_RETRY_PROMPT.format(error=err)
        if not verdict:
            return _bail(state, cxt, "review_unparseable")

        issues = [str(i) for i in (verdict.get("issues") or []) if str(i).strip()]
        state["round"] += 1
        state["critique_log"].append(
            {"round": state["round"],
             "verdict": verdict["verdict"],
             "issues": issues})

        # ---- Convergence gate (deterministic, never model-judged) ----
        if verdict["verdict"] == "pass":
            state["done_reason"] = "pass"
            nxt = NP_DELIVER_CODE
        elif state["round"] >= _runtime_settings()["max_review_rounds"]:
            state["done_reason"] = "max_rounds"
            nxt = NP_DELIVER_CODE
        else:
            sig = _issues_signature(issues)
            state["stale"] = state["stale"] + 1 if sig and sig == state["last_issues_signature"] else 0
            state["last_issues_signature"] = sig
            if state["stale"] >= _STALE_LIMIT:
                state["done_reason"] = "stale"  # stop polishing, report honestly
                nxt = NP_DELIVER_CODE
            else:
                nxt = NP_REVISE_CODE
        _save_state(cxt, state)
        return TurnResult(content="", next=nxt)


class NpReviseExecutor(NodeExecutor):
    """np_revise: THE experience-inheritance station. The prompt carries the
    request, the current memo, the FULL critique history, and the revision
    log — without them, round 3 happily replays round 1's failed fix."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt)
        if state is None:
            return _bail(_new_state(cxt, ""), cxt, "state_lost")

        memo = _read_memo(state)
        critique_history = "\n".join(
            "第{r}轮 [{v}]：{issues}".format(
                r=entry.get("round"), v=entry.get("verdict"),
                issues="；".join(entry.get("issues") or []) or "(无)")
            for entry in state["critique_log"]) or "(尚无评审)"
        revision_history = "\n".join(state["revision_log"]) or "(尚无修订)"

        revised = await _one_llm_call(ec, REVISE_PHASE_TMPL.format(
            request=state.get("request", ""),
            memo=memo,
            critique_history=critique_history,
            revision_history=revision_history))

        latest = state["critique_log"][-1] if state["critique_log"] else {}
        top = "；".join((latest.get("issues") or [])[:2]) or "无明确意见"
        if revised:
            landed = _write_memo(state, revised)
            state["revision_log"].append(
                f"第{state['round']}轮修订：针对（{top}）"
                + ("已落盘" if landed else "写盘失败，未生效"))
        else:
            state["revision_log"].append(
                f"第{state['round']}轮修订：输出为空，未落盘")
        _save_state(cxt, state)
        return TurnResult(content="", next=NP_REVIEW_CODE)


class NpDeliverExecutor(NodeExecutor):
    """np_deliver: deterministic assembly (no LLM) — final memo + an honest
    process summary; the only station whose content becomes the reply."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)
        state = _load_state(cxt) or _new_state(cxt, "")

        reason = state.get("done_reason") or "interrupted"
        memo = _read_memo(state) or "(备忘未能产出或未落盘)"
        summary = f"打磨完成度：{reason}（共评审 {state.get('round', 0)} 轮）"
        return TurnResult(content=f"{summary}\n\n---\n\n{memo}")


# ============================================================================
# Self-registration (plugin code = node code; route.py's bottom import
# triggers this module — the archify / deep_research convention)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", "np_draft", NpDraftExecutor)
plugin_registry.register("executor", "np_review", NpReviewExecutor)
plugin_registry.register("executor", "np_revise", NpReviseExecutor)
plugin_registry.register("executor", "np_deliver", NpDeliverExecutor)
