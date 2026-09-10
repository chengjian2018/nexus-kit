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
    TurnResult,
    _dispatch_tool_calls,
    _execute_tool,
    _parse_args,
    _resolve_lent_tools,
    _resolve_tools,
    append_force_close_suffix,
    warn_prompt_length,
)
from nexus.engine.messages import build_agent_messages
from nexus.llm.resolve import build_provider
from nexus.context import encode_tool_call_content

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

        # MCP server 是启动期后台异步注册的:首轮对话若抢在连接完成之前,
        # 这里会解析出一个缺失 MCP 工具的集合。等待连接终态——未配置
        # server 时零开销(mcp_tool 内部吞异常,不阻塞对话)
        from atoms.tools.mcp_tool import ensure_mcp_ready
        ensure_mcp_ready()

        own_tools = _resolve_tools(module, pattern)
        lent_schemas, lent_by = _resolve_lent_tools(module, pattern)
        # Plan-⑥: transfer_to_XX tools are gone. Projection-served adjacency
        # (enable_project=True sub_modules) instead gets ONE generic
        # defer_to_module tool — calling it schedules the end-of-turn base
        # switch (DeferredModuleSwitch); the module keeps answering this
        # turn with the projected knowledge it already used
        defer_tool = [] if force_close else _build_defer_tool(
            module, pattern, cxt)
        tools = own_tools + lent_schemas + defer_tool
        # Available set for main-flow validation / the P4 guard (own + lent;
        # the defer tool is framework-managed — its dispatch writes the
        # event, no real tool execution)
        allowed_names = ({t.get("function", {}).get("name", "")
                          for t in own_tools + lent_schemas}
                         | {DEFER_TOOL_NAME} if defer_tool else
                         {t.get("function", {}).get("name", "")
                          for t in own_tools + lent_schemas})

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

            # Plan-⑤: stream each round natively; aggregate the chunks into
            # this round's complete result (tool dispatch needs the merged
            # tool_calls), forwarding text deltas optimistically (the wire
            # finish_reason only arrives at a round's end, so a later round
            # may turn out to be the real reply — round events carry the
            # outcome; done is authoritative)
            if tools:
                round_result = _stream_round(
                    provider, messages, model, temperature, max_tokens,
                    ec.stream, tools=tools,
                )
            else:
                round_result = _stream_round(
                    provider, messages, model, temperature, max_tokens,
                    ec.stream,
                )
            result = round_result

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
                _emit_round(ec.stream, "final", round_idx)
                # P7 on_agent_end: direct-answer exit
                if hooks:
                    fire(hooks, "on_agent_end", AgentEndEvent(
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        rounds=round_idx + 1, outcome="reply", reply=content))
                return TurnResult(content=content)

            defer_call = next(
                (tc for tc in tool_calls
                 if tc.get("function", {}).get("name", "") == DEFER_TOOL_NAME),
                None,
            )
            if defer_call is not None:
                args = _parse_args(defer_call)
                target = str(args.get("module_code", "") or "")
                defer_reason = str(args.get("reason", "") or "")

                # Hallucinated target (not a projection-served adjacency):
                # backfill an error and keep looping so the LLM answers
                # directly (protocol pairing preserved like any bad call)
                if target not in _projection_targets(module, pattern, cxt):
                    logger.warning(
                        "[defer] 目标 %s 不是有效投影子模块，错误回填继续 loop",
                        target,
                    )
                    _dispatch_tool_calls(
                        cxt, module, messages, content, tool_calls,
                        hooks, allowed_names, lent_by, round_idx,
                        transfer_error=json.dumps(
                            {"error": "延迟切换目标无效，请直接回应用户"},
                            ensure_ascii=False))
                    continue

                # Defer hit: this turn KEEPS answering with the projected
                # knowledge already used; the DeferredModuleSwitch schedules
                # the end-of-turn base switch (chat layer applies it after
                # the hop loop — the NEXT turn runs on the target). The
                # defer entry gets a normal tool row (ack text) so replay
                # stays paired; other tool_calls of this response dispatch
                # for real (their results ride along into the target's
                # context via history)
                from nexus.context import DeferredModuleSwitch

                cxt.add_message(
                    "assistant",
                    encode_tool_call_content(content or "", tool_calls),
                    stage="agent",
                )
                messages.append({"role": "assistant", "content": content or None,
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "")
                    call_id = tc.get("id", "")
                    if name == DEFER_TOOL_NAME:
                        result_content = json.dumps(
                            {"ok": True,
                             "message": f"已登记延迟切换至 {target}，本轮请继续以当前身份回答完用户"},
                            ensure_ascii=False)
                    else:
                        tool_result = _execute_tool(name, _parse_args(tc))
                        result_content = tool_result
                    cxt.add_message("tool", result_content, stage="agent",
                                    metadata={"tool_name": name,
                                              "tool_call_id": call_id})
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": result_content})

                cxt.actions.append(DeferredModuleSwitch(
                    target_module_code=target,
                    reason=defer_reason,
                    source="projection",
                ))
                logger.info(
                    "[defer] %s ⇒ %s（延迟切换已写入 actions，轮末生效）",
                    module.module_code, target,
                )
                # P6 on_transfer + P7 on_agent_end: defer exit (point names
                # retained — the machinery is the successor of transfer)
                if hooks:
                    fire(hooks, "on_transfer", TransferEvent(
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        round_idx=round_idx, target=target,
                        reason=defer_reason))
                    fire(hooks, "on_agent_end", AgentEndEvent(
                        session_id=cxt.session_id,
                        module_code=module.module_code,
                        rounds=round_idx + 1, outcome="transfer",
                        transfer_target=target))
                _emit_round(ec.stream, "transfer", round_idx)
                # Loop continues: the model answers the user in the next
                # round with its projection knowledge (the switch itself is
                # end-of-turn; if the model instead replies directly, the
                # pending switch still applies)
                continue

            # Ordinary tool calls: P4 rewrite -> main-flow validation -> execute
            # -> P5 rewrite -> append to history
            _dispatch_tool_calls(
                cxt, module, messages, content, tool_calls,
                hooks, allowed_names, lent_by, round_idx)
            _emit_round(ec.stream, "tool", round_idx)

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
        _emit_round(ec.stream, "max_rounds", _MAX_TOOL_ROUNDS - 1)
        return TurnResult(content="抱歉，处理超时，请稍后重试。")


# ---------------------------------------------------------------------------
# Plan-⑥ defer tool (projection adjacency's exit signal)
# ---------------------------------------------------------------------------

DEFER_TOOL_NAME = "defer_to_module"


def _projection_targets(module, pattern, cxt) -> set:
    """Adjacency targets served via projection (effective enable_project).

    enable_project=False sub-modules are jump targets (ROUTE-side NLU jumps
    / custom executors), NOT defer candidates; force-projected modules
    (anti-ping-pong records) count as projection-served.
    """
    from nexus.engine.chat import _effective_enable_project

    module_map = cxt.module_map if cxt is not None else {}
    targets = set()
    for link in getattr(module, "sub_modules", None) or []:
        target = module_map.get(link.get("target"))
        if target is None:
            continue
        if _effective_enable_project(target, cxt):
            targets.add(target.module_code)
    return targets


def _build_defer_tool(module, pattern, cxt) -> list:
    """Build the single generic defer_to_module tool from the
    projection-served adjacency (empty when there is none)."""
    targets = sorted(_projection_targets(module, pattern, cxt))
    if not targets:
        return []
    return [{
        "type": "function",
        "function": {
            "name": DEFER_TOOL_NAME,
            "description": (
                "登记「延迟切换」：本轮继续以当前身份用投影知识回答用户，"
                "回答完成后，后续轮次以目标模块为底座继续服务。"
                "适用：该域需要多轮深入流程。"
                "不适用：一句话或一次工具能解决的请求——那类直接自己处理。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "module_code": {
                        "type": "string",
                        "enum": targets,
                        "description": "目标模块 code（仅限列出的投影子模块）",
                    },
                    "reason": {
                        "type": "string",
                        "description": "切换原因及已收集的用户信息摘要，供下一轮底座无缝承接",
                    },
                },
                "required": ["module_code", "reason"],
            },
        },
    }]


# ---------------------------------------------------------------------------
# Plan-⑤ streaming helpers
# ---------------------------------------------------------------------------

def _stream_round(provider, messages, model, temperature, max_tokens,
                  stream_emitter, tools=None):
    """One agent-loop LLM round, streamed: consume LLMChunks, forward text
    deltas optimistically (when an emitter is attached), and aggregate the
    round into the legacy dict (tool dispatch needs merged tool_calls).

    Duck-typed providers without ``chat_completion_stream`` (test stubs /
    legacy custom providers) fall back to ``chat_completion`` — no deltas
    forwarded, the result shape is identical."""
    from nexus.llm.aggregate import collect_stream

    kwargs = {}
    if tools:
        kwargs = {"tools": tools, "tool_choice": "auto"}

    if not hasattr(provider, "chat_completion_stream"):
        return provider.chat_completion(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, **kwargs)

    chunks = provider.chat_completion_stream(
        messages=messages, model=model, temperature=temperature,
        max_tokens=max_tokens, **kwargs)
    if stream_emitter is None:
        return collect_stream(chunks)

    def _tap():
        for chunk in chunks:
            if chunk.text:
                stream_emitter.emit_delta(chunk.text)
            yield chunk

    return collect_stream(_tap())


def _emit_round(stream_emitter, outcome: str, round_idx: int) -> None:
    """Emit a round-boundary event (no-op without an attached emitter)."""
    if stream_emitter is not None:
        stream_emitter.emit_round(outcome, round_idx)


from nexus.engine.loop import run_agent  # noqa: E402,F401 -- test anchor re-export
