"""The six-node topic-research executor — one node per pipeline station:

    tr_preplan ──next──> tr_plan ──sends──> tr_search ×N ──join──> tr_merge ──next──> tr_report ──next──> tr_polish
     pre-plan/pre-retrieve    per-topic planning    one instance per topic   structured merge        report draft          format polish (streaming, terminal)

Station inventory:

    PREPLAN  research-state initialization + optional pre-retrieval (the
             model itself decides; reuse of DeepResearchExecutor's
             stateless phase method)
    PLAN     one tool-less LLM call → {"themes": [...]} (fault-tolerant
             JSON extraction with a self-correcting retry, degrading to
             [the original question]); then dispatches ONE tr_search
             worker instance per theme via ``TurnResult.sends`` (the
             engine's runtime fan-out — this app exercises exactly that)
    SEARCH   a fan-out WORKER: one instance researches ONE theme in its
             private workspace (reuse of DeepResearchExecutor's
             _search_branch — per-instance rounds guard, findings travel
             back only via TurnResult.extra onto the __fanout_results__
             board)
    MERGE    the fan-out JOIN (barrier): a STRUCTURAL merge only — no LLM
             call; folds the board's per-branch findings/tool_stats/
             reflection notes into the state board (FIFO findings cap,
             failed branches counted, never blocking)
    REPORT   one tool-less LLM call writing the report DRAFT from the
             merged findings (citations [S1]..; no delta forwarding — the
             draft is not the reply)
    POLISH   one streaming LLM call beautifying the draft's format (the
             only delta-forwarding station; terminal — writes the final
             trace into cxt.metadata["topic_research"])

Reuse note: PREPLAN/SEARCH reuse the battle-tested stateless phase methods
of apps.deep_research_agent.executor_multi.DeepResearchExecutor (the same
subclass-for-phase-reuse idiom the four dr_* executors use internally);
the theme split, merge, report and polish stations are this app's own.

Inter-station state travels via ``cxt.graph_state["topic_research_state"]``
(own key — no collision with the deep_research recipe); workers see none
of it (branch isolation), which is why the dispatch payload folds in the
theme + sub-question.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from atoms.executors.loop_executor import _emit_round, _stream_round
from atoms.tools.mcp_tool import ensure_mcp_ready

from nexus.engine.agent_hooks import (
    AgentEndEvent,
    AgentStartEvent,
    LLMCallEvent,
    LLMResponseEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
)
from nexus.engine.chat import FANOUT_RESULTS_KEY
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.loop import TurnResult, _resolve_tools, warn_prompt_length
from nexus.engine.messages import build_agent_messages
from nexus.engine.turn_result import Send
from nexus.llm.resolve import build_provider

from apps.deep_research_agent.executor_multi import (
    DeepResearchExecutor,
    _MAX_FINDINGS,
    _current_user_query,
)
from apps.topic_research_agent.prompts import (
    POLISH_PROMPT_TEMPLATE,
    REPORT_PROMPT_TEMPLATE,
    THEMES_PLAN_PROMPT,
    THEMES_RETRY_PROMPT,
    TOPIC_BASE_PROMPT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runaway protection budget (hard LLM call ceiling ≈ 1(pre-retrieval) + 1 +
# 1(self-correct retry) + max_fanout × per-branch SEARCH + 1(report) +
# 1(polish); MERGE is structural and costs zero LLM calls)
# ---------------------------------------------------------------------------

_THEMES_RETRIES = 1              # PLAN JSON parse-failure retry count

# ---------------------------------------------------------------------------
# Pipeline topology (node code = plugin code; route.py's six nodes bind
# accordingly)
# ---------------------------------------------------------------------------

TR_PREPLAN_CODE = "tr_preplan"
TR_PLAN_CODE = "tr_plan"
TR_SEARCH_CODE = "tr_search"
TR_MERGE_CODE = "tr_merge"
TR_REPORT_CODE = "tr_report"
TR_POLISH_CODE = "tr_polish"

# In-flight state key on the graph runtime's state board (own key — the
# deep_research recipe's state never collides); the final trace keeps the
# "topic_research" key
_STATE_KEY = "topic_research_state"
_TRACE_KEY = "topic_research"

_FORCE_CLOSE_REPLY = "(研究流程被强制收尾,未能完成研究。)"

_NO_DRAFT_REPLY = "(研究流程异常:无报告草稿可美化,证据不足。)"


def _load_state(cxt) -> Optional[Dict[str, Any]]:
    """Load the in-flight research state from the graph state board (a
    non-dict shape counts as none, guarding against dirty data)."""
    state = (cxt.graph_state or {}).get(_STATE_KEY)
    return state if isinstance(state, dict) else None


def _save_state(cxt, state: Dict[str, Any]) -> None:
    cxt.graph_state[_STATE_KEY] = state


def _orphan_state(cxt, node, phase: str) -> Dict[str, Any]:
    """Fallback state for an anomalous entry: with no in-flight state, go
    straight to wrap-up — the pipeline degrades to a single-theme,
    no-material run rather than deadlocking any station."""
    return {
        "question": _current_user_query(cxt),
        "phases": [phase],
        "degraded": True,
        "session_id": cxt.session_id,
        "node_code": node.code,
        "findings": [],
        "tool_stats": {},
        "plan": {},
    }


def _extract_themes_json(content: str) -> Tuple[Dict[str, Any], str]:
    """Fault-tolerant PLAN JSON extraction: first balanced ``{...}`` block →
    parse → validate themes is a non-empty list of strings.

    Returns: (plan, err) — on success plan is non-empty and err an empty
    string; on failure plan is an empty dict and err a model-facing
    self-correction description (fed into THEMES_RETRY_PROMPT).
    """
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {}, "输出中找不到 JSON 对象(缺少 {...})"
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as e:
        return {}, f"JSON 语法错误: {e}"
    if not isinstance(data, dict):
        return {}, "JSON 顶层不是对象"
    themes = data.get("themes")
    if (not isinstance(themes, list)
            or not themes
            or not all(isinstance(t, str) and t.strip() for t in themes)):
        return {}, "缺少合法的 themes 字段(需非空字符串数组)"
    return {"themes": [t.strip() for t in themes],
            "notes": str(data.get("notes", ""))}, ""


class TopicResearchExecutor(DeepResearchExecutor):
    """Stateless station-method base of the six graph executors (PREPLAN →
    PLAN → SEARCH → MERGE → REPORT → POLISH); the per-node execute()
    orchestration lives in the Tr* subclasses below."""

    # ------------------------------------------------------------------
    # PLAN (per-theme)
    # ------------------------------------------------------------------

    async def _themes_phase(self, provider,
                            messages: List[Dict[str, Any]],
                            llm_config: Dict[str, Any],
                            ec: "ExecutionContext", hooks,
                            trace: Dict[str, Any]) -> Dict[str, Any]:
        """One tool-less LLM call splitting the question into research
        themes (messages include the pre-retrieval context, if any).

        Fault-tolerant JSON extraction with a self-correcting retry (the
        bad output + parse error are fed back into messages); a final
        failure degrades to ``{"themes": [the original question]}`` — a
        PLAN failure never blocks the research itself. No deltas
        forwarded (the themes JSON is not the reply).
        """
        cxt = ec.cxt
        node = ec.node
        model = llm_config["model"]
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2048)

        plan_messages = list(messages) + [
            {"role": "user", "content": THEMES_PLAN_PROMPT}]

        plan: Dict[str, Any] = {}
        for attempt in range(1 + _THEMES_RETRIES):
            if hooks:
                fire(hooks, "on_llm_call", LLMCallEvent(
                    session_id=cxt.session_id,
                    node_code=node.code,
                    round_idx=0, messages=plan_messages, model=model))
            result = await _stream_round(
                provider, plan_messages, model, temperature, max_tokens,
                None)  # no delta forwarding
            content = result.get("content", "") or ""
            if hooks:
                fire(hooks, "on_llm_response", LLMResponseEvent(
                    session_id=cxt.session_id,
                    node_code=node.code,
                    round_idx=0, content=content, tool_calls=[]))
            plan, err = _extract_themes_json(content)
            if plan:
                break
            logger.warning("[topic_research] PLAN 主题 JSON 解析失败(第 %d 次): %s",
                           attempt + 1, err)
            if attempt < _THEMES_RETRIES:
                # Self-correcting retry: bad output + error fed back into
                # messages (not a verbatim re-send)
                plan_messages.append(
                    {"role": "assistant", "content": content or "(空输出)"})
                plan_messages.append(
                    {"role": "user",
                     "content": THEMES_RETRY_PROMPT.replace("{error}", err)})

        if not plan:
            plan = {"themes": [trace.get("question", "")],
                    "notes": "规划降级:直接研究原问题"}
            trace["degraded"] = True

        trace["phases"].append("plan")
        trace["plan"] = plan
        _emit_round(ec.stream, "plan", 0)
        return plan

    # ------------------------------------------------------------------
    # MERGE (join: pure structural folding, zero LLM calls)
    # ------------------------------------------------------------------

    def _merge_phase(self, cxt,
                     state: Dict[str, Any]) -> Dict[str, Any]:
        """Fold the fan-out results board into the state board.

        Board entries: {branch_id, node_code, ok, content, extra, error?};
        a failed branch is counted and skipped (degrades the report,
        never blocks it). Pre-retrieval findings from the state board
        merge in first, per-branch findings follow (completion order),
        with the shared FIFO findings cap.

        Returns the merged summary (also stored into state["merged"] by
        the caller): findings / tool_stats / per-theme stats / reflection
        notes / branch counts.
        """
        board = [e for e in (cxt.graph_state.get(FANOUT_RESULTS_KEY) or [])
                 if isinstance(e, dict)]
        findings: List[Dict[str, Any]] = list(state.get("findings") or [])
        tool_stats: Dict[str, int] = dict(state.get("tool_stats") or {})
        per_theme: Dict[str, Dict[str, Any]] = {}
        notes: List[str] = []
        failed_branches = sum(1 for e in board if not e.get("ok"))
        for entry in board:
            if not entry.get("ok"):
                continue
            ex = entry.get("extra") or {}
            theme = str(ex.get("sub_question", ""))
            branch_findings = ex.get("findings") or []
            per_theme[theme] = {"findings": len(branch_findings),
                                "rounds": int(ex.get("rounds") or 0)}
            findings.extend(branch_findings)
            for name, cnt in (ex.get("tool_stats") or {}).items():
                tool_stats[name] = tool_stats.get(name, 0) + cnt
            if ex.get("reflection_note"):
                notes.append(f"[{theme}] {ex['reflection_note']}")
        if len(findings) > _MAX_FINDINGS:
            findings = findings[len(findings) - _MAX_FINDINGS:]
        if failed_branches:
            logger.warning(
                "[topic_research] 扇出分支失败 %d/%d,报告将基于部分资料",
                failed_branches, len(board))
        return {
            "findings": findings,
            "tool_stats": tool_stats,
            "per_theme": per_theme,
            "reflection_note": "\n".join(notes),
            "total": len(board),
            "failed": failed_branches,
        }

    # ------------------------------------------------------------------
    # REPORT (report draft: tool-less, no delta forwarding)
    # ------------------------------------------------------------------

    async def _report_phase(self, provider, question: str,
                            plan: Dict[str, Any],
                            findings: List[Dict[str, Any]],
                            llm_config: Dict[str, Any],
                            ec: "ExecutionContext", hooks,
                            trace: Dict[str, Any]) -> str:
        """Write the report draft from the merged findings (structure and
        citations enforced by the prompt; the draft is NOT the reply —
        POLISH owns the final wording, so no deltas are forwarded)."""
        model = llm_config["model"]
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2048)

        findings_lines = []
        for i, f in enumerate(findings):
            findings_lines.append(
                f"[S{i + 1}] (工具: {f['tool']} | 查询: {f['query']})\n"
                f"{f['snippet']}")
        findings_block = ("\n".join(findings_lines)
                          or "(无资料——报告需明示证据不足)")

        themes = plan.get("themes") or [question]
        report_messages = [
            {"role": "system", "content": TOPIC_BASE_PROMPT},
            {"role": "user", "content": (
                f"用户问题:{question}\n\n研究主题:\n"
                + "\n".join(f"- {t}" for t in themes)
                + "\n\n" + REPORT_PROMPT_TEMPLATE.format(
                    findings_block=findings_block)
                + (f"\n\n【检索阶段小结】\n{trace.get('reflection_note') or ''}"
                   if trace.get("reflection_note") else ""))},
        ]

        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=ec.cxt.session_id,
                node_code=ec.node.code,
                round_idx=0, messages=report_messages, model=model))

        result = await _stream_round(
            provider, report_messages, model, temperature, max_tokens,
            None)  # draft deltas are NOT the reply
        draft = result.get("content", "") or ""

        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=ec.cxt.session_id,
                node_code=ec.node.code,
                round_idx=0, content=draft, tool_calls=[]))

        trace["phases"].append("report")
        _emit_round(ec.stream, "report", 0)
        return draft

    # ------------------------------------------------------------------
    # POLISH (format polish: the only phase forwarding deltas, terminal)
    # ------------------------------------------------------------------

    async def _polish_phase(self, provider, draft: str,
                            llm_config: Dict[str, Any],
                            ec: "ExecutionContext", hooks) -> str:
        """Beautify the draft's formatting (heading levels, emphasis,
        aligned source list — facts and [S1] citations untouched); report
        deltas are forwarded optimistically (this is the turn's reply)."""
        model = llm_config["model"]
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2048)

        polish_messages = [
            {"role": "system", "content": TOPIC_BASE_PROMPT},
            {"role": "user",
             "content": POLISH_PROMPT_TEMPLATE.format(draft=draft)},
        ]

        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=ec.cxt.session_id,
                node_code=ec.node.code,
                round_idx=0, messages=polish_messages, model=model))

        result = await _stream_round(
            provider, polish_messages, model, temperature, max_tokens,
            ec.stream)  # final-report deltas forwarded
        polished = result.get("content", "") or ""

        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=ec.cxt.session_id,
                node_code=ec.node.code,
                round_idx=0, content=polished, tool_calls=[]))

        _emit_round(ec.stream, "polish", 0)
        return polished


# ============================================================================
# The six node executors (one per graph station; each does
# "load state → run one station → save state → return the routing output")
# ============================================================================

class TrPreplanExecutor(TopicResearchExecutor):
    """tr_preplan node: research-state initialization + optional
    pre-retrieval (DeepResearchExecutor._preplan_phase reused verbatim);
    relays to tr_plan."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        pattern = ec.pattern
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)

        # P1 on_agent_start: fragments go into the PLAN base (carried by
        # the pipeline's first node; same contract as the default loop)
        hooks = resolve_agent_hooks(node, pattern)
        fragments = collect_fragments(
            hooks,
            AgentStartEvent(session_id=cxt.session_id,
                            node_code=node.code, cxt=cxt),
        ) if hooks else []

        state: Dict[str, Any] = {
            "question": _current_user_query(cxt),
            "phases": [],
            "degraded": False,
            "session_id": cxt.session_id,
            "node_code": node.code,
            "findings": [],
            "tool_stats": {},
            "plan": {},
        }

        # force_close defense: the step budget is exhausted, later
        # stations will not run — return the wrap-up text directly
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        # MCP timing gate: resolving tools before connections complete
        # would freeze allowed_names as an empty set
        await ensure_mcp_ready()
        tools = _resolve_tools(node, pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}

        base_messages = build_agent_messages(
            node, cxt, pattern=pattern, extra_blocks=fragments)
        warn_prompt_length(base_messages, cxt, node)

        plan_base, preplan_findings, preplan_stats = (
            await self._preplan_phase(
                provider, base_messages, tools, allowed_names,
                ec, hooks, state))

        state.update({
            "messages": plan_base,           # PLAN base (includes pre-retrieval context, if any)
            "base_messages": base_messages,  # rebuild base for the SEARCH workspace
            "findings": preplan_findings,
            "tool_stats": preplan_stats,
        })
        _save_state(cxt, state)
        # Intermediate stations return empty content + the conditional
        # edge; the graph's reply is the last non-empty content along the
        # run (tr_polish's)
        return TurnResult(content="", next=TR_PLAN_CODE)


class TrPlanExecutor(TopicResearchExecutor):
    """tr_plan node: the PLAN station (theme-splitting JSON,
    self-correcting retry, degradation fallback); dispatches one tr_search
    worker instance per theme via ``TurnResult.sends`` (the engine's
    runtime fan-out — this app's raison d'être)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _load_state(cxt)
        if state is None:
            # Orphan entry (no in-flight state): skip planning and bail to
            # the join — MERGE/REPORT will degrade to a no-material run
            state = _orphan_state(cxt, ec.node, "orphan_plan")
            _save_state(cxt, state)
            return TurnResult(content="", next=TR_MERGE_CODE)

        state["node_code"] = ec.node.code
        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(ec.node, ec.pattern)
        plan = await self._themes_phase(
            provider, state.get("messages") or [], cxt.llm_config or {},
            ec, hooks, state)

        # Dispatch: one worker instance per theme, capped at the pattern's
        # fan-out width (overflow is noted in the plan, not fatal)
        question = state.get("question", "")
        themes = plan.get("themes") or [question]
        max_fanout = ec.pattern.max_fanout
        if len(themes) > max_fanout:
            logger.warning(
                "[topic_research] 主题 %d 个超过 max_fanout=%d,"
                "仅研究前 %d 个",
                len(themes), max_fanout, max_fanout)
            plan["notes"] = (str(plan.get("notes", ""))
                             + f";主题数超过扇出宽度上限 {max_fanout},"
                               f"仅研究前 {max_fanout} 个").lstrip(";")
            themes = themes[:max_fanout]

        state["plan"] = plan
        _save_state(cxt, state)
        return TurnResult(content="", sends=[
            Send(TR_SEARCH_CODE, {"theme": question, "sub_question": t})
            for t in themes
        ])


class TrSearchExecutor(TopicResearchExecutor):
    """tr_search node — the fan-out WORKER: one instance researches ONE
    theme (``ec.branch_input`` carries {"theme", "sub_question"}) in a
    private workspace (DeepResearchExecutor._search_branch reused
    verbatim); results travel back exclusively via TurnResult.extra, which
    the engine settles into the ``__fanout_results__`` board for the join
    node (tr_merge)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        # Defensive main-path entry (no dispatch coordinates — unreachable
        # via the normal flow): behave like the old orphan, bail to the join
        if ec.branch_input is None:
            state = _load_state(cxt)
            if state is None:
                state = _orphan_state(cxt, ec.node, "orphan_search")
                _save_state(cxt, state)
            return TurnResult(content="", next=TR_MERGE_CODE)

        payload = ec.branch_input if isinstance(ec.branch_input, dict) else {}
        theme = str(payload.get("theme") or payload.get("sub_question") or "")
        sub_question = str(payload.get("sub_question") or theme)

        await ensure_mcp_ready()
        tools = _resolve_tools(ec.node, ec.pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}
        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(ec.node, ec.pattern)

        findings, stats = await self._search_branch(
            provider, theme, sub_question, tools, allowed_names, ec, hooks)
        return TurnResult(content="", extra={
            "sub_question": sub_question,
            "findings": findings,
            "tool_stats": stats["tool_stats"],
            "rounds": stats["rounds"],
            "reflection_note": stats["reflection_note"],
        })


class TrMergeExecutor(TopicResearchExecutor):
    """tr_merge node — the fan-out JOIN (barrier): a STRUCTURAL merge of
    the settled branches into the state board (zero LLM calls — the
    merged, renumbered [S1..Sn] material is what REPORT consumes); relays
    to tr_report."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _load_state(cxt)
        if state is None:
            state = _orphan_state(cxt, ec.node, "orphan_merge")

        state["node_code"] = ec.node.code

        merged = self._merge_phase(cxt, state)
        state.update({
            "findings": merged["findings"],
            "tool_stats": merged["tool_stats"],
            "merged": {
                "per_theme": merged["per_theme"],
                "reflection_note": merged["reflection_note"],
                "branches": {"total": merged["total"],
                             "failed": merged["failed"]},
            },
        })
        if merged["failed"]:
            state["degraded"] = True
        # The fan-out ran between PLAN and here — one "search" phase mark
        # for the whole barrier, then this station's own
        state.setdefault("phases", []).append("search")
        state["phases"].append("merge")
        _save_state(cxt, state)
        _emit_round(ec.stream, "merge", 0)
        return TurnResult(content="", next=TR_REPORT_CODE)


class TrReportExecutor(TopicResearchExecutor):
    """tr_report node: writes the report DRAFT from the merged findings
    (tool-less, no delta forwarding — the draft is stored in the state
    board for POLISH); relays to tr_polish."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _load_state(cxt)
        if state is None:
            state = _orphan_state(cxt, ec.node, "orphan_report")
            _save_state(cxt, state)

        state["node_code"] = ec.node.code
        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(ec.node, ec.pattern)

        draft = await self._report_phase(
            provider, state.get("question", ""),
            state.get("plan") or {}, state.get("findings") or [],
            cxt.llm_config or {}, ec, hooks, state)

        state["draft"] = draft
        _save_state(cxt, state)
        return TurnResult(content="", next=TR_POLISH_CODE)


class TrPolishExecutor(TopicResearchExecutor):
    """tr_polish node — terminal format-beautification station: streams
    the polished final report (the only delta-forwarding station, the
    turn's reply), writes the final trace into
    cxt.metadata["topic_research"]; is_end=True — the next turn re-enters
    the graph at the entry node."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        pattern = ec.pattern

        state = _load_state(cxt)
        if state is None:
            state = _orphan_state(cxt, node, "orphan_polish")

        state["node_code"] = node.code
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)
        hooks = resolve_agent_hooks(node, pattern)

        draft = state.get("draft")
        if not draft:
            if ec.force_close:
                return TurnResult(content=_FORCE_CLOSE_REPLY)
            draft = "(无报告草稿——研究流程异常或证据不足。)"
            state["degraded"] = True

        report = await self._polish_phase(
            provider, draft, llm_config, ec, hooks)

        merged = state.get("merged") or {}
        branches = merged.get("branches") or {"total": 0, "failed": 0}
        tool_stats: Dict[str, int] = dict(state.get("tool_stats") or {})
        rounds = sum(v.get("rounds", 0)
                     for v in (merged.get("per_theme") or {}).values())

        state["phases"].append("polish")
        trace = {
            "question": state.get("question", ""),
            "phases": state.get("phases", []),
            "degraded": bool(state.get("degraded")),
            "themes": (state.get("plan") or {}).get("themes", []),
            "sources": state.get("findings") or [],
            "tool_call_count": sum(tool_stats.values()),
            "tool_stats": tool_stats,
            "rounds": rounds,
            "reflection_note": merged.get("reflection_note", ""),
            "per_theme": merged.get("per_theme", {}),
            "branches": branches,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        cxt.metadata[_TRACE_KEY] = trace

        # P7 on_agent_end: the polished report is this turn's reply
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id,
                node_code=node.code,
                rounds=rounds + 3, outcome="reply", reply=report))

        _emit_round(ec.stream, "final", rounds)

        return TurnResult(content=report, extra={"topic_research": trace})


# ============================================================================
# Plugin registration — import side effect at the bottom (route.py imports
# this module at its end to complete registration)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", TR_PREPLAN_CODE, TrPreplanExecutor)
plugin_registry.register("executor", TR_PLAN_CODE, TrPlanExecutor)
plugin_registry.register("executor", TR_SEARCH_CODE, TrSearchExecutor)
plugin_registry.register("executor", TR_MERGE_CODE, TrMergeExecutor)
plugin_registry.register("executor", TR_REPORT_CODE, TrReportExecutor)
plugin_registry.register("executor", TR_POLISH_CODE, TrPolishExecutor)
