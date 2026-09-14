"""The four-node graph variant of the deep-research executor — one node per
research phase (plan-⑧ form; plan-⑨ migrates the SEARCH station to the
engine's runtime fan-out):

    dr_preplan ──next──> dr_plan ──sends──> dr_search ×N ──join──> dr_synthesize
     pre-retrieval/init    plan sub-questions  one per sub-question   merge & report

Phase inventory (the shared phase methods on DeepResearchExecutor):

    PREPLAN  one tool-carrying LLM call: the model itself decides whether
             to run a retrieval round first for background (no tool_calls =
             skip); retrieval results stay in messages for PLAN to lean on,
             findings merge into the join's findings
    PLAN     one tool-less LLM call → {"sub_questions": [...]} (fault-tolerant
             JSON extraction; on failure the bad output + error message are
             fed back into messages for a self-correcting retry, and a second
             failure degrades to [the original question]); then dispatches
             ONE worker instance per sub-question via ``TurnResult.sends``
             (capped at ``pattern.max_fanout``)
    SEARCH   a fan-out WORKER (plan-⑨ §5): one instance researches ONE
             sub-question in its own private workspace — a small tool-
             carrying ReAct loop (≤ _MAX_SEARCH_ROUNDS per instance) with a
             per-instance state board. Instance isolation structurally
             replaces the pre-fan-out coverage heuristics across a shared
             12-round loop; results travel back ONLY via TurnResult.extra
             (the engine settles them into the ``__fanout_results__`` board)
    SYNTHESIZE  the JOIN node (barrier): merges the state board's pre-
             retrieval findings with every branch's findings/tool_stats,
             then slims messages down and streams the report — the only
             phase that forwards text deltas to ec.stream

Plan-⑨ adaptations over the plan-⑧ form:

- the same-turn relay is ``TurnResult(content="", next=...)`` for PREPLAN/
  SYNTHESIZE-degraded, ``TurnResult(content="", sends=[Send(dr_search,
  payload), ...])`` for PLAN — the engine runs the instances concurrently
  (asyncio.gather; retrieval latency = slowest branch, not the sum) and
  executes this node as the join once all settle (a failed branch settles
  as an error entry and degrades the report, never killing the run);
- inter-phase state (question / workspace messages / pre-retrieval
  findings / plan) still travels via ``cxt.graph_state[
  "deep_research_state"]`` — workers see NONE of it (branch isolation:
  their cxt copy carries an empty graph_state/history), which is why the
  dispatch payload folds in the theme + sub-question;
- the final trace still goes to ``cxt.metadata["deep_research"]`` (same
  key, same shape + an additive ``branches`` summary) — observability /
  next-turn research continuation unaffected;
- the research process likewise never lands in cxt.history (the private
  workspace decision now doubly enforced: worker branches run on isolated
  cxt copies whose message_sink is cut); history keeps only the "user
  question → research report" Q/A pair;
- each tool-carrying node resolves its own tool surface (_resolve_tools:
  node.use_tools ∩ pattern.allow_toolset 工具集， both deny-by-default);
  intermediate phases forward no deltas — the only streaming phase is
  still SYNTHESIZE.

Zero-instance-state phase implementations: the four classes subclass
DeepResearchExecutor only to reuse its stateless phase methods
(_preplan_phase etc.; the plugin registry shares a single instance
anyway); each execute() does only "load state → run one phase → save
state → return the routing/dispatch output".
"""

import asyncio
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
    ToolCallEvent,
    ToolResultEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
    rewrite_tool_call,
    rewrite_tool_result,
)
from nexus.engine.chat import FANOUT_RESULTS_KEY
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.loop import (
    TurnResult,
    _execute_tool,
    _parse_args,
    _resolve_tools,
    warn_prompt_length,
)
from nexus.engine.messages import build_agent_messages
from nexus.engine.turn_result import Send
from nexus.llm.resolve import build_provider
from apps.deep_research_agent.prompts import (
    DEEP_RESEARCH_BASE_PROMPT,
    PLAN_PHASE_PROMPT,
    PLAN_RETRY_PROMPT,
    PREPLAN_SEARCH_PROMPT,
    SEARCH_STATE_BOARD_TMPL,
    SYNTHESIZE_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runaway protection budget (hard LLM call ceiling ≈ 1(pre-retrieval) + 1 +
# 1(self-correct retry) + max_fanout × per-branch SEARCH + 1; plan-⑨ trades
# the pre-fan-out single 12-round loop for N concurrent instances of a
# tighter per-branch cap — width × depth, each dimension independently
# bounded)
# ---------------------------------------------------------------------------

_MAX_SEARCH_ROUNDS = 5         # per-branch SEARCH round ceiling — the
                               # research-query budget (深度研究相关应用最多
                               # 调用 5 轮查询，覆盖 deep_research 与复用
                               # _search_branch 的 topic_research)
_QUERY_INTERVAL_SECONDS = 5.0  # pause after EVERY executed research query
                               # (rate limit; read at call time so tests can
                               # zero it via monkeypatch)
_PLAN_RETRIES = 1              # PLAN JSON parse-failure retry count
_PER_RESULT_CHARS = 4000       # per-tool-result truncation (workspace / findings)
_MAX_FINDINGS = 30             # findings entry ceiling (join-side FIFO across
                               # ALL branches + pre-retrieval)
_WORKSPACE_CHAR_BUDGET = 60000 # per-branch SEARCH workspace char budget (over
                               # budget: middle-truncate the oldest tool rows)

# Model-generalized tool name → canonical name normalization table (empirically verified:
# qwen3.8-flash writes web_search_prime as the generic name web_search). Flash-tier models have
# limited schema-name compliance; rather than wasting a round on "intercept → feed back correct name → self-correct",
# it's better to normalize in place before the hooks/guard — and since the assistant payload (history) keeps
# the canonical name, the model learns the correct name along the way. Only effective when the normalized
# result is actually in allowed_names; otherwise the interception guard proceeds as before.
_TOOL_NAME_ALIASES = {
    "web_search": "web_search_prime",
}

# State-board marks
_DONE_MARK = "✓"
_TODO_MARK = "○"

# ---------------------------------------------------------------------------
# Pipeline topology (node code = plugin code; route_multi.py's four nodes
# bind accordingly)
# ---------------------------------------------------------------------------

DR_PREPLAN_CODE = "dr_preplan"
DR_PLAN_CODE = "dr_plan"
DR_SEARCH_CODE = "dr_search"
DR_SYNTHESIZE_CODE = "dr_synthesize"

# In-flight research state key on the graph runtime's state board
# (cxt.graph_state: shared across the run's nodes, cleared by the runtime
# at graph termination); the final trace keeps the "deep_research" key
_STATE_KEY = "deep_research_state"
_TRACE_KEY = "deep_research"


def _normalize_tool_name(name: str, allowed_names: set) -> Tuple[str, bool]:
    """Generic tool name normalization: returns (canonical name, whether normalized).

    Returns as-is when the original name is available or the alias doesn't hit — misses
    still go through the interception guard, so genuinely nonexistent names won't be
    swallowed by mistake.
    """
    if name in allowed_names:
        return name, False
    mapped = _TOOL_NAME_ALIASES.get(name, "")
    if mapped and mapped in allowed_names:
        return mapped, True
    return name, False


def _load_state(cxt) -> Optional[Dict[str, Any]]:
    """Load the in-flight research state from the graph state board (a
    non-dict shape counts as none, guarding against dirty data)."""
    state = (cxt.graph_state or {}).get(_STATE_KEY)
    return state if isinstance(state, dict) else None


def _save_state(cxt, state: Dict[str, Any]) -> None:
    cxt.graph_state[_STATE_KEY] = state


def _orphan_state(cxt, node, phase: str) -> Dict[str, Any]:
    """Fallback state for an anomalous entry: with no in-flight state, go
    straight to wrap-up — SYNTHESIZE will emit an "insufficient evidence"
    report with the original question as the only sub-question — no
    pipeline node may deadlock for lack of state."""
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


_FORCE_CLOSE_REPLY = "(研究流程被强制收尾,未能完成研究。)"


class DeepResearchExecutor(NodeExecutor):
    """Stateless phase-method base of the four graph executors
    (PREPLAN → PLAN → SEARCH → SYNTHESIZE); the per-node execute()
    orchestration lives in the Dr* subclasses below."""

    # ------------------------------------------------------------------
    # PREPLAN
    # ------------------------------------------------------------------

    async def _preplan_phase(self, provider,
                             base_messages: List[Dict[str, Any]],
                             tools: List[Dict[str, Any]], allowed_names: set,
                             ec: "ExecutionContext", hooks,
                             trace: Dict[str, Any]
                             ) -> Tuple[List[Dict[str, Any]],
                                        List[Dict[str, Any]],
                                        Dict[str, int]]:
        """Optional pre-retrieval before PLAN: the model itself decides
        whether to run one search round for background information.

        One tool-carrying call (no follow-up rounds): with tool_calls →
        executed via _dispatch_research_round, results stay in messages for
        PLAN to reference, findings/tool_stats merge into SEARCH; without
        tool_calls → the model judged pre-retrieval unnecessary and it is
        skipped (the "skip" reply still enters messages, keeping the
        user/assistant alternation). No deltas forwarded (pre-retrieval is
        not the reply).

        Returns: (messages for PLAN, findings, tool_stats).
        """
        if not tools:
            return list(base_messages), [], {}

        cxt = ec.cxt
        node = ec.node
        model = (cxt.llm_config or {})["model"]
        temperature = (cxt.llm_config or {}).get("temperature", 0.7)
        max_tokens = (cxt.llm_config or {}).get("max_tokens", 2048)

        messages = list(base_messages) + [
            {"role": "user", "content": PREPLAN_SEARCH_PROMPT}]

        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=cxt.session_id, node_code=node.code,
                round_idx=0, messages=messages, model=model))

        result = await _stream_round(
            provider, messages, model, temperature, max_tokens,
            None, tools=tools)  # no delta forwarding
        content = result.get("content", "") or ""
        tool_calls = result.get("tool_calls", []) or []

        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=cxt.session_id, node_code=node.code,
                round_idx=0, content=content, tool_calls=tool_calls))

        findings: List[Dict[str, Any]] = []
        tool_stats: Dict[str, int] = {}
        if tool_calls:
            # round_idx=-1 → findings record round=0 (pre-retrieval marker,
            # distinguishing it from SEARCH rounds)
            findings = await self._dispatch_research_round(
                messages, tool_calls, hooks, allowed_names, -1,
                cxt, node, findings, tool_stats, stream=ec.stream)
            _truncate_workspace(messages)
            trace["phases"].append("preplan_search")
            _emit_round(ec.stream, "preplan", 0)
        else:
            messages.append({"role": "assistant", "content": content})
        return messages, findings, tool_stats

    # ------------------------------------------------------------------
    # PLAN
    # ------------------------------------------------------------------

    async def _plan_phase(self, provider, messages: List[Dict[str, Any]],
                          llm_config: Dict[str, Any],
                          ec: "ExecutionContext", hooks,
                          trace: Dict[str, Any]) -> Dict[str, Any]:
        """One tool-less LLM call producing the research plan (messages
        include the pre-retrieval context, if any).

        Fault-tolerant JSON extraction (the first balanced ``{...}`` block);
        on failure the messages are not re-sent verbatim — the bad output
        (assistant row) + parse error (user row) are fed back into messages
        so the model self-corrects from its own error up to _PLAN_RETRIES
        times; a final failure degrades to ``{"sub_questions": [the
        original question]}`` and marks degraded — a PLAN failure never
        blocks the research itself. No deltas forwarded (the plan JSON is
        not the reply).
        """
        cxt = ec.cxt
        node = ec.node
        model = llm_config["model"]
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2048)

        plan_messages = list(messages) + [
            {"role": "user", "content": PLAN_PHASE_PROMPT}]

        plan: Dict[str, Any] = {}
        for attempt in range(1 + _PLAN_RETRIES):
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
            plan, err = _extract_plan_json(content)
            if plan:
                break
            logger.warning("[deep_research] PLAN JSON 解析失败(第 %d 次): %s",
                           attempt + 1, err)
            if attempt < _PLAN_RETRIES:
                # Self-correcting retry: bad output + error fed back into
                # messages (not a verbatim re-send)
                plan_messages.append(
                    {"role": "assistant", "content": content or "(空输出)"})
                plan_messages.append(
                    {"role": "user",
                     "content": PLAN_RETRY_PROMPT.replace("{error}", err)})

        if not plan:
            plan = {"sub_questions": [trace.get("question", "")],
                    "notes": "规划降级:直接研究原问题"}
            trace["degraded"] = True

        trace["phases"].append("plan")
        trace["plan"] = plan
        _emit_round(ec.stream, "plan", 0)
        return plan

    # ------------------------------------------------------------------
    # SEARCH（fan-out worker：一个实例一个子问题，plan-⑨ §5）
    # ------------------------------------------------------------------

    async def _search_branch(self, provider, theme: str, sub_question: str,
                             tools: List[Dict[str, Any]], allowed_names: set,
                             ec: "ExecutionContext", hooks,
                             ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """One worker instance's research loop: ONE sub-question in a
        private messages workspace (never lands in cxt.history — and the
        branch cxt copy's history is empty by construction anyway).

        A per-instance state board (rounds left / findings / tool stats /
        the single sub-question's check-off) is rewritten into system each
        round; tool_calls are executed via _dispatch_research_round; no
        tool_calls means the model judged the information sufficient — its
        content becomes this branch's reflection note.

        Instance isolation structurally replaces the pre-fan-out coverage
        heuristics: there is exactly one sub-question to cover, and the
        engine runs the instances concurrently (latency = slowest branch).

        Returns: (findings, {"tool_stats", "rounds", "reflection_note"}) —
        everything travels back via TurnResult.extra; this method never
        touches the shared state board.
        """
        cxt = ec.cxt
        node = ec.node
        model = (cxt.llm_config or {})["model"]
        temperature = (cxt.llm_config or {}).get("temperature", 0.7)
        max_tokens = (cxt.llm_config or {}).get("max_tokens", 2048)

        findings: List[Dict[str, Any]] = []
        tool_stats: Dict[str, int] = {}
        reflection_note = ""
        rounds_done = 0

        if not tools:
            logger.warning(
                "[deep_research] 无可用工具(节点未声明可用工具或 toolset "
                "未授权 MCP 工具?),本分支跳过检索")
            return findings, {"tool_stats": tool_stats, "rounds": 0,
                              "reflection_note": ""}

        # Initial workspace shape: system (base + 子任务框定 + 状态板) +
        # user (the sub-question itself)
        sub_questions = [sub_question]
        covered = _covered_questions(sub_questions, findings)
        question_lines = [
            f"1. {_DONE_MARK if sub_question in covered else _TODO_MARK}"
            f" {sub_question}"]
        board = SEARCH_STATE_BOARD_TMPL.format(
            question_lines="\n".join(question_lines),
            rounds_left=_MAX_SEARCH_ROUNDS, total_rounds=_MAX_SEARCH_ROUNDS,
            findings_count=len(findings),
            tool_stats=json.dumps(tool_stats, ensure_ascii=False))
        workspace: List[Dict[str, Any]] = [
            {"role": "system",
             "content": (
                 f"{DEEP_RESEARCH_BASE_PROMPT}\n\n【研究子任务】\n"
                 f"你是一个并行检索实例,只负责研究主题「{theme}」下的"
                 f"这一个子问题,不要展开其他子问题:\n1. {sub_question}"
                 + board)},
            {"role": "user", "content": sub_question},
        ]

        for round_idx in range(_MAX_SEARCH_ROUNDS):
            rounds_done = round_idx + 1
            # State-board rewrite (the sub-question gets its check mark once
            # its keywords show up in findings)
            covered = _covered_questions(sub_questions, findings)
            question_lines = [
                f"1. {_DONE_MARK if sub_question in covered else _TODO_MARK}"
                f" {sub_question}"]
            _rewrite_state_board(
                workspace, sub_questions, question_lines,
                _MAX_SEARCH_ROUNDS - round_idx, _MAX_SEARCH_ROUNDS,
                len(findings), tool_stats)

            if hooks:
                fire(hooks, "on_llm_call", LLMCallEvent(
                    session_id=cxt.session_id,
                    node_code=node.code,
                    round_idx=round_idx, messages=workspace, model=model))

            result = await _stream_round(
                provider, workspace, model, temperature, max_tokens,
                None, tools=tools)  # intermediate rounds forward no deltas
            content = result.get("content", "") or ""
            tool_calls = result.get("tool_calls", []) or []

            if hooks:
                fire(hooks, "on_llm_response", LLMResponseEvent(
                    session_id=cxt.session_id,
                    node_code=node.code,
                    round_idx=round_idx, content=content,
                    tool_calls=tool_calls))

            if not tool_calls:
                # Model judged the information sufficient → wrap up; the
                # content becomes this branch's reflection note
                reflection_note = content
                break

            new_findings = await self._dispatch_research_round(
                workspace, tool_calls, hooks, allowed_names, round_idx,
                cxt, node, findings, tool_stats, stream=ec.stream)
            findings.extend(new_findings)
            if len(findings) > _MAX_FINDINGS:
                findings = findings[len(findings) - _MAX_FINDINGS:]
            _truncate_workspace(workspace)
            _emit_round(ec.stream, "search", round_idx)
        else:
            logger.info(
                "[deep_research] SEARCH 分支达到最大轮次 %d,收束(子问题: %s)",
                _MAX_SEARCH_ROUNDS, sub_question)

        return findings, {
            "tool_stats": tool_stats,
            "rounds": rounds_done,
            "reflection_note": reflection_note,
        }

    async def _dispatch_research_round(
            self, messages: List[Dict[str, Any]], tool_calls: List[Dict[str, Any]],
            hooks, allowed_names: set, round_idx: int,
            cxt, node, findings: List[Dict[str, Any]],
            tool_stats: Dict[str, int], stream=None) -> List[Dict[str, Any]]:
        """Tool dispatch of a research round (the private-workspace version
        of _dispatch_tool_calls).

        Same semantics as nexus.engine.loop._dispatch_tool_calls (P4 chained
        rewrite → allowed_names validation (illegal ones get an error fed
        back) → _execute_tool → P5 rewrite), but it **only appends to the
        executor's private messages, never writes cxt.history**; successful
        tool results are also collected into findings (truncated + query
        recorded). Each issued / returned call is forwarded as trace events
        (tool_call / tool_result, same vocabulary/data keys as the kernel
        path) — inside a fan-out branch the engine hands a
        BranchStreamEmitter, so emissions carry the branch_id automatically.

        Returns: the findings entries added this round.
        """
        session_id = cxt.session_id
        node_code = node.code
        _emit = getattr(stream, "emit_trace", None)

        # P4 chained rewrite (applied back to tc, single source of truth)
        rewrite_audits = {}
        for idx, tc in enumerate(tool_calls):
            name = tc.get("function", {}).get("name", "")
            # Generic-name normalization comes before hooks/guard: the tc and the
            # assistant payload both keep the canonical name, so the history also "teaches" the
            # model the correct name (hooks observe the name that is actually dispatched)
            canonical, aliased = _normalize_tool_name(name, allowed_names)
            if aliased:
                logger.info("[deep_research] Tool name normalized: %s → %s",
                            name, canonical)
                tc["function"]["name"] = canonical
                name = canonical
            parsed_args = _parse_args(tc)
            if hooks:
                event = ToolCallEvent(
                    session_id=session_id, node_code=node_code,
                    round_idx=round_idx, tool_name=name, args=parsed_args)
                final_name, final_args, original = rewrite_tool_call(
                    hooks, event, allowed_names)
            else:
                final_name, final_args, original = name, parsed_args, None
            if final_name != name:
                tc["function"]["name"] = final_name
            if final_args is not parsed_args:
                try:
                    tc["function"]["arguments"] = json.dumps(
                        final_args, ensure_ascii=False)
                except (TypeError, ValueError) as e:
                    logger.warning("[hooks] 改写后 args 无法序列化,保留原串: %s", e)
            if original is not None:
                rewrite_audits[idx] = original

        # assistant payload (protocol pairing: ids untouched)
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": tool_calls})

        # Per tc: validate → execute / feed back error → P5 → append the tool row
        new_findings: List[Dict[str, Any]] = []
        for idx, tc in enumerate(tool_calls):
            name = tc.get("function", {}).get("name", "")
            call_id = tc.get("id", "")
            parsed_args = _parse_args(tc)

            if _emit is not None:
                _emit("tool_call", node_code=node_code, call_id=call_id,
                      tool_name=name, args=parsed_args, round_idx=round_idx)

            synthetic = False
            if name not in allowed_names:
                logger.warning(
                    "[deep_research] 工具 '%s' 不在本轮可用集合,拦截不执行", name)
                synthetic = True
                result_content = json.dumps({
                    "error": (f"工具 '{name}' 不存在或本轮不可用。"
                              f"可用工具:{sorted(allowed_names)}。")
                }, ensure_ascii=False)
            else:
                tool_result = await _execute_tool(name, parsed_args)
                if hooks:
                    event = ToolResultEvent(
                        session_id=session_id, node_code=node_code,
                        round_idx=round_idx, tool_name=name,
                        tool_call_id=call_id, result=tool_result)
                    tool_result, _orig = rewrite_tool_result(hooks, event)
                result_content = tool_result

                # findings collection (success path; error JSON never enters findings)
                if not result_content.lstrip().startswith("{\"error"):
                    query = (parsed_args.get("search_query")
                             or parsed_args.get("query")
                             or parsed_args.get("q")
                             or parsed_args.get("url")
                             or json.dumps(parsed_args, ensure_ascii=False))
                    new_findings.append({
                        "tool": name,
                        "query": str(query)[:200],
                        "snippet": result_content[:_PER_RESULT_CHARS],
                        "round": round_idx + 1,
                    })
                    tool_stats[name] = tool_stats.get(name, 0) + 1

            if _emit is not None:
                _emit("tool_result", node_code=node_code, call_id=call_id,
                      tool_name=name, result=result_content,
                      round_idx=round_idx, synthetic=synthetic)

            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": result_content})

            # Query rate limit: pause after every actually-executed research
            # query (synthetic error feedback is not a query; concurrent
            # branches each pause their own queries, so the wall-clock gap
            # per branch stays _QUERY_INTERVAL_SECONDS)
            if not synthetic:
                await asyncio.sleep(_QUERY_INTERVAL_SECONDS)

        return new_findings

    # ------------------------------------------------------------------
    # SYNTHESIZE
    # ------------------------------------------------------------------

    async def _synthesize_phase(self, provider, user_query: str,
                                plan: Dict[str, Any],
                                findings: List[Dict[str, Any]],
                                llm_config: Dict[str, Any],
                                ec: "ExecutionContext", hooks,
                                trace: Dict[str, Any]) -> str:
        """Slim the messages down and stream the report (the only
        delta-forwarding phase)."""
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

        sub_questions = plan.get("sub_questions") or [user_query]
        synth_messages = [
            {"role": "system", "content": DEEP_RESEARCH_BASE_PROMPT},
            {"role": "user", "content": (
                f"用户问题:{user_query}\n\n研究子问题:\n"
                + "\n".join(f"- {q}" for q in sub_questions)
                + "\n\n" + SYNTHESIZE_PROMPT_TEMPLATE.format(
                    findings_block=findings_block)
                + (f"\n\n【检索阶段小结】\n{trace.get('reflection_note') or ''}"
                   if trace.get("reflection_note") else ""))},
        ]

        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=ec.cxt.session_id,
                node_code=ec.node.code,
                round_idx=0, messages=synth_messages, model=model))

        result = await _stream_round(
            provider, synth_messages, model, temperature, max_tokens,
            ec.stream)  # report deltas forwarded optimistically
        report = result.get("content", "") or ""

        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=ec.cxt.session_id,
                node_code=ec.node.code,
                round_idx=0, content=report, tool_calls=[]))

        trace["phases"].append("synthesize")
        _emit_round(ec.stream, "synthesize", 0)
        return report


# ============================================================================
# The four node executors (one per graph station; each does
# "load state → run one phase → save state → return the routing output")
# ============================================================================

class DrPreplanExecutor(DeepResearchExecutor):
    """dr_preplan node: research-state initialization + the PREPLAN phase
    (optional pre-retrieval); relays to dr_plan."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        pattern = ec.pattern
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)

        # P1 on_agent_start: fragments go into the PLAN base (carried by the
        # pipeline's first node; same contract as the default loop)
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

        # force_close defense: the step budget is exhausted, later stations
        # will not run — return the wrap-up text directly (no next → the
        # graph terminates on this node)
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        # MCP timing gate: resolving tools before connections complete would
        # freeze allowed_names as an empty set; wait for the final state
        # here (zero overhead when no server is configured)
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
        # Intermediate stations return empty content + the conditional edge;
        # the graph's reply is the last non-empty content along the run
        return TurnResult(content="", next=DR_PLAN_CODE)


class DrPlanExecutor(DeepResearchExecutor):
    """dr_plan node: the PLAN phase (sub-question JSON, self-correcting
    retry, degradation fallback); dispatches one dr_search worker instance
    per sub-question via ``TurnResult.sends`` (plan-⑨ runtime fan-out)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _load_state(cxt)
        if state is None:
            state = _orphan_state(cxt, ec.node, "orphan_plan")
            _save_state(cxt, state)
            return TurnResult(content="", next=DR_SYNTHESIZE_CODE)

        state["node_code"] = ec.node.code
        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(ec.node, ec.pattern)
        plan = await self._plan_phase(
            provider, state.get("messages") or [], cxt.llm_config or {},
            ec, hooks, state)

        # Dispatch: one worker instance per sub-question, capped at the
        # pattern's fan-out width (overflow is noted in the plan, not fatal)
        question = state.get("question", "")
        sub_questions = plan.get("sub_questions") or [question]
        max_fanout = ec.pattern.max_fanout
        if len(sub_questions) > max_fanout:
            logger.warning(
                "[deep_research] 子问题 %d 个超过 max_fanout=%d,"
                "仅研究前 %d 个",
                len(sub_questions), max_fanout, max_fanout)
            plan["notes"] = (str(plan.get("notes", ""))
                             + f";子问题数超过扇出宽度上限 {max_fanout},"
                               f"仅研究前 {max_fanout} 个").lstrip(";")
            sub_questions = sub_questions[:max_fanout]

        state["plan"] = plan
        _save_state(cxt, state)
        return TurnResult(content="", sends=[
            Send(DR_SEARCH_CODE, {"theme": question, "sub_question": q})
            for q in sub_questions
        ])


class DrSearchExecutor(DeepResearchExecutor):
    """dr_search node — the fan-out WORKER (plan-⑨ §5): one instance
    researches ONE sub-question (``ec.branch_input`` carries
    {"theme", "sub_question"}) in a private workspace; results travel back
    exclusively via TurnResult.extra, which the engine settles into the
    ``__fanout_results__`` board for the join node (dr_synthesize)."""

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
            return TurnResult(content="", next=DR_SYNTHESIZE_CODE)

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


class DrSynthesizeExecutor(DeepResearchExecutor):
    """dr_synthesize node — the fan-out JOIN (barrier): merges the state
    board's pre-retrieval findings with every settled branch's
    findings/tool_stats/notes, then streams the report (the only streaming
    phase; terminal: no next, is_end=True — the next turn re-enters the
    graph at the entry node)."""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        pattern = ec.pattern

        state = _load_state(cxt)
        if state is None:
            # Defensive (unreachable via the normal flow): wrap up with the
            # existing trace's sources / no material
            prior = _prior_trace(cxt)
            state = _orphan_state(cxt, node, "orphan_synthesize")
            if prior:
                state["findings"] = list(prior.get("sources", []))
                state["tool_stats"] = dict(prior.get("tool_stats", {}))
                state["degraded"] = bool(prior.get("degraded", False))

        state["node_code"] = node.code
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)
        hooks = resolve_agent_hooks(node, pattern)

        plan = state.get("plan") or {}

        # ---- Join: merge the fan-out results board --------------------
        # (entries: {branch_id, node_code, ok, content, extra, error?};
        # a failed branch degrades the report, never blocks it)
        board = [e for e in (cxt.graph_state.get(FANOUT_RESULTS_KEY) or [])
                 if isinstance(e, dict)]
        findings: List[Dict[str, Any]] = list(state.get("findings") or [])
        tool_stats: Dict[str, int] = dict(state.get("tool_stats") or {})
        rounds = 0
        notes: List[str] = []
        failed_branches = sum(1 for e in board if not e.get("ok"))
        for entry in board:
            if not entry.get("ok"):
                continue
            ex = entry.get("extra") or {}
            findings.extend(ex.get("findings") or [])
            for name, cnt in (ex.get("tool_stats") or {}).items():
                tool_stats[name] = tool_stats.get(name, 0) + cnt
            rounds += int(ex.get("rounds") or 0)
            if ex.get("reflection_note"):
                notes.append(f"[{ex.get('sub_question', '')}] "
                             f"{ex['reflection_note']}")
        if len(findings) > _MAX_FINDINGS:
            findings = findings[len(findings) - _MAX_FINDINGS:]
        reflection_note = "\n".join(notes)
        degraded = bool(state.get("degraded")) or failed_branches > 0
        if failed_branches:
            logger.warning(
                "[deep_research] 扇出分支失败 %d/%d,报告将基于部分资料",
                failed_branches, len(board))

        # The fan-out ran between PLAN and here — one "search" phase mark
        # for the whole barrier
        state.setdefault("phases", []).append("search")

        report = await self._synthesize_phase(
            provider, state.get("question", ""), plan, findings,
            llm_config, ec, hooks, {
                # _synthesize_phase appends "synthesize" onto phases and
                # reads reflection_note; the authoritative trace below is
                # composed from the state + join merge above
                "phases": state["phases"],
                "reflection_note": reflection_note,
            })

        # Final trace: legacy key shape + the additive branches summary
        trace = {
            "question": state.get("question", ""),
            "phases": state.get("phases", []),
            "degraded": degraded,
            "sub_questions": plan.get("sub_questions", []),
            "sources": findings,
            "tool_call_count": sum(tool_stats.values()),
            "tool_stats": tool_stats,
            "rounds": rounds,
            "reflection_note": reflection_note,
            "branches": {"total": len(board), "failed": failed_branches},
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        cxt.metadata[_TRACE_KEY] = trace

        # P7 on_agent_end: the report is this turn's reply
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id,
                node_code=node.code,
                rounds=rounds + 2, outcome="reply", reply=report))

        _emit_round(ec.stream, "final", rounds)

        return TurnResult(content=report, extra={"deep_research": trace})


# ============================================================================
# Module-level helpers
# ============================================================================

def _current_user_query(cxt) -> str:
    """This turn's user question (the last user message in cxt.history;
    empty string as fallback).

    history is a list of SessionMessage objects (role/content attributes);
    the user row at turn start was already written by the chat layer, so
    take the most recent from the end.
    """
    for m in reversed(cxt.history or []):
        if getattr(m, "role", "") == "user":
            return getattr(m, "content", "") or ""
    return ""


def _prior_trace(cxt) -> Optional[Dict[str, Any]]:
    """The previous turn's research trace (cxt.metadata; for continuation /
    force_close)."""
    prior = (cxt.metadata or {}).get(_TRACE_KEY)
    return prior if isinstance(prior, dict) else None


def _extract_plan_json(content: str) -> Tuple[Dict[str, Any], str]:
    """Fault-tolerant PLAN JSON extraction: first balanced ``{...}`` block →
    parse → validate sub_questions is a non-empty list of strings.

    Returns: (plan, err) — on success plan is non-empty and err an empty
    string; on failure plan is an empty dict and err a model-facing
    self-correction description (fed into PLAN_RETRY_PROMPT).
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
    questions = data.get("sub_questions")
    if (not isinstance(questions, list)
            or not questions
            or not all(isinstance(q, str) and q.strip() for q in questions)):
        return {}, "缺少合法的 sub_questions 字段(需非空字符串数组)"
    return {"sub_questions": [q.strip() for q in questions],
            "notes": str(data.get("notes", ""))}, ""


def _covered_questions(sub_questions: List[str],
                       findings: List[Dict[str, Any]]) -> set:
    """The set of covered sub-questions (a sub-question counts once any of
    its keywords shows up in some finding's query; a simple heuristic, good
    enough for the state board)."""
    covered = set()
    for q in sub_questions:
        keywords = [w for w in re.split(r"[\s,，。?？]+", q) if len(w) >= 2]
        for f in findings:
            query = f.get("query", "")
            if any(k in query for k in keywords):
                covered.add(q)
                break
    return covered


def _rewrite_state_board(workspace: List[Dict[str, Any]],
                         sub_questions: List[str],
                         question_lines: List[str],
                         rounds_left: int, total_rounds: int,
                         findings_count: int,
                         tool_stats: Dict[str, int]) -> None:
    """Rewrite the state-board section of system[0] (the board is always
    system's last section)."""
    board = SEARCH_STATE_BOARD_TMPL.format(
        question_lines="\n".join(question_lines),
        rounds_left=rounds_left, total_rounds=total_rounds,
        findings_count=findings_count,
        tool_stats=json.dumps(tool_stats, ensure_ascii=False))
    system = workspace[0]
    content = system.get("content", "") or ""
    idx = content.find("\n\n【研究状态板】")
    if idx >= 0:
        content = content[:idx]
    system["content"] = content + board


def _truncate_workspace(workspace: List[Dict[str, Any]],
                        budget: int = _WORKSPACE_CHAR_BUDGET) -> None:
    """When the workspace exceeds budget, middle-truncate the oldest tool
    rows (keep head and tail)."""
    total = sum(len(str(m.get("content", "") or "")) for m in workspace)
    for m in workspace:
        if total <= budget:
            return
        if m.get("role") == "tool" and not m.get("_truncated"):
            content = str(m.get("content", "") or "")
            if len(content) > 800:
                removed = len(content) - 800
                m["content"] = (
                    f"{content[:400]}\n...[工作区超预算,已截断 {removed} 字符]...\n"
                    f"{content[-400:]}")
                m["_truncated"] = True
                total -= removed


# ============================================================================
# Plugin registration — import side effect at the bottom (route_multi.py
# imports this module at its end to complete registration)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", DR_PREPLAN_CODE, DrPreplanExecutor)
plugin_registry.register("executor", DR_PLAN_CODE, DrPlanExecutor)
plugin_registry.register("executor", DR_SEARCH_CODE, DrSearchExecutor)
plugin_registry.register("executor", DR_SYNTHESIZE_CODE, DrSynthesizeExecutor)
