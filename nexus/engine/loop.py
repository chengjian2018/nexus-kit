"""
Agent dialogue loop — run_agent's dual-primitive executor
(inject: projection answers directly / transfer: writes a jump event).

Supports:
- Two-layer tool filtering: pattern permissions + module.use_tools
- Lent tools from neighbor modules (via ModuleLink.lend_tools)
- transfer_to_XX tools generated per sub_modules link; on call, a
  ModuleJumpEvent is appended to cxt.actions and the module's turn ends —
  the chat layer consumes the event and reroutes (no adjacency check /
  rebound rejection; target existence in module_map is the only guard)
- Tool round-trips recorded into DialogueContext history
- Pluggable hooks at loop points (pattern.agent_hooks declaration; events,
  dispatch and guard semantics see chat/agent_hooks.py)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nexus.engine.agent_hooks import (
    AgentEndEvent,
    AgentStartEvent,
    LLMCallEvent,
    LLMResponseEvent,
    ToolCallEvent,
    ToolResultEvent,
    TransferEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
    rewrite_tool_call,
    rewrite_tool_result,
)
from nexus.engine.messages import build_agent_messages
from nexus.engine.session import Session
from nexus.context import (
    ModuleJumpEvent,
    encode_tool_call_content,
)
from nexus.llm.resolve import build_provider
from nexus.registry.tools import registry as tool_registry

logger = logging.getLogger(__name__)

TRANSFER_TOOL_PREFIX = "transfer_to_"

# Max tool calling rounds to prevent infinite loops
_MAX_TOOL_ROUNDS = 10

# Warn when the system prompt exceeds this length (projection bloat observability)
_PROMPT_LENGTH_WARN = 4000


@dataclass
class TurnResult:
    """Result of running one turn of a single module.

    Empty reply + a ModuleJumpEvent in cxt.actions = this module silently
    transferred; the chat layer's hop loop consumes the event and reroutes.
    Otherwise reply is the outgoing response. actions is a reserved channel
    (same shape as cxt.actions); the current executor never produces it.
    """

    reply: Optional[str] = None
    actions: List[Dict[str, Any]] = field(default_factory=list)


def conversation(
    session: Session,
    module,
    llm_config: Dict[str, Any],
) -> str:
    """Compatibility wrapper: calls run_agent and returns reply (the chat layer continues transfer turns)."""
    result = run_agent(session, module, llm_config)
    return result.reply or ""


def run_agent(
    session: Session,
    module,
    llm_config: Dict[str, Any],
    force_close: bool = False,
) -> TurnResult:
    """Run one turn of a single AGENT module: inject answers directly /
    transfer writes a jump event and returns.

    Args:
        session: current session
        module: current module object (AgentModule)
        llm_config: LLM config dict with code, model, temperature, etc.
        force_close: force close (max_hops exhausted): appends the close-out
            hint (_FORCE_CLOSE_SUFFIX) and does not inject transfer tools

    Returns:
        TurnResult: reply is the response; on a transfer hit, reply is empty
        and the jump event has already been written to cxt.actions
        (ModuleJumpEvent) for the chat layer to consume.
    """
    cxt = session.cxt
    provider = build_provider(llm_config)

    # Agent loop hooks (declared at pattern level; a non-empty module-level
    # agent_hooks replaces it wholesale; when empty, every hook point is a
    # zero-overhead pass-through)
    hooks = resolve_agent_hooks(module, session.pattern)

    # P1 on_agent_start: fetched and injected before the loop and messages
    # assembly. Fragments reach the builder via extra_blocks (the contract
    # requires including them); hooks do not write to cxt
    fragments = collect_fragments(
        hooks,
        AgentStartEvent(session_id=cxt.session_id,
                        module_code=module.module_code, cxt=cxt),
    ) if hooks else []

    own_tools = _resolve_tools(module, session.pattern)
    lent_schemas, lent_by = _resolve_lent_tools(module, session.pattern)
    transfer_tools = [] if force_close else build_transfer_tools(module, cxt.module_map)
    tools = own_tools + lent_schemas + transfer_tools
    # Available set for main-flow validation / the P4 guard (own + lent;
    # transfer tools excluded — transfer turns skip tool dispatch, and a
    # rename smuggling the prefix is blocked by both guard and validation)
    allowed_names = {t.get("function", {}).get("name", "")
                     for t in own_tools + lent_schemas}

    # Integrated messages build (system content and list assembly share one
    # source): module.messages_builder > pattern.messages_builder > default
    # three-segment layout
    messages = build_agent_messages(module, cxt, pattern=session.pattern,
                                    extra_blocks=fragments)
    # force_close close-out suffix is enforced framework-side (control-flow
    # semantics; no builder may break it)
    if force_close:
        _append_force_close_suffix(messages)
    _warn_prompt_length(messages, cxt, module)

    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.7)
    max_tokens = llm_config.get("max_tokens", 2048)

    for round_idx in range(_MAX_TOOL_ROUNDS):
        logger.info(
            "Agent loop 第 %d 轮: session=%s, module=%s, tools=%d",
            round_idx + 1, cxt.session_id, module.module_code, len(tools),
        )

        # P2 on_llm_call: before each LLM call (messages passed by reference,
        # read-only discipline)
        if hooks:
            fire(hooks, "on_llm_call", LLMCallEvent(
                session_id=cxt.session_id, module_code=module.module_code,
                round_idx=round_idx, messages=messages, model=model))

        if tools:
            result = provider.chat_completion(
                messages=messages, model=model, temperature=temperature,
                max_tokens=max_tokens, tools=tools, tool_choice="auto",
            )
        else:
            result = provider.chat_completion(
                messages=messages, model=model, temperature=temperature,
                max_tokens=max_tokens,
            )

        content = result.get("content", "") or ""
        tool_calls = result.get("tool_calls", []) or []

        # P3 on_llm_response: after each LLM response (content/tool_calls
        # already parsed)
        if hooks:
            fire(hooks, "on_llm_response", LLMResponseEvent(
                session_id=cxt.session_id, module_code=module.module_code,
                round_idx=round_idx, content=content,
                tool_calls=tool_calls))

        # No tool calls -> inject primitive: answer directly
        if not tool_calls:
            logger.info("Agent loop 完成，共 %d 轮", round_idx + 1)
            # P7 on_agent_end: direct-answer exit
            if hooks:
                fire(hooks, "on_agent_end", AgentEndEvent(
                    session_id=cxt.session_id, module_code=module.module_code,
                    rounds=round_idx + 1, outcome="reply", reply=content))
            return TurnResult(reply=content)

        transfer_call = next(
            (tc for tc in tool_calls
             if tc.get("function", {}).get("name", "").startswith(TRANSFER_TOOL_PREFIX)),
            None,
        )
        if transfer_call is not None:
            target = transfer_call["function"]["name"][len(TRANSFER_TOOL_PREFIX):]
            transfer_reason = _transfer_reason(transfer_call)

            # Target missing (no sub_modules edge / hallucinated call): backfill
            # an error and keep looping so the LLM can pick another path (a real
            # OpenAI-compatible API requires an answer for every tool_call_id)
            if target not in cxt.module_map:
                logger.warning(
                    "[transfer] 目标 %s 不在 module_map 中，错误回填继续 loop",
                    target,
                )
                err = json.dumps(
                    {"error": "转移目标不存在，请直接回应用户"},
                    ensure_ascii=False)
                _dispatch_tool_calls(
                    cxt, module, messages, content, tool_calls,
                    hooks, allowed_names, lent_by, round_idx,
                    transfer_error=err)
                continue

            # Transfer hit: write the jump event and silently hand off from this
            # module (content is suppressed from output but kept in history);
            # the chat layer consumes the event and reroutes to the target
            # module within the same turn. Every tool_call of this response
            # gets a synthetic tool row (transfer entry logged as transferred,
            # the rest as not executed) so assistant.tool_calls replay fully
            # paired on the next round
            cxt.add_message(
                "assistant",
                encode_tool_call_content(content or "", tool_calls),
                stage="agent",
                metadata={"suppressed": True},
            )
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                if name.startswith(TRANSFER_TOOL_PREFIX):
                    synthetic = f"[已移交至模块 {target}]"
                else:
                    synthetic = "[未执行：本轮已移交]"
                cxt.add_message("tool", synthetic, stage="agent",
                                metadata={"synthetic": True,
                                          "tool_name": name,
                                          "tool_call_id": tc.get("id", "")})
            cxt.actions.append(ModuleJumpEvent(
                target_module_code=target,
                reason=transfer_reason,
                source="handoff_tool",
            ))
            logger.info(
                "[transfer] %s → %s（事件已写入 actions，移交 chat 层）",
                module.module_code, target,
            )
            # P6 on_transfer + P7 on_agent_end: transfer exit
            if hooks:
                fire(hooks, "on_transfer", TransferEvent(
                    session_id=cxt.session_id, module_code=module.module_code,
                    round_idx=round_idx, target=target,
                    reason=transfer_reason))
                fire(hooks, "on_agent_end", AgentEndEvent(
                    session_id=cxt.session_id, module_code=module.module_code,
                    rounds=round_idx + 1, outcome="transfer",
                    transfer_target=target))
            return TurnResult()

        # Ordinary tool calls: P4 rewrite -> main-flow validation -> execute
        # -> P5 rewrite -> append to history
        _dispatch_tool_calls(
            cxt, module, messages, content, tool_calls,
            hooks, allowed_names, lent_by, round_idx)

    logger.warning(
        "Agent loop 达到最大轮次 %d，强制终止: session=%s",
        _MAX_TOOL_ROUNDS, cxt.session_id,
    )
    # P7 on_agent_end: max-rounds-exceeded exit
    if hooks:
        fire(hooks, "on_agent_end", AgentEndEvent(
            session_id=cxt.session_id, module_code=module.module_code,
            rounds=_MAX_TOOL_ROUNDS, outcome="max_rounds",
            reply="抱歉，处理超时，请稍后重试。"))
    return TurnResult(reply="抱歉，处理超时，请稍后重试。")


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
# Transfer tool builders (projection block building has moved to chat/messages.py)
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
# System Prompt construction
# (four-block structure + hooks extension blocks have moved to
#  build_system_prompt in chat/messages.py — same source as the integrated
#  messages build; this module keeps only framework-enforced items)
# ---------------------------------------------------------------------------

# force_close close-out suffix (control-flow semantics that prevents infinite
# loops when hops are exhausted; no messages_builder may break it — run_agent
# enforces it via _append_force_close_suffix after the builder returns)
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
