"""delegate_task — 单次子代理委托（toolset: subagent）。

一次 tool call = 一个自包含任务的完整子 ReAct 循环：子代理拥有独立的
system prompt 与受限工具集，跑完把最终结论作为 tool result 返回主循环。
v1 深度=1：子代理不可再委托，也不可在 workflow 叶子里被调用。

授权（deny-by-default 三层收口：注册 toolset → pattern.allow_toolset →
node.use_tools）::

    pattern:
      allow_toolset: [subagent, knowledge]   # 子代理池 = knowledge 工具集
    node:
      use_tools: [delegate_task]             # 主节点只需点它自己要用的工具

子代理可用工具池 = ``pattern.allow_toolset`` 各工具集下的工具，剔除
``subagent`` 工具集自身（结构性防递归提权）；调用方还可在 args.tools 里
点名收窄，越权项直接报错回填。llm_config 与授权边界由 default_loop 执行
器经 ``nexus.engine.tool_context`` 注入；脱离 agent loop 直接 dispatch 时
回退 ``get_llm_config()`` 且子代理无工具（纯推理循环）。

护栏（config ``subagent_tool`` 节可调）：整体超时默认 120s（args 只能调
小）、轮次上限 8、最终结论截断 8000 字符；子循环内单个工具结果截 4000
字符防子上下文膨胀。超时/轮次耗尽返回部分结论 + status 标记，主 agent
自行取舍；子循环内部工具错误走错误回填让子 agent 自纠，不打断循环。

子代理的 messages 只存在于本次调用内：不写 DialogueContext、不落会话
历史（与 fanout ``_branch_cxt`` 的隔离哲学一致）。子循环引擎本体在
atoms/tools/_subagent_core.py（与 workflow_tool 共享）。
"""

import asyncio
import logging
import time
from typing import Any, Dict, Set

from atoms.tools._subagent_core import (
    _DEFAULT_SYSTEM_PROMPT,
    _run_sub_agent,
    _truncate,
)
from nexus.engine.tool_context import (
    ToolCallContext,
    current_tool_context,
    subagent_scope,
)
from nexus.llm.resolve import build_provider
from nexus.registry.tools import registry, tool_error, tool_result
from nexus.settings import get_llm_config, get_subagent_tool_config

logger = logging.getLogger(__name__)

# 子代理工具池永不包含 subagent 工具集自身（Q4 深度=1 的结构性保障；
# subagent_scope 的标志位是第二道防线，run_workflow 另再排除 workflow）
_SELF_TOOLSETS = frozenset({"subagent"})
_TRACE_CAP = 20

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "把一个自包含的子任务委托给独立子代理执行，取回最终结论。"
        "子代理拥有独立上下文与受限工具集，看不到当前对话——task 必须写明"
        "目标、背景与期望产出。适合检索、查证、资料整理等可独立完成的工作。"
        "子代理不能再委托（深度=1）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "自包含的任务描述：目标、必要背景、期望的产出格式。"
                    "子代理看不到当前对话历史，所有必要信息都要写进任务里。"
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": "子代理的角色设定（可选；默认为结论导向的通用执行者）",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "子代理可用的工具名列表（可选；必须在系统授权范围内，"
                    "缺省=授权范围内全部可用工具）"
                ),
            },
            "temperature": {
                "type": "number",
                "description": "子代理采样温度 0-2（可选，默认继承当前会话）",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "子代理整体超时秒数（可选；只能调小，不能超过系统上限）",
            },
        },
        "required": ["task"],
    },
}


async def _handle_delegate_task(args: Dict[str, Any]) -> str:
    ambient = current_tool_context()

    # 深度=1：工具集已从子池结构性排除，这里再挡 workflow 叶子等绕道调用
    if ambient is not None and (ambient.in_subagent or ambient.in_workflow):
        return tool_error(
            "delegate_task 不允许嵌套调用（v1 深度=1）：子代理/workflow 内无法再委托子代理")

    task = str(args.get("task") or "").strip()
    if not task:
        return tool_error("task 必填：请提供自包含的任务描述（子代理看不到当前对话）")

    llm_config = (ambient.llm_config if ambient is not None else None) \
        or get_llm_config()
    guard = get_subagent_tool_config()

    system_prompt = str(args.get("system_prompt") or "").strip() \
        or _DEFAULT_SYSTEM_PROMPT

    temperature = llm_config.get("temperature", 0.7)
    if args.get("temperature") is not None:
        try:
            temperature = max(0.0, min(2.0, float(args["temperature"])))
        except (TypeError, ValueError):
            return tool_error("temperature 应为 0-2 的数字")

    timeout = float(guard["timeout_seconds"])
    if args.get("timeout_seconds") is not None:
        try:
            requested = float(args["timeout_seconds"])
        except (TypeError, ValueError):
            return tool_error("timeout_seconds 应为正数（秒）")
        if requested <= 0:
            return tool_error("timeout_seconds 必须大于 0")
        timeout = min(requested, timeout)

    # 子代理工具池：pattern 授权工具集 − subagent 自身（结构性防递归）
    pool: Set[str] = set()
    if ambient is not None and ambient.allow_toolsets:
        pool = registry.names_in_toolsets(
            set(ambient.allow_toolsets) - _SELF_TOOLSETS)
    granted = set(pool)
    if args.get("tools"):
        requested_tools = {str(t) for t in args["tools"]}
        invalid = sorted(requested_tools - pool)
        if invalid:
            return tool_error(
                f"以下工具不在子代理可用池中: {invalid}。"
                f"可用工具：{sorted(pool)}。请从中选择，或省略 tools 使用全部可用工具。")
        granted = requested_tools

    logger.info("[delegate_task] 委托开始: tools=%s, timeout=%.0fs, task=%r",
                sorted(granted), timeout, task[:80])

    started = time.monotonic()
    state: Dict[str, Any] = {
        "last_content": "", "rounds": 0, "trace": [], "usage": {},
    }
    base = ambient or ToolCallContext(
        llm_config=dict(llm_config), allow_toolsets=frozenset())
    provider = build_provider(llm_config)

    async def _guarded() -> Dict[str, Any]:
        with subagent_scope(base):
            return await _run_sub_agent(
                provider=provider, llm_config=llm_config,
                system_prompt=system_prompt, task=task, granted=granted,
                temperature=temperature,
                max_rounds=int(guard["max_rounds"]), state=state)

    try:
        payload = await asyncio.wait_for(_guarded(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[delegate_task] 子代理超时（%.0fs），返回部分结论", timeout)
        payload = {"status": "timeout", "content": state.get("last_content", ""),
                   "rounds": state.get("rounds", 0), "trace": state.get("trace", []),
                   "usage": state.get("usage", {})}

    # 最终结论截断（护栏：主 agent 上下文防爆）
    content = payload.get("content") or ""
    limit = int(guard["max_result_chars"])
    payload["truncated"] = len(content) > limit
    if payload["truncated"]:
        payload["content"] = _truncate(content, limit, "结论")
    payload["trace"] = (payload.get("trace") or [])[:_TRACE_CAP]
    payload["elapsed_seconds"] = round(time.monotonic() - started, 1)

    logger.info("[delegate_task] 委托结束: status=%s, rounds=%s, elapsed=%.1fs",
                payload.get("status"), payload.get("rounds"),
                payload["elapsed_seconds"])
    return tool_result(payload)


registry.register(
    name="delegate_task",
    toolset="subagent",
    schema=DELEGATE_TASK_SCHEMA,
    handler=_handle_delegate_task,
    is_async=True,
    description=DELEGATE_TASK_SCHEMA["description"],
    emoji="🤖",
    max_result_size_chars=10000,
)
