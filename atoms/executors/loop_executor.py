"""Default AGENT-module executor — the ReAct tool loop (moved from
nexus/engine/loop.py::run_agent, behavior unchanged).

The loop body lives here as the plugin implementation of kind="executor" /
code="default_loop". The kernel's nexus.engine.loop keeps TurnResult and a
compat facade run_agent() (test anchor) plus the shared tool-resolution
toolbox (_resolve_tools / _resolve_lent_tools / _parse_args /
_execute_tool) that this executor imports — atoms→nexus is a legal layering
direction, and keeping those helpers in the kernel preserves the
test_customer_agent_route import anchor.
"""

import json
import logging

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
from nexus.engine.execution import ExecutionContext, ModuleExecutor
from nexus.engine.loop import (
    TRANSFER_TOOL_PREFIX,
    TurnResult,
    _dispatch_tool_calls,
    _execute_tool,  # noqa: F401 -- re-exported convenience for custom loops
    _parse_args,  # noqa: F401 -- re-exported convenience for custom loops
    _resolve_lent_tools,
    _resolve_tools,
    _transfer_reason,
    append_force_close_suffix,
    build_transfer_tools,
    warn_prompt_length,
)
from nexus.engine.messages import build_agent_messages
from nexus.llm.resolve import build_provider
from nexus.context import ModuleJumpEvent, encode_tool_call_content

logger = logging.getLogger(__name__)

# Max tool calling rounds to prevent infinite loops
_MAX_TOOL_ROUNDS = 10


class DefaultLoopExecutor(ModuleExecutor):
    """AGENT executor: inject (answer directly with projection knowledge) /
    transfer (write a ModuleJumpEvent and end the module turn)."""

    def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        module = ec.module
        pattern = ec.pattern
        force_close = ec.force_close
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)

        # Agent loop hooks (declared at pattern level; a non-empty module-level
        # agent_hooks replaces it wholesale; when empty, every hook point is a
        # zero-overhead pass-through)
        hooks = resolve_agent_hooks(module, pattern)

        # P1 on_agent_start: fetched and injected before the loop and messages
        # assembly. Fragments reach the builder via extra_blocks (the contract
        # requires including them); hooks do not write to cxt
        fragments = collect_fragments(
            hooks,
            AgentStartEvent(session_id=cxt.session_id,
                            module_code=module.module_code, cxt=cxt),
        ) if hooks else []

        own_tools = _resolve_tools(module, pattern)
        lent_schemas, lent_by = _resolve_lent_tools(module, pattern)
        transfer_tools = [] if force_close else build_transfer_tools(
            module, cxt.module_map)
        tools = own_tools + lent_schemas + transfer_tools
        # Available set for main-flow validation / the P4 guard (own + lent;
        # transfer tools excluded — transfer turns skip tool dispatch, and a
        # rename smuggling the prefix is blocked by both guard and validation)
        allowed_names = {t.get("function", {}).get("name", "")
                         for t in own_tools + lent_schemas}

        # Integrated messages build (system content and list assembly share one
        # source): module.messages_builder > pattern.messages_builder > default
        # three-segment layout
        messages = build_agent_messages(module, cxt, pattern=pattern,
                                        extra_blocks=fragments)
        # force_close close-out suffix is enforced framework-side (control-flow
        # semantics; no builder may break it)
        if force_close:
            append_force_close_suffix(messages)
        warn_prompt_length(messages, cxt, module)

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
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        rounds=round_idx + 1, outcome="reply", reply=content))
                return TurnResult(content=content)

            transfer_call = next(
                (tc for tc in tool_calls
                 if tc.get("function", {}).get("name", "").startswith(
                     TRANSFER_TOOL_PREFIX)),
                None,
            )
            if transfer_call is not None:
                target = transfer_call["function"]["name"][
                    len(TRANSFER_TOOL_PREFIX):]
                transfer_reason = _transfer_reason(transfer_call)

                # Target missing (no sub_modules edge / hallucinated call):
                # backfill an error and keep looping so the LLM can pick
                # another path (a real OpenAI-compatible API requires an
                # answer for every tool_call_id)
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

                # Transfer hit: write the jump event and silently hand off from
                # this module (content is suppressed from output but kept in
                # history); the chat layer consumes the event and reroutes to
                # the target module within the same turn. Every tool_call of
                # this response gets a synthetic tool row (transfer entry
                # logged as transferred, the rest as not executed) so
                # assistant.tool_calls replay fully paired on the next round
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
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        round_idx=round_idx, target=target,
                        reason=transfer_reason))
                    fire(hooks, "on_agent_end", AgentEndEvent(
                        session_id=cxt.session_id,
                        module_code=module.module_code,
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
        return TurnResult(content="抱歉，处理超时，请稍后重试。")


from nexus.engine.loop import run_agent  # noqa: E402,F401 -- test anchor re-export
