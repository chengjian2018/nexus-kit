"""DeepResearchExecutor 的多模块版 —— 每个研究相位一个模块,同轮接力。

单模块版(executor.py)在一次 execute() 里跑完全部相位;本文件把
PREPLAN / PLAN / SEARCH / SYNTHESIZE 拆成四个 AGENT 模块,各绑一个
相位 executor 插件,经 ModuleJumpEvent 在 chat 层 hop 循环里同轮接力
(ARCHITECTURE.md「跳转多样化配方」的自定义 executor 写事件通道):

    dr_preplan ──jump──> dr_plan ──jump──> dr_search ──jump──> dr_synthesize
     预检索/初始化        规划子问题          迭代检索          综合报告+复位底座

与单模块版的关键差异:
- 相位间状态(question / 工作区 messages / findings / plan / 轮次)经
  ``cxt.metadata["deep_research_state"]`` 传递——轮内瞬态(每轮 begin_turn
  出清,SYNTHESIZE 收尾即弹出),终态 trace 仍写 ``cxt.metadata
  ["deep_research"]``(与单模块版同键同构,观测 / 续研不受影响);
- 研究过程同样不落 cxt.history(相位方法原样复用,私有工作区决策不变),
  历史仍只有「用户问题 → 研究报告」的 Q/A 对;
- 每个相位模块各自解析工具面(_resolve_tools),pattern ACL 语义按模块
  生效;中间相位不转发 delta,唯一流式相位仍是 SYNTHESIZE;
- SYNTHESIZE 收尾把底座复位到 entry_module_code——current_module_code
  跨轮保留,不复位的话下一问会直接落进综合模块。

相位实现零拷贝:四个类继承 DeepResearchExecutor 只为复用其无状态相位
方法(_preplan_phase 等,插件注册中心本就共享单实例),execute 各自只
做「取状态 → 跑一个相位 → 存状态 → 写跳转」。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from atoms.executors.loop_executor import _emit_round
from atoms.tools.mcp_tool import ensure_mcp_ready

from apps.deep_research_agent.executor import (
    DeepResearchExecutor,
    _current_user_query,
    _prior_trace,
)
from nexus.context import ModuleJumpEvent
from nexus.engine.agent_hooks import (
    AgentEndEvent,
    AgentStartEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
)
from nexus.engine.execution import ExecutionContext, ModuleExecutor
from nexus.engine.loop import TurnResult, _resolve_tools, warn_prompt_length
from nexus.engine.messages import build_agent_messages
from nexus.llm.resolve import build_provider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 流水线拓扑(模块码 = 插件码,route_multi.py 的四个模块按此绑定)
# ---------------------------------------------------------------------------

DR_PREPLAN_CODE = "dr_preplan"
DR_PLAN_CODE = "dr_plan"
DR_SEARCH_CODE = "dr_search"
DR_SYNTHESIZE_CODE = "dr_synthesize"

# 轮内瞬态研究状态键(SYNTHESIZE 弹出;TurnLifecycle.begin_turn 兜底出清,
# 防中途异常留下陈旧状态);终态 trace 沿用单模块版的 "deep_research" 键
_STATE_KEY = "deep_research_state"
_TRACE_KEY = "deep_research"
_JUMP_SOURCE = "dr_pipeline"


def _load_state(cxt) -> Optional[Dict[str, Any]]:
    """取在途研究状态(非 dict 形态视为无,防脏数据)。"""
    state = (cxt.metadata or {}).get(_STATE_KEY)
    return state if isinstance(state, dict) else None


def _save_state(cxt, state: Dict[str, Any]) -> None:
    cxt.metadata[_STATE_KEY] = state


def _jump(cxt, target: str, reason: str = "") -> None:
    """写同轮跳转事件(hop 循环消费;目标存在性由 chat 层校验)。"""
    cxt.actions.append(ModuleJumpEvent(
        target_module_code=target, reason=reason, source=_JUMP_SOURCE))


def _orphan_state(cxt, module, phase: str) -> Dict[str, Any]:
    """异常入口的兜底状态:无在途状态直送收尾,SYNTHESIZE 会以原问题为
    唯一子问题出「证据不足」报告——流水线任何一站都不因缺状态而卡死。"""
    return {
        "question": _current_user_query(cxt),
        "phases": [phase],
        "degraded": True,
        "session_id": cxt.session_id,
        "module_code": module.module_code,
        "findings": [],
        "tool_stats": {},
        "plan": {},
    }


_FORCE_CLOSE_REPLY = "(研究流程被强制收尾,未能完成研究。)"


class DrPreplanExecutor(DeepResearchExecutor):
    """dr_preplan 模块:研究状态初始化 + PREPLAN 相位(可选预检索)。"""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        module = ec.module
        pattern = ec.pattern
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)

        # P1 on_agent_start:片段进 PLAN 底座(流水线首站承担,契约同 default loop)
        hooks = resolve_agent_hooks(module, pattern)
        fragments = collect_fragments(
            hooks,
            AgentStartEvent(session_id=cxt.session_id,
                            module_code=module.module_code, cxt=cxt),
        ) if hooks else []

        state: Dict[str, Any] = {
            "question": _current_user_query(cxt),
            "phases": [],
            "degraded": False,
            "session_id": cxt.session_id,
            "module_code": module.module_code,
            "findings": [],
            "tool_stats": {},
            "plan": {},
        }

        # force_close 防御(线性流水线 + max_hops=4 正常不可达):此时 hop
        # 循环已不再消费跳转事件,后续相位不会执行,直接收尾话术
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        # MCP 时序闸同单模块版:抢在连接完成前解析工具会把 allowed_names
        # 冻结成空集,这里等到终态(未配置 server 时零开销)
        await ensure_mcp_ready()
        tools = _resolve_tools(module, pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}

        base_messages = build_agent_messages(
            module, cxt, pattern=pattern, extra_blocks=fragments)
        warn_prompt_length(base_messages, cxt, module)

        plan_base, preplan_findings, preplan_stats = (
            await self._preplan_phase(
                provider, base_messages, tools, allowed_names,
                ec, hooks, state))

        state.update({
            "messages": plan_base,           # PLAN 底座(含预检索上下文,若有)
            "base_messages": base_messages,  # SEARCH 工作区重建底座
            "findings": preplan_findings,
            "tool_stats": preplan_stats,
        })
        _save_state(cxt, state)
        _jump(cxt, DR_PLAN_CODE, reason="预检索完成,进入研究规划")
        # 中间相位 content 为空:hop 循环消费跳转事件后续答,本结果被丢弃
        return TurnResult(content="")


class DrPlanExecutor(DeepResearchExecutor):
    """dr_plan 模块:PLAN 相位(子问题 JSON,自纠重试,降级兜底)。"""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _load_state(cxt)
        if state is None:
            _save_state(cxt, _orphan_state(cxt, ec.module, "orphan_plan"))
            _jump(cxt, DR_SYNTHESIZE_CODE, reason="无在途研究状态,跳过规划")
            return TurnResult(content="")

        state["module_code"] = ec.module.module_code
        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(ec.module, ec.pattern)
        plan = await self._plan_phase(
            provider, state.get("messages") or [], cxt.llm_config or {},
            ec, hooks, state)
        state["plan"] = plan
        _save_state(cxt, state)
        _jump(cxt, DR_SEARCH_CODE, reason="研究计划就绪,进入迭代检索")
        return TurnResult(content="")


class DrSearchExecutor(DeepResearchExecutor):
    """dr_search 模块:SEARCH 相位(带工具 ReAct 循环 + 反思状态板)。"""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        if ec.force_close:
            return TurnResult(content=_FORCE_CLOSE_REPLY)

        state = _load_state(cxt)
        if state is None:
            _save_state(cxt, _orphan_state(cxt, ec.module, "orphan_search"))
            _jump(cxt, DR_SYNTHESIZE_CODE, reason="无在途研究状态,跳过检索")
            return TurnResult(content="")

        state["module_code"] = ec.module.module_code
        await ensure_mcp_ready()
        tools = _resolve_tools(ec.module, ec.pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}
        provider = build_provider(cxt.llm_config or {})
        hooks = resolve_agent_hooks(ec.module, ec.pattern)

        findings, search_stats = await self._search_phase(
            provider, state.get("base_messages") or [],
            state.get("plan") or {}, tools, allowed_names, ec, hooks, state,
            initial_findings=state.get("findings"),
            initial_tool_stats=state.get("tool_stats"))
        state.update({
            "findings": findings,
            "tool_stats": search_stats["tool_stats"],
            "rounds": search_stats["rounds"],
            "reflection_note": search_stats["reflection_note"],
        })
        _save_state(cxt, state)
        _jump(cxt, DR_SYNTHESIZE_CODE, reason="检索完成,进入综合")
        return TurnResult(content="")


class DrSynthesizeExecutor(DeepResearchExecutor):
    """dr_synthesize 模块:SYNTHESIZE 相位——唯一流式相位,收尾 + 复位底座。"""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        module = ec.module
        pattern = ec.pattern

        state = _load_state(cxt)
        if state is None:
            # 防御(正常流程不可达):以既有 trace 的 sources / 空资料收尾
            prior = _prior_trace(cxt)
            state = _orphan_state(cxt, module, "orphan_synthesize")
            if prior:
                state["findings"] = list(prior.get("sources", []))
                state["tool_stats"] = dict(prior.get("tool_stats", {}))
                state["degraded"] = bool(prior.get("degraded", False))

        state["module_code"] = module.module_code
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)
        hooks = resolve_agent_hooks(module, pattern)

        plan = state.get("plan") or {}
        findings = state.get("findings") or []
        report = await self._synthesize_phase(
            provider, state.get("question", ""), plan, findings,
            llm_config, ec, hooks, state)

        # 终态 trace:与单模块版同键同构(_synthesize_phase 已 append
        # "synthesize" 进 phases)
        trace = {
            "question": state.get("question", ""),
            "phases": state.get("phases", []),
            "degraded": bool(state.get("degraded", False)),
            "sub_questions": plan.get("sub_questions", []),
            "sources": findings,
            "tool_call_count": sum(
                (state.get("tool_stats") or {}).values()),
            "tool_stats": state.get("tool_stats") or {},
            "rounds": state.get("rounds", 0),
            "reflection_note": state.get("reflection_note", ""),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        cxt.metadata[_TRACE_KEY] = trace
        cxt.metadata.pop(_STATE_KEY, None)  # 轮内瞬态出清

        # P7 on_agent_end:报告即本轮回复
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id,
                module_code=module.module_code,
                rounds=trace["rounds"] + 2, outcome="reply", reply=report))

        _emit_round(ec.stream, "final", trace["rounds"])

        # 底座复位:研究流水线一轮走完,下一轮从首站重新进入(hop 循环的
        # reroute 把底座留在了 dr_synthesize,不复位下一问会直接进综合模块)
        if pattern is not None and pattern.entry_module_code:
            cxt.current_module_code = pattern.entry_module_code
            cxt.current_node_code = None

        return TurnResult(content=report, extra={"deep_research": trace})


# ============================================================================
# 插件注册 —— 底部 import 副作用(route_multi.py 末尾 import 本模块完成
# 注册;与 executor.py 同一 idiom)
# ============================================================================

from nexus.registry.plugins import registry as plugin_registry  # noqa: E402

plugin_registry.register("executor", DR_PREPLAN_CODE, DrPreplanExecutor)
plugin_registry.register("executor", DR_PLAN_CODE, DrPlanExecutor)
plugin_registry.register("executor", DR_SEARCH_CODE, DrSearchExecutor)
plugin_registry.register("executor", DR_SYNTHESIZE_CODE, DrSynthesizeExecutor)
