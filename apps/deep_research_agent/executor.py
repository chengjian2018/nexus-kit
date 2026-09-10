"""DeepResearchExecutor — kind="executor" / code="deep_research" 的插件实现。

结构化研究循环(deep research = 一次 execute 内完成全部相位):

    PLAN    一次无工具 LLM 调用 → {"sub_questions": [...]}(JSON 容错提取,
            失败重试,再失败降级为 [原问题])
    SEARCH  带工具 ReAct 循环(≤ _MAX_SEARCH_ROUNDS):每轮把「研究状态板」
            重写进 system(子问题勾选进度 / 剩余轮次 / 命中统计),模型据此
            决策继续检索还是收工;无 tool_calls 即收工信号(REFLECT 并入
            SEARCH,不设独立相位)
    SYNTHESIZE  精简 messages(报告指令 + 问题/计划/findings 汇编)流式生成
            报告 —— 唯一把 text delta 转发给 ec.stream 的相位

与 DefaultLoopExecutor 的关键差异(也是不复用 _dispatch_tool_calls 的原因):
研究过程住在 executor 私有 messages 工作区,**不落 cxt.history**——几十条
tool rows 进历史会撑爆下一轮 prompt(压缩只在轮首触发,救不了当轮);对话
历史里只留下「用户问题 → 研究报告」的 Q/A 对。结构化 trace 写
``cxt.metadata["deep_research"]`` 供观测与下一轮续研。
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from atoms.tools.mcp_tool import ensure_mcp_ready

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
from nexus.engine.execution import ExecutionContext, ModuleExecutor
from nexus.engine.loop import (
    TurnResult,
    _execute_tool,
    _parse_args,
    _resolve_tools,
    warn_prompt_length,
)
from nexus.engine.messages import build_agent_messages
from nexus.llm.resolve import build_provider
from apps.deep_research_agent.prompts import (
    DEEP_RESEARCH_BASE_PROMPT,
    PLAN_ANCHOR,
    PLAN_PHASE_PROMPT,
    SEARCH_STATE_BOARD_TMPL,
    SYNTHESIZE_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 防失控预算(总 LLM 调用硬上限 ≈ 1 + 1(重试) + 12 + 1 = 15)
# ---------------------------------------------------------------------------

_MAX_SEARCH_ROUNDS = 12        # SEARCH 相位轮次上限
_PLAN_RETRIES = 1              # PLAN JSON 解析失败重试次数
_PER_RESULT_CHARS = 4000       # 单条 tool result 截断(进工作区 / findings)
_MAX_FINDINGS = 30             # findings 条数上限(FIFO 淘汰最旧)
_WORKSPACE_CHAR_BUDGET = 60000 # SEARCH 工作区字符预算(超限中段截断最旧 tool 行)

# 状态板标记
_DONE_MARK = "✓"
_TODO_MARK = "○"


class DeepResearchExecutor(ModuleExecutor):
    """deep_research 模块的结构化研究 executor(PLAN → SEARCH → SYNTHESIZE)。"""

    def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        module = ec.module
        pattern = ec.pattern
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)

        hooks = resolve_agent_hooks(module, pattern)

        # P1 on_agent_start:片段进 PLAN 底座(契约同 default loop)
        fragments = collect_fragments(
            hooks,
            AgentStartEvent(session_id=cxt.session_id,
                            module_code=module.module_code, cxt=cxt),
        ) if hooks else []

        user_query = _current_user_query(cxt)
        trace: Dict[str, Any] = {
            "question": user_query,
            "phases": [],
            "degraded": False,
        }

        # force_close(单模块 pattern 实际不可达,防御性):跳过研究相位,
        # 用既有 findings 直接收尾
        findings: List[Dict[str, Any]] = []
        plan: Dict[str, Any] = {}
        if ec.force_close:
            trace["phases"].append("force_close")
            prior = _prior_trace(cxt)
            findings = list(prior.get("sources", [])) if prior else []
        else:
            # MCP server 是启动期后台异步注册的:首轮对话若抢在连接完成
            # 之前,allowed_names 会被冻结成空集,后续轮次模型引用工具即
            # 触发"不在本轮可用集合"拦截(模型经 mcp_list_tools 能看到工
            # 具名)。这里在解析工具前等待 MCP 连接终态——未配置 server
            # 时零开销,已就绪时立即返回
            ensure_mcp_ready()
            tools = _resolve_tools(module, pattern)
            allowed_names = {t.get("function", {}).get("name", "")
                             for t in tools}

            base_messages = build_agent_messages(
                module, cxt, pattern=pattern, extra_blocks=fragments)
            warn_prompt_length(base_messages, cxt, module)

            # ---- PLAN ----
            plan = self._plan_phase(
                provider, base_messages, llm_config, hooks, trace)

            # ---- SEARCH(含反思状态板)----
            findings, search_stats = self._search_phase(
                provider, base_messages, plan, tools, allowed_names,
                ec, hooks, trace)

        # ---- SYNTHESIZE ----
        report = self._synthesize_phase(
            provider, user_query, plan, findings, llm_config, ec, hooks, trace)

        trace.update({
            "sub_questions": plan.get("sub_questions", []),
            "sources": findings,
            "tool_call_count": sum(
                (search_stats or {}).get("tool_stats", {}).values())
            if not ec.force_close else len(findings),
            "tool_stats": (search_stats or {}).get("tool_stats", {}),
            "rounds": (search_stats or {}).get("rounds", 0),
            "reflection_note": (search_stats or {}).get("reflection_note", ""),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        cxt.metadata["deep_research"] = trace

        # P7 on_agent_end:报告即本轮回复
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id,
                module_code=module.module_code,
                rounds=trace["rounds"] + 2, outcome="reply", reply=report))

        _emit_round(ec.stream, "final", trace["rounds"])
        return TurnResult(content=report, extra={"deep_research": trace})

    # ------------------------------------------------------------------
    # PLAN
    # ------------------------------------------------------------------

    def _plan_phase(self, provider, messages: List[Dict[str, Any]],
                    llm_config: Dict[str, Any], hooks,
                    trace: Dict[str, Any]) -> Dict[str, Any]:
        """一次无工具 LLM 调用产研究计划。

        JSON 容错提取(首个 ``{...}`` 平衡块);失败重试 _PLAN_RETRIES 次,
        仍失败降级为 ``{"sub_questions": [原问题]}`` 并标记 degraded——
        PLAN 失败绝不阻塞研究本身。不转发 delta(计划 JSON 不是回复)。
        """
        model = llm_config["model"]
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2048)

        plan_messages = list(messages) + [
            {"role": "user", "content": PLAN_PHASE_PROMPT}]

        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=trace.get("session_id", ""),
                module_code=trace.get("module_code", ""),
                round_idx=0, messages=plan_messages, model=model))

        plan: Dict[str, Any] = {}
        for attempt in range(1 + _PLAN_RETRIES):
            result = _stream_round(
                provider, plan_messages, model, temperature, max_tokens,
                None)  # 不转发 delta
            content = result.get("content", "") or ""
            if hooks:
                fire(hooks, "on_llm_response", LLMResponseEvent(
                    session_id=trace.get("session_id", ""),
                    module_code=trace.get("module_code", ""),
                    round_idx=0, content=content, tool_calls=[]))
            plan = _extract_plan_json(content)
            if plan:
                break
            logger.warning("[deep_research] PLAN JSON 解析失败(第 %d 次)",
                           attempt + 1)

        if not plan:
            plan = {"sub_questions": [trace.get("question", "")],
                    "notes": "规划降级:直接研究原问题"}
            trace["degraded"] = True

        trace["phases"].append("plan")
        trace["plan"] = plan
        _emit_round(None, "plan", 0)
        return plan

    # ------------------------------------------------------------------
    # SEARCH
    # ------------------------------------------------------------------

    def _search_phase(self, provider, base_messages: List[Dict[str, Any]],
                      plan: Dict[str, Any], tools: List[Dict[str, Any]],
                      allowed_names: set, ec: "ExecutionContext", hooks,
                      trace: Dict[str, Any]
                      ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """带工具研究循环:私有 messages 工作区(不落 cxt.history)。

        每轮开始重写 system[0] 的状态板段;tool_calls 经
        _dispatch_research_round 执行并追加协议对;无 tool_calls 即模型
        判定信息足够,content 留作 reflection_note 进 SYNTHESIZE。
        """
        cxt = ec.cxt
        module = ec.module
        model = (cxt.llm_config or {})["model"]
        temperature = (cxt.llm_config or {}).get("temperature", 0.7)
        max_tokens = (cxt.llm_config or {}).get("max_tokens", 2048)

        user_query = trace["question"]
        sub_questions = plan.get("sub_questions") or [user_query]

        # 工作区初始形态:system(角色 + 计划 + 状态板占位)+ user(原问题)
        system_base = _system_content(base_messages)
        question_lines = [
            f"{i + 1}. {_TODO_MARK} {q}" for i, q in enumerate(sub_questions)]
        board = SEARCH_STATE_BOARD_TMPL.format(
            question_lines="\n".join(question_lines),
            rounds_left=_MAX_SEARCH_ROUNDS, total_rounds=_MAX_SEARCH_ROUNDS,
            findings_count=0, tool_stats="{}")
        workspace: List[Dict[str, Any]] = [
            {"role": "system",
             "content": f"{system_base}\n\n【研究计划】\n" + "\n".join(
                 f"{i + 1}. {q}" for i, q in enumerate(sub_questions)) + board},
            {"role": "user", "content": user_query},
        ]

        findings: List[Dict[str, Any]] = []
        tool_stats: Dict[str, int] = {}
        reflection_note = ""
        rounds_done = 0

        if tools:
            for round_idx in range(_MAX_SEARCH_ROUNDS):
                rounds_done = round_idx + 1
                # 状态板重写(已完成子问题勾选:该子问题的查询出现在
                # findings 里即视为已覆盖)
                covered = _covered_questions(sub_questions, findings)
                question_lines = [
                    f"{i + 1}. {_DONE_MARK if q in covered else _TODO_MARK} {q}"
                    for i, q in enumerate(sub_questions)]
                _rewrite_state_board(
                    workspace, sub_questions, question_lines,
                    _MAX_SEARCH_ROUNDS - round_idx, _MAX_SEARCH_ROUNDS,
                    len(findings), tool_stats)

                if hooks:
                    fire(hooks, "on_llm_call", LLMCallEvent(
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        round_idx=round_idx, messages=workspace, model=model))

                result = _stream_round(
                    provider, workspace, model, temperature, max_tokens,
                    None, tools=tools)  # 中间轮不转发 delta
                content = result.get("content", "") or ""
                tool_calls = result.get("tool_calls", []) or []

                if hooks:
                    fire(hooks, "on_llm_response", LLMResponseEvent(
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        round_idx=round_idx, content=content,
                        tool_calls=tool_calls))

                if not tool_calls:
                    # 模型判定信息足够 → 收工,content 作反思笔记
                    reflection_note = content
                    break

                new_findings = self._dispatch_research_round(
                    workspace, tool_calls, hooks, allowed_names, round_idx,
                    cxt, module, findings, tool_stats)
                findings.extend(new_findings)
                if len(findings) > _MAX_FINDINGS:
                    findings = findings[len(findings) - _MAX_FINDINGS:]
                _truncate_workspace(workspace)
                _emit_round(ec.stream, "search", round_idx)
            else:
                logger.info(
                    "[deep_research] SEARCH 达到最大轮次 %d,进入综合",
                    _MAX_SEARCH_ROUNDS)
        else:
            logger.warning(
                "[deep_research] 无可用工具(pattern ACL 未授权任何 MCP 工具?),"
                "跳过 SEARCH 直接综合")

        trace["phases"].append("search")
        return findings, {
            "tool_stats": tool_stats,
            "rounds": rounds_done,
            "reflection_note": reflection_note,
        }

    def _dispatch_research_round(
            self, messages: List[Dict[str, Any]], tool_calls: List[Dict[str, Any]],
            hooks, allowed_names: set, round_idx: int,
            cxt, module, findings: List[Dict[str, Any]],
            tool_stats: Dict[str, int]) -> List[Dict[str, Any]]:
        """研究轮的工具分派(私有工作区版 _dispatch_tool_calls)。

        与 nexus.engine.loop._dispatch_tool_calls 同语义(P4 链式改写 →
        allowed_names 校验(不合法回填错误)→ _execute_tool → P5 改写),
        但**只 append 到 executor 私有 messages,不写 cxt.history**;同时
        把成功的工具结果收进 findings(截断 + 记录查询词)。

        Returns: 本轮新增的 findings 条目。
        """
        session_id = cxt.session_id
        module_code = module.module_code

        # P4 链式改写(应用回 tc,单一事实源)
        rewrite_audits = {}
        for idx, tc in enumerate(tool_calls):
            name = tc.get("function", {}).get("name", "")
            parsed_args = _parse_args(tc)
            if hooks:
                event = ToolCallEvent(
                    session_id=session_id, module_code=module_code,
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

        # assistant 载荷(协议配对:id 不改)
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": tool_calls})

        # 逐 tc:校验 → 执行/错误回填 → P5 → 追加 tool 行
        new_findings: List[Dict[str, Any]] = []
        for idx, tc in enumerate(tool_calls):
            name = tc.get("function", {}).get("name", "")
            call_id = tc.get("id", "")
            parsed_args = _parse_args(tc)

            if name not in allowed_names:
                logger.warning(
                    "[deep_research] 工具 '%s' 不在本轮可用集合,拦截不执行", name)
                result_content = json.dumps({
                    "error": (f"工具 '{name}' 不存在或本轮不可用。"
                              f"可用工具:{sorted(allowed_names)}。")
                }, ensure_ascii=False)
            else:
                tool_result = _execute_tool(name, parsed_args)
                if hooks:
                    event = ToolResultEvent(
                        session_id=session_id, module_code=module_code,
                        round_idx=round_idx, tool_name=name,
                        tool_call_id=call_id, result=tool_result)
                    tool_result, _orig = rewrite_tool_result(hooks, event)
                result_content = tool_result

                # findings 收集(成功路径;错误 JSON 不进 findings)
                if not result_content.lstrip().startswith("{\"error"):
                    query = (parsed_args.get("query")
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

            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": result_content})

        return new_findings

    # ------------------------------------------------------------------
    # SYNTHESIZE
    # ------------------------------------------------------------------

    def _synthesize_phase(self, provider, user_query: str,
                          plan: Dict[str, Any],
                          findings: List[Dict[str, Any]],
                          llm_config: Dict[str, Any],
                          ec: "ExecutionContext", hooks,
                          trace: Dict[str, Any]) -> str:
        """精简 messages 流式生成报告(唯一转发 delta 的相位)。"""
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
                module_code=ec.module.module_code,
                round_idx=0, messages=synth_messages, model=model))

        result = _stream_round(
            provider, synth_messages, model, temperature, max_tokens,
            ec.stream)  # 报告 delta 乐观转发
        report = result.get("content", "") or ""

        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=ec.cxt.session_id,
                module_code=ec.module.module_code,
                round_idx=0, content=report, tool_calls=[]))

        trace["phases"].append("synthesize")
        _emit_round(ec.stream, "synthesize", 0)
        return report


# ============================================================================
# 模块级辅助
# ============================================================================

def _current_user_query(cxt) -> str:
    """本轮用户问题(cxt.history 里最后一条 user 消息;兜底空串)。

    history 是 SessionMessage 对象列表(role/content 属性);turn 开头的
    user 行已由 chat 层写入,故取末尾最近一条。
    """
    for m in reversed(cxt.history or []):
        if getattr(m, "role", "") == "user":
            return getattr(m, "content", "") or ""
    return ""


def _prior_trace(cxt) -> Optional[Dict[str, Any]]:
    """上一轮的研究 trace(cxt.metadata;续研 / force_close 用)。"""
    prior = (cxt.metadata or {}).get("deep_research")
    return prior if isinstance(prior, dict) else None


def _system_content(messages: List[Dict[str, Any]]) -> str:
    """取首条 system 行内容(无则空串)。"""
    for m in messages:
        if m.get("role") == "system":
            return m.get("content", "") or ""
    return ""


def _extract_plan_json(content: str) -> Dict[str, Any]:
    """容错提取 PLAN JSON:首个平衡的 ``{...}`` 块 → 解析 → 校验
    sub_questions 为非空字符串列表;不合法返回空 dict。"""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    questions = data.get("sub_questions")
    if (not isinstance(questions, list)
            or not questions
            or not all(isinstance(q, str) and q.strip() for q in questions)):
        return {}
    return {"sub_questions": [q.strip() for q in questions],
            "notes": str(data.get("notes", ""))}


def _covered_questions(sub_questions: List[str],
                       findings: List[Dict[str, Any]]) -> set:
    """已覆盖的子问题集合(该子问题的关键词出现在某条 finding 的
    query 里即算;简单启发式,够状态板用)。"""
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
    """重写 system[0] 的状态板段(状态板总是 system 的最后一段)。"""
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
    """工作区超预算时对最旧的 tool 行做中段截断(保头尾)。"""
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
# 插件注册 —— 底部 import 副作用(route.py 末尾 import 本模块完成注册;
# 与 apps/install_booking_agent 的 stages.py 同一 idiom)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", "deep_research", DeepResearchExecutor)
