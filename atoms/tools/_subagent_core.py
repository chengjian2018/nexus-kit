"""Sub-agent leaf-loop shared kernel — the execution primitive behind
delegate_task and run_workflow.

Shared by atoms/tools/subagent_tool.py (one-shot delegation) and
atoms/tools/workflow_tool.py (six-topology orchestration): a slimmed ReAct
loop operating purely on a local messages list (nothing written to
DialogueContext or session history — the same isolation philosophy as the
fanout ``_branch_cxt``) plus a few result-shaping helpers. This module has
no top-level ``registry.register`` call, so AST discovery
(nexus/registry/discovery.py) never imports it.

The provider is injected by the caller (not built inside the kernel): each
tool module imports ``build_provider`` itself, keeping the tests' patch
points in each tool module's namespace.
"""

import json
import logging
from typing import Any, Dict, Optional, Set

from nexus.engine.loop import _parse_args
from nexus.registry.tools import registry

logger = logging.getLogger(__name__)

# Per-tool-result truncation inside the sub-loop (the deep_research
# _PER_RESULT_CHARS precedent: one tool output must not blow up the
# sub-context)
_INNER_RESULT_CHARS = 4000

# The leaf sub-agent's builtin default system prompt (callers may override
# via args.system_prompt)
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
                # Hallucinated-name interception (same self-correction philosophy as the main loop in loop.py)
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
