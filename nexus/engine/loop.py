"""Engine loop kernel — TurnResult, the shared tool toolbox.

The ReAct loop body lives in the executor atom
(atoms/executors/loop_executor.py::DefaultLoopExecutor, plugin code
"default_loop" — the AGENT graph's default node executor); this kernel
module keeps:

- TurnResult — the executor return contract (content/next/sends/
  wait_human/actions/extra; re-exported from turn_result)
- the tool-resolution / dispatch toolbox (_resolve_tools /
  _dispatch_tool_calls / _parse_args / _execute_tool) shared by the default
  executor and custom loops — kept in the kernel so the layering stays
  one-directional (atoms → nexus) and existing import anchors hold
- framework-enforced prompt items (force-close suffix, prompt-length warning)

Tool authorization (deny-by-default, three-layer gate):

1. toolset tag — every registered tool carries one (builtin: knowledge /
   mcp; MCP servers register under ``mcp-<server>``)
2. ``pattern.allow_toolset`` — the toolset-level grant (empty = nothing)
3. ``node.use_tools`` — the concrete tool-name grant (**empty = no tools**)

Effective set = use_tools ∩ tools-of-allowed-toolsets; declared-but-
unavailable names log a warning at resolution (registration-time
validation already failed fast on dangling/cross-toolset declarations).
"""

import json
import logging
from typing import Any, Dict, List

from nexus.engine.agent_hooks import (
    ToolCallEvent,
    ToolResultEvent,
    rewrite_tool_call,
    rewrite_tool_result,
)
from nexus.engine.turn_result import TurnResult  # noqa: F401 -- compat re-export (import anchor)
from nexus.context import encode_tool_call_content
from nexus.registry.tools import registry as tool_registry

logger = logging.getLogger(__name__)

# Warn when the system prompt exceeds this length (prompt bloat observability)
_PROMPT_LENGTH_WARN = 4000


# ---------------------------------------------------------------------------
# Tool round dispatch (P4/P5 + main-flow tool-name validation)
# ---------------------------------------------------------------------------

async def _dispatch_tool_calls(
    cxt, node, messages, content, tool_calls, hooks, allowed_names,
    round_idx, stream=None,
) -> None:
    """Unified dispatch of a tool round: P4 rewrite -> validate -> append
    assistant payload -> execute -> P5 -> append tool rows.

    Ordering (rule 4): P4 chained rewrite first, applied back to tc (args
    re-serialized into ``tc["function"]["arguments"]`` — rewriting the tc
    dict returned by the LLM in place, so in-loop messages / history payload
    / execution all share the rewritten single source of truth), then the
    assistant JSON payload is appended; ``tool_call_id`` is never changed
    (the lifeline of protocol pairing).

    Main-flow final validation (rule 2, the authoritative checkpoint): any
    final name not in allowed_names is **not executed**; the tool row is
    backfilled with an error listing the available tools (the model
    self-corrects next round). Metadata follows the synthetic-row
    convention ``{"synthetic": True}``. P5 only fires on a real
    ``_execute_tool`` result, never on synthetic/backfilled strings.

    Audit metadata: when a rewrite happens, ``rewritten=True`` plus
    ``original_call`` (P4, original name/args) / ``original_result``
    (P5, original string).

    stream: optional StreamEmitter — each issued / returned call is
    forwarded as trace events (tool_call / tool_result) for real-time
    consumers; None = no emission.
    """
    session_id = cxt.session_id
    node_code = node.code
    _emit = getattr(stream, "emit_trace", None)

    # 1. P4 chained rewrite -> applied back to tc
    rewrite_audits = {}
    for idx, tc in enumerate(tool_calls):
        name = tc.get("function", {}).get("name", "")
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
                logger.warning(
                    "[hooks] 改写后 args 无法序列化，保留原串: %s", e)
        if original is not None:
            rewrite_audits[idx] = original

    # 2. assistant payload (post-rewrite reality; ids untouched to guarantee
    #    replay pairing)
    messages.append({"role": "assistant", "content": content or None,
                     "tool_calls": tool_calls})
    await cxt.add_message(
        "assistant",
        encode_tool_call_content(content or "", tool_calls),
        stage="agent",
    )

    # 3. Per tc: validate -> execute (valid) / error backfill (invalid) ->
    #    P5 -> append row
    for idx, tc in enumerate(tool_calls):
        name = tc.get("function", {}).get("name", "")
        call_id = tc.get("id", "")
        metadata = {"tool_name": name, "tool_call_id": call_id}

        if _emit is not None:
            _emit("tool_call", node_code=node_code, call_id=call_id,
                  tool_name=name, args=_parse_args(tc), round_idx=round_idx)

        if name not in allowed_names:
            logger.warning(
                "[tools] 工具 '%s' 不在本轮可用集合中，拦截不执行"
                "（幻觉/越权调用，错误回填供模型自纠）", name,
            )
            result_content = json.dumps({
                "error": (
                    f"工具 '{name}' 不存在或本轮不可用。"
                    f"可用工具：{sorted(allowed_names)}。"
                    f"请从可用工具中重新选择，或直接回应用户。"
                ),
            }, ensure_ascii=False)
            metadata["synthetic"] = True
        else:
            tool_result = await _execute_tool(name, _parse_args(tc))
            result_original = None
            if hooks:
                event = ToolResultEvent(
                    session_id=session_id, node_code=node_code,
                    round_idx=round_idx, tool_name=name,
                    tool_call_id=call_id, result=tool_result)
                tool_result, result_original = rewrite_tool_result(hooks, event)
            result_content = tool_result
            if result_original is not None:
                metadata["rewritten"] = True
                metadata["original_result"] = result_original

        if idx in rewrite_audits:
            metadata["rewritten"] = True
            metadata["original_call"] = rewrite_audits[idx]

        if _emit is not None:
            _emit("tool_result", node_code=node_code, call_id=call_id,
                  tool_name=name, result=result_content, round_idx=round_idx,
                  synthetic=bool(metadata.get("synthetic")))

        await cxt.add_message("tool", result_content, stage="agent",
                              metadata=metadata)
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": result_content})


# ---------------------------------------------------------------------------
# Framework-enforced prompt items
# ---------------------------------------------------------------------------

# force_close close-out suffix (control-flow semantics that prevents infinite
# loops when the step budget is exhausted; no messages_builder may break it —
# the loop executor enforces it via append_force_close_suffix after the
# builder returns)
_FORCE_CLOSE_SUFFIX = "\n请直接回应用户，勿再移交。"


def _append_force_close_suffix(messages: List[Dict[str, Any]]) -> None:
    """Append the close-out suffix to the first system row; prepend one if none exists."""
    for m in messages:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + _FORCE_CLOSE_SUFFIX
            return
    messages.insert(0, {"role": "system",
                        "content": _FORCE_CLOSE_SUFFIX.strip()})


def _warn_prompt_length(messages, cxt, node) -> None:
    """Warn on system row length (prompt bloat observability; measures the
    real length after hooks injection and the suffix)."""
    system_row = next(
        (m for m in messages if m.get("role") == "system"), None)
    if system_row is None:
        return
    length = len(system_row.get("content") or "")
    if length > _PROMPT_LENGTH_WARN:
        logger.warning(
            "Agent system_prompt 过长 (%d 字符): session=%s, node=%s（prompt 膨胀观测）",
            length, cxt.session_id, node.code,
        )


# Public aliases for executor atoms (the executor-facing names; the underscore
# originals remain for existing test anchors)
append_force_close_suffix = _append_force_close_suffix
warn_prompt_length = _warn_prompt_length


# ---------------------------------------------------------------------------
# Tool resolution and filtering (deny-by-default)
# ---------------------------------------------------------------------------

def _resolve_tools(node, pattern=None) -> List[Dict[str, Any]]:
    """Filter tool definitions by the three-layer authorization.

    1. **Pattern layer**: ``pattern.allow_toolset`` — only tools whose
       toolset is listed are candidates (empty allowlist = nothing).
    2. **Node layer**: ``node.use_tools`` — **empty = NO tools**
       (deny-by-default); non-empty intersects with layer 1's candidates.
    """
    node_code = node.code if node is not None else ""
    use_tools = set(getattr(node, "use_tools", None) or [])
    if not use_tools:
        logger.info("节点 '%s' 未声明 use_tools（deny-by-default，无工具）",
                    node_code)
        return []

    allowed_toolsets = set(getattr(pattern, "allow_toolset", None) or []) \
        if pattern is not None else set()
    if not allowed_toolsets:
        logger.info(
            "节点 '%s' 声明了工具但 pattern.allow_toolset 为空（无可用工具集）",
            node_code)
        return []

    tool_names = tool_registry.names_in_toolsets(allowed_toolsets) & use_tools

    missing = use_tools - tool_names
    if missing:
        logger.warning(
            "节点 '%s' 声明的工具不可用（未授权 toolset 或未注册）: %s",
            node_code, sorted(missing),
        )

    if not tool_names:
        return []

    tool_schemas = tool_registry.get_definitions(tool_names)

    logger.info(
        "节点 '%s' 可用工具: %s",
        node_code,
        [t.get("function", {}).get("name", "?") for t in tool_schemas],
    )
    return tool_schemas


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _parse_args(tc) -> Dict[str, Any]:
    """Parse a tool_call's arguments string into a dict."""
    args_str = tc.get("function", {}).get("arguments", "{}")
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else args_str
    except json.JSONDecodeError:
        args = {}
    return args if isinstance(args, dict) else {}


async def _execute_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a single tool call, returning a JSON string result."""
    try:
        result = await tool_registry.dispatch(tool_name, tool_args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("工具执行异常: %s", tool_name)
        return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)
