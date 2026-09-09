"""Agent loop kernel module — TurnResult, the shared tool toolbox, and the
run_agent compat facade.

The loop's orchestration body moved to the executor atom
(atoms/executors/loop_executor.py::DefaultLoopExecutor, plugin code
"default_loop"); this kernel module keeps:

- TurnResult — the executor return contract (content/actions/extra)
- run_agent — compat facade (test anchor): builds an ExecutionContext and
  delegates to the plugin-resolved executor
- the tool-resolution / dispatch toolbox (_resolve_tools /
  _resolve_lent_tools / _dispatch_tool_calls / _parse_args / _execute_tool /
  build_transfer_tools / _transfer_reason) shared by the default executor
  and custom loops — kept in the kernel so the layering stays one-directional
  (atoms → nexus) and existing import anchors hold
- framework-enforced prompt items (force-close suffix, prompt-length warning)

Docs for the transfer semantics preserved by the toolbox live on the
functions themselves; see atoms/executors/loop_executor.py for the loop.
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
from nexus.engine.execution import ExecutionContext
from nexus.engine.session import Session
from nexus.engine.turn_result import TurnResult  # noqa: F401 -- compat re-export (import anchor)
from nexus.context import encode_tool_call_content
from nexus.registry.plugins import registry as plugin_registry
from nexus.registry.tools import registry as tool_registry

logger = logging.getLogger(__name__)

TRANSFER_TOOL_PREFIX = "transfer_to_"

# Warn when the system prompt exceeds this length (projection bloat observability)
_PROMPT_LENGTH_WARN = 4000


def conversation(
    session: Session,
    module,
    llm_config: Dict[str, Any],
) -> str:
    """Compatibility wrapper: calls run_agent and returns the reply text (the chat layer continues transfer turns)."""
    result = run_agent(session, module, llm_config)
    return result.content or ""


def run_agent(
    session: Session,
    module,
    llm_config: Dict[str, Any],
    force_close: bool = False,
) -> TurnResult:
    """Compat facade (test anchor): run one turn of a single AGENT module.

    Builds an ExecutionContext (llm_config written back to cxt.llm_config)
    and delegates to the plugin-resolved default_loop executor — identical
    behavior to the pre-pluginization function body.
    """
    from nexus.model.module import ModuleType  # local: avoid import cycle at module import time

    ec = ExecutionContext(
        cxt=session.cxt, pattern=session.pattern, module=module,
        force_close=force_close,
    )
    session.cxt.llm_config = llm_config
    executor = plugin_registry.resolve(
        "executor", plugin_registry.default_executor_code(ModuleType.AGENT.value))
    return executor.execute(ec)


# ---------------------------------------------------------------------------
# Tool round dispatch (P4/P5 + main-flow tool-name validation)
# ---------------------------------------------------------------------------

def _dispatch_tool_calls(
    cxt, module, messages, content, tool_calls, hooks, allowed_names,
    lent_by, round_idx, transfer_error=None,
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
    self-corrects next round; this also seals the bypass where dispatch only
    checks registration and not ACL). Metadata follows the synthetic-row
    convention ``{"synthetic": True}``. P5 only fires on a real
    ``_execute_tool`` result, never on synthetic/backfilled strings.

    Non-empty transfer_error = the error-backfill branch for an illegal
    transfer target: the transfer entry (prefix determined earlier; rule 1 —
    not rewritten, not executed) is backfilled with that error string, while
    ordinary entries still go through the full P4 / validation / execution /
    P5 flow.

    Audit metadata: when a rewrite happens, ``rewritten=True`` plus
    ``original_call`` (P4, original name/args) / ``original_result``
    (P5, original string).
    """
    session_id = cxt.session_id
    module_code = module.module_code

    # 1. P4 chained rewrite -> applied back to tc (transfer entries skipped:
    #    their determination already happened earlier)
    rewrite_audits = {}
    for idx, tc in enumerate(tool_calls):
        name = tc.get("function", {}).get("name", "")
        if transfer_error is not None and name.startswith(TRANSFER_TOOL_PREFIX):
            continue
        parsed_args = _parse_args(tc)
        if hooks:
            event = ToolCallEvent(
                session_id=session_id, module_code=module_code,
                round_idx=round_idx, tool_name=name, args=parsed_args)
            final_name, final_args, original = rewrite_tool_call(
                hooks, event, allowed_names,
                reserved_prefix=TRANSFER_TOOL_PREFIX)
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
    cxt.add_message(
        "assistant",
        encode_tool_call_content(content or "", tool_calls),
        stage="agent",
    )

    # 3. Per tc: validate -> execute (valid) / error backfill (invalid,
    #    transfer entries) -> P5 -> append row
    for idx, tc in enumerate(tool_calls):
        name = tc.get("function", {}).get("name", "")
        call_id = tc.get("id", "")
        metadata = {"tool_name": name, "tool_call_id": call_id}

        if transfer_error is not None and name.startswith(TRANSFER_TOOL_PREFIX):
            result_content = transfer_error
        elif name not in allowed_names:
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
            tool_result = _execute_tool(name, _parse_args(tc))
            result_original = None
            if hooks:
                event = ToolResultEvent(
                    session_id=session_id, module_code=module_code,
                    round_idx=round_idx, tool_name=name,
                    tool_call_id=call_id, result=tool_result)
                tool_result, result_original = rewrite_tool_result(hooks, event)
            result_content = tool_result

            source = lent_by.get(name)
            if source:
                cxt.metadata["served_by_projection"] = {
                    "module": module_code, "source": source,
                }
                metadata["lent_by"] = source
            if result_original is not None:
                metadata["rewritten"] = True
                metadata["original_result"] = result_original

        if idx in rewrite_audits:
            metadata["rewritten"] = True
            metadata["original_call"] = rewrite_audits[idx]

        cxt.add_message("tool", result_content, stage="agent",
                        metadata=metadata)
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": result_content})


# ---------------------------------------------------------------------------
# Transfer tool builders (projection block building lives in engine/messages.py)
# ---------------------------------------------------------------------------

def build_transfer_tools(module, module_map) -> list:
    """Generate transfer tools edge-by-edge from sub_modules (spec §4 §3.3)."""
    tools = []
    for link in module.sub_modules:
        target = module_map.get(link.target)
        if target is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": f"{TRANSFER_TOOL_PREFIX}{link.target}",
                "description": (
                    f"移交给【{target.module_name}】。适用：该域的多轮深入流程。"
                    f"不适用：一句话或一次工具能解决的请求——那类直接自己处理。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {
                        "type": "string",
                        "description": "移交原因及已收集的用户信息摘要，供接手方无缝承接",
                    }},
                    "required": ["reason"],
                },
            },
        })
    return tools


# ---------------------------------------------------------------------------
# Framework-enforced prompt items
# ---------------------------------------------------------------------------

# force_close close-out suffix (control-flow semantics that prevents infinite
# loops when hops are exhausted; no messages_builder may break it — the loop
# executor enforces it via append_force_close_suffix after the builder returns)
_FORCE_CLOSE_SUFFIX = "\n请直接回应用户，勿再移交。"


def _append_force_close_suffix(messages: List[Dict[str, Any]]) -> None:
    """Append the close-out suffix to the first system row; prepend one if none exists."""
    for m in messages:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + _FORCE_CLOSE_SUFFIX
            return
    messages.insert(0, {"role": "system",
                        "content": _FORCE_CLOSE_SUFFIX.strip()})


def _warn_prompt_length(messages, cxt, module) -> None:
    """Warn on system row length (projection bloat observability; measures the
    real length after hooks injection and the suffix)."""
    system_row = next(
        (m for m in messages if m.get("role") == "system"), None)
    if system_row is None:
        return
    length = len(system_row.get("content") or "")
    if length > _PROMPT_LENGTH_WARN:
        logger.warning(
            "Agent system_prompt 过长 (%d 字符): session=%s, module=%s（投影膨胀观测）",
            length, cxt.session_id, module.module_code,
        )


# Public aliases for executor atoms (the executor-facing names; the underscore
# originals remain for existing test anchors)
append_force_close_suffix = _append_force_close_suffix
warn_prompt_length = _warn_prompt_length


# ---------------------------------------------------------------------------
# Tool resolution and filtering
# ---------------------------------------------------------------------------

def _resolve_tools(module, pattern=None) -> List[Dict[str, Any]]:
    """Filter tool definitions by pattern permissions + module.use_tools.

    Two-layer filtering:
    1. **Pattern layer**: get the tool set allowed for the current pattern +
       module via :meth:`ToolRegistry.get_allowed_tools_for_pattern`.
    2. **Module layer**: if ``module.use_tools`` is non-empty, take the
       intersection; if empty, use all tools allowed by the pattern layer.
    """
    pattern_code = pattern.code if pattern is not None else ""
    module_code = module.module_code or ""

    if pattern_code:
        allowed_tool_names = tool_registry.get_allowed_tools_for_pattern(
            pattern_code, module_code
        )
    else:
        allowed_tool_names = tool_registry.get_allowed_tools_for_pattern(
            "*", module_code
        )

    use_tools = module.use_tools or []
    if use_tools:
        tool_names_from_module = set(use_tools)

        missing = tool_names_from_module - allowed_tool_names
        if missing:
            logger.warning(
                "模块 '%s' 声明的工具不可用: %s (未授权或未注册)",
                module_code, missing,
            )

        tool_names = allowed_tool_names & tool_names_from_module
    else:
        tool_names = allowed_tool_names

    if not tool_names:
        logger.info(
            "模块 '%s' (pattern='%s') 无可用工具",
            module_code, pattern_code,
        )
        return []

    tool_schemas = tool_registry.get_definitions(tool_names)

    logger.info(
        "模块 '%s' (pattern='%s') 可用工具: %s",
        module_code,
        pattern_code,
        [t.get("function", {}).get("name", "?") for t in tool_schemas],
    )
    return tool_schemas


def _resolve_lent_tools(module, pattern):
    """Resolve borrowed tool schemas and the name -> source-domain mapping
    (spec §3.3 permissions).

    Returns:
        (schemas, lent_by): schemas is a list in OpenAI format; lent_by is
        {tool_name: source module_code}.
    """
    schemas, lent_by = [], {}
    for link in module.sub_modules:
        if not link.lend_tools:
            continue
        target = (pattern.module_map if pattern else {}).get(link.target)
        if target is None:
            continue
        allowed = set(target.use_tools or []) & set(link.lend_tools)
        # Second-pass filter: the lending path is bound by the same pattern-level
        # tool ACL (deny-by-default), with the borrower (target module) as the
        # ACL subject — this must not bypass get_allowed_tools_for_pattern
        if not allowed:
            continue
        if pattern is not None:
            allowed &= tool_registry.get_allowed_tools_for_pattern(
                pattern.code, link.target)
        else:
            allowed = set()
        for schema in tool_registry.get_definitions(allowed):
            name = schema["function"]["name"]
            schemas.append(schema)
            lent_by[name] = link.target
    return schemas, lent_by


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


def _execute_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a single tool call, returning a JSON string result."""
    try:
        result = tool_registry.dispatch(tool_name, tool_args)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.exception("工具执行异常: %s", tool_name)
        return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Transfer handling
# ---------------------------------------------------------------------------

def _transfer_reason(transfer_call) -> str:
    """Parse the transfer context (reason) from a transfer tool call's arguments."""
    args = _parse_args(transfer_call)
    reason = args.get("reason", "") if isinstance(args, dict) else ""
    return str(reason or "")
