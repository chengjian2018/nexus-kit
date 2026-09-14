"""子代理叶子循环共享内核 —— delegate_task 与 run_workflow 的执行原语。

被 atoms/tools/subagent_tool.py（单次委托）与 atoms/tools/workflow_tool.py
（六种拓扑编排）共享：一个只操作本地 messages 列表的瘦身 ReAct 循环
（不写 DialogueContext、不落会话历史，与 fanout ``_branch_cxt`` 的隔离
哲学一致）+ 若干结果整形辅助。本模块没有顶层 ``registry.register`` 调用，
AST 发现（nexus/registry/discovery.py）不会 import 它。

provider 由调用方注入（不在内核内 build）：各 tool 模块自己 import
``build_provider``，测试的 patch 点保持在各 tool 模块命名空间。
"""

import json
import logging
from typing import Any, Dict, Optional, Set

from nexus.engine.loop import _parse_args
from nexus.registry.tools import registry

logger = logging.getLogger(__name__)

# 子循环内单个工具结果的截断长度（deep_research _PER_RESULT_CHARS 先例：
# 防子上下文被单次工具输出撑爆）
_INNER_RESULT_CHARS = 4000

# 叶子子代理的内置默认 system prompt（调用方可经 args.system_prompt 覆盖）
_DEFAULT_SYSTEM_PROMPT = (
    "你是一个独立执行子任务的助手。你会收到一个自包含的任务描述；"
    "请使用可用工具完成它，并给出最终结论。\n"
    "规则：\n"
    "- 任务完成即止：不追问、不寒暄，直接给结论。\n"
    "- 结论先行：最终回复先用 1-3 句话给出核心答案，再附必要的细节与依据。\n"
    "- 工具失败时先自行换路重试；确实无法完成时，说明已尝试什么、卡在哪里。\n"
    "- 不虚构工具未返回的信息。"
)


def _result_is_error(raw: str) -> bool:
    """A dispatched tool result is a failure when its JSON carries "error"."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and "error" in payload


def _truncate(raw: str, limit: int, label: str) -> str:
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"…[{label}超长，已截断（原 {len(raw)} 字符）]"


def _accumulate_usage(total: Dict[str, int], usage: Optional[Dict[str, Any]]) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total[key] += int(value)


async def _run_sub_agent(*, provider, llm_config: Dict[str, Any],
                         system_prompt: str, task: str, granted: Set[str],
                         temperature: float, max_rounds: int,
                         state: Dict[str, Any]) -> Dict[str, Any]:
    """The sub ReAct loop — local messages only, never touching cxt/history.

    ``state`` is the timeout capture: every round snapshots the loop position
    (latest content / rounds / trace / usage) so asyncio.wait_for's
    cancellation can still return the partial progress.
    """
    model = llm_config.get("model") or ""
    max_tokens = llm_config.get("max_tokens", 2048)
    tools = registry.get_definitions(granted) if granted else None
    call_kwargs: Dict[str, Any] = (
        {"tools": tools, "tool_choice": "auto"} if tools else {})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    trace: list = []

    for round_idx in range(max_rounds):
        try:
            result = await provider.achat_completion(
                messages=messages, model=model, temperature=temperature,
                max_tokens=max_tokens, **call_kwargs)
        except Exception as e:
            logger.warning("子代理 LLM 调用失败（第 %d 轮）: %s", round_idx + 1, e)
            return {
                "status": "error",
                "error": f"子代理 LLM 调用失败: {type(e).__name__}: {e}",
                "content": state.get("last_content", ""),
                "rounds": round_idx,
                "trace": trace,
                "usage": usage_total,
            }

        content = result.get("content", "") or ""
        if content:
            state["last_content"] = content
        _accumulate_usage(usage_total, result.get("usage"))
        state.update(rounds=round_idx + 1, trace=trace, usage=usage_total)

        tool_calls = result.get("tool_calls", []) or []
        if not tool_calls:
            return {"status": "ok", "content": content,
                    "rounds": round_idx + 1, "trace": trace,
                    "usage": usage_total}

        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            if name not in granted:
                # 幻觉名拦截（与主循环 loop.py 同款自纠哲学）
                inner = json.dumps(
                    {"error": f"工具 '{name}' 不在子代理可用集合中。"
                              f"可用：{sorted(granted)}。"},
                    ensure_ascii=False)
                trace.append({"tool": name, "ok": False})
            else:
                raw = await registry.dispatch(name, _parse_args(tc))
                trace.append({"tool": name, "ok": not _result_is_error(raw)})
                inner = _truncate(raw, _INNER_RESULT_CHARS, "工具结果")
            messages.append({"role": "tool",
                             "tool_call_id": tc.get("id", ""),
                             "content": inner})

    logger.warning("子代理达到轮次上限 %d，强制收尾", max_rounds)
    return {"status": "max_rounds", "content": state.get("last_content", ""),
            "rounds": max_rounds, "trace": trace, "usage": usage_total}
