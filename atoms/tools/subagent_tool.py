"""delegate_task — one-shot sub-agent delegation (toolset: subagent).

One tool call = a full sub-ReAct loop for one self-contained task: the
sub-agent gets its own system prompt and a restricted toolset, and returns
its final conclusion to the main loop as the tool result. v1 depth=1: a
sub-agent may not delegate again, nor be called inside a workflow leaf.

Authorization (deny-by-default three-layer gate: registered toolset →
pattern.allow_toolset → node.use_tools)::

    pattern:
      allow_toolset: [subagent, knowledge]   # sub-agent pool = knowledge toolset
    node:
      use_tools: [delegate_task]             # the main node names only its own tools

The sub-agent's tool pool = the tools under each ``pattern.allow_toolset``
toolset, minus the ``subagent`` toolset itself (structurally preventing
recursive privilege escalation); the caller may narrow it via args.tools —
out-of-pool entries get an error backfill. llm_config and the authorization
boundary are injected by the default_loop executor via
``nexus.engine.tool_context``; direct dispatch outside the agent loop falls
back to ``get_llm_config()`` and the sub-agent runs tool-less (pure
reasoning loop).

Guardrails (tunable via the config ``subagent_tool`` section): whole-loop
timeout default 120s (args may only lower it), round cap 8, final-conclusion
truncation at 8000 chars; a single tool result inside the sub-loop truncates
at 4000 chars to keep the sub-context from bloating. Timeout / round
exhaustion returns the partial conclusion + a status marker for the main
agent to weigh; tool errors inside the sub-loop go through error backfill
for the sub-agent to self-correct, never breaking the loop.

The sub-agent's messages exist only within this call: nothing is written to
DialogueContext or session history (the same isolation philosophy as the
fanout ``_branch_cxt``). The sub-loop engine itself lives in
atoms/tools/_subagent_core.py (shared with workflow_tool).
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

# The sub-agent tool pool never includes the subagent toolset itself (the
# structural guarantee of the Q4 depth=1 rule; subagent_scope's flags are
# the second line of defense; run_workflow additionally excludes workflow)
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

    # depth=1: the toolset is already structurally excluded from the pool;
    # this blocks detour calls from workflow leaves etc.
    if ambient is not None and (ambient.in_subagent or ambient.in_workflow):
        return tool_error(
            "delegate_task 不允许嵌套调用（v1 深度=1）：子代理/workflow 内无法再委托子代理")

    task = str(args.get("task") or "").strip()
    if not task:
        return tool_error("task 必填：请提供自包含的任务描述（子代理看不到当前对话）")

    llm_config = (ambient.llm_config if ambient is not None else None) \
        or get_llm_config()
    guard = get_subagent_tool_config(
        ambient.pattern_code if ambient is not None else "")

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

    # Sub-agent tool pool: pattern-granted toolsets − subagent itself
    # (structural anti-recursion)
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

    # Final-conclusion truncation (guardrail: keep the main agent's context from exploding)
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
