"""Default AGENT node executor — the ReAct tool loop (plan-⑧ node form).

The AGENT graph runtime dispatches each node here by default (plugin
kind="executor" / code="default_loop"; resolution node.plugins["loop"] >
pattern.plugins["loop"] > this code). The loop body: messages build →
LLM rounds with tools → tool dispatch → final content. Routing
(``TurnResult.next``) is NOT emitted by this executor — multi-node graphs
declare routing executors of their own (a node with successors whose
executor returns no next terminates the run, see chat._run_agent_graph).
"""

import logging

from nexus.engine.agent_hooks import (
    AgentEndEvent,
    AgentStartEvent,
    LLMCallEvent,
    LLMResponseEvent,
    ToolResultEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
)
from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.loop import (
    TurnResult,
    _dispatch_tool_calls,
    _parse_args,
    _resolve_tools,
    append_force_close_suffix,
    warn_prompt_length,
)
from nexus.engine.messages import build_agent_messages
from nexus.engine.tool_context import tool_call_context
from nexus.llm.resolve import build_provider

logger = logging.getLogger(__name__)

# Max tool calling rounds to prevent infinite loops
_MAX_TOOL_ROUNDS = 10


class DefaultLoopExecutor(NodeExecutor):
    """AGENT node executor: ReAct tool loop, replies directly. Tool
    availability = node.use_tools ∩ pattern.allow_toolset 工具集（both
    deny-by-default，见 nexus/engine/loop.py::_resolve_tools）。"""

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        cxt = ec.cxt
        node = ec.node
        pattern = ec.pattern
        force_close = ec.force_close
        llm_config = cxt.llm_config or {}
        provider = build_provider(llm_config)

        # Agent loop hooks (node layer replaces the pattern declaration
        # wholesale; when empty, every hook point is a zero-overhead
        # pass-through)
        hooks = resolve_agent_hooks(node, pattern)

        # P1 on_agent_start: fetched and injected before the loop and messages
        # assembly. Fragments reach the builder via extra_blocks (the contract
        # requires including them); hooks do not write to cxt
        fragments = collect_fragments(
            hooks,
            AgentStartEvent(session_id=cxt.session_id,
                            node_code=node.code, cxt=cxt),
        ) if hooks else []

        # MCP servers register asynchronously in the background at startup:
        # if the first turn races ahead of connection completion, the set
        # resolved here would be missing the MCP tools. Wait for the
        # connection's final state — zero overhead when no server is
        # configured (mcp_tool swallows internally, never blocking)
        from atoms.tools.mcp_tool import ensure_mcp_ready
        await ensure_mcp_ready()

        tools = _resolve_tools(node, pattern)
        allowed_names = {t.get("function", {}).get("name", "") for t in tools}

        # Integrated messages build (system content and list assembly share one
        # source): node.messages_builder > pattern.messages_builder > default
        # three-segment layout. On a wait_human resume the explicit query
        # slot already carries the resuming message (cxt.user_query IS the
        # resume input — begin_turn wrote it), and the suspending turn's
        # question + wait reply replay as cross-turn history — no special
        # casing needed here.
        messages = build_agent_messages(node, cxt, pattern=pattern,
                                        extra_blocks=fragments)
        # force_close close-out suffix is enforced framework-side (control-flow
        # semantics; no builder may break it)
        if force_close:
            append_force_close_suffix(messages)
        warn_prompt_length(messages, cxt, node)

        model = llm_config["model"]
        temperature = llm_config.get("temperature", 0.7)
        max_tokens = llm_config.get("max_tokens", 2048)

        # Context-bound tools (delegate_task / read_tasks) inherit this
        # loop's llm_config, the pattern's toolset grant, and the session id
        # (task-list scoping) via the ambient contextvar; custom loops that
        # skip this leave those tools on their fallback path
        with tool_call_context(
                llm_config, getattr(pattern, "allow_toolset", None) or [],
                session_id=cxt.session_id):
            for round_idx in range(_MAX_TOOL_ROUNDS):
                logger.info(
                    "Agent loop 第 %d 轮: session=%s, node=%s, tools=%d",
                    round_idx + 1, cxt.session_id, node.code, len(tools),
                )

                # P2 on_llm_call: before each LLM call (messages passed by reference,
                # read-only discipline)
                if hooks:
                    fire(hooks, "on_llm_call", LLMCallEvent(
                        session_id=cxt.session_id, node_code=node.code,
                        round_idx=round_idx, messages=messages, model=model))

                result = await _stream_round(
                    provider, messages, model, temperature, max_tokens,
                    ec.stream, tools=tools if tools else None,
                )

                content = result.get("content", "") or ""
                tool_calls = result.get("tool_calls", []) or []

                # P3 on_llm_response: after each LLM response
                if hooks:
                    fire(hooks, "on_llm_response", LLMResponseEvent(
                        session_id=cxt.session_id, node_code=node.code,
                        round_idx=round_idx, content=content,
                        tool_calls=tool_calls))

                # No tool calls -> direct answer
                if not tool_calls:
                    logger.info("Agent loop 完成，共 %d 轮", round_idx + 1)
                    _emit_round(ec.stream, "final", round_idx)
                    # P7 on_agent_end: direct-answer exit
                    if hooks:
                        fire(hooks, "on_agent_end", AgentEndEvent(
                            session_id=cxt.session_id,
                            node_code=node.code,
                            rounds=round_idx + 1, outcome="reply", reply=content))
                    return TurnResult(content=content)

                # Ordinary tool calls: P4 rewrite -> main-flow validation -> execute
                # -> P5 rewrite -> append to history
                await _dispatch_tool_calls(
                    cxt, node, messages, content, tool_calls,
                    hooks, allowed_names, round_idx,
                    stream=ec.stream)
                _emit_round(ec.stream, "tool", round_idx)

        logger.warning(
            "Agent loop 达到最大轮次 %d，强制终止: session=%s",
            _MAX_TOOL_ROUNDS, cxt.session_id,
        )
        # P7 on_agent_end: max-rounds-exceeded exit
        if hooks:
            fire(hooks, "on_agent_end", AgentEndEvent(
                session_id=cxt.session_id, node_code=node.code,
                rounds=_MAX_TOOL_ROUNDS, outcome="max_rounds",
                reply="抱歉，处理超时，请稍后重试。"))
        _emit_round(ec.stream, "max_rounds", _MAX_TOOL_ROUNDS - 1)
        return TurnResult(content="抱歉，处理超时，请稍后重试。")


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

async def _stream_round(provider, messages, model, temperature, max_tokens,
                        stream_emitter, tools=None):
    """One agent-loop LLM round, streamed: consume LLMChunks, forward text
    deltas optimistically (when an emitter is attached), and aggregate the
    round into the legacy dict (tool dispatch needs merged tool_calls).

    Duck-typed providers without ``achat_completion_stream`` (test stubs /
    legacy custom providers) fall back to ``achat_completion`` — no deltas
    forwarded, the result shape is identical."""
    from nexus.llm.aggregate import acollect_stream

    kwargs = {}
    if tools:
        kwargs = {"tools": tools, "tool_choice": "auto"}

    if not hasattr(provider, "achat_completion_stream"):
        return await provider.achat_completion(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, **kwargs)

    chunks = provider.achat_completion_stream(
        messages=messages, model=model, temperature=temperature,
        max_tokens=max_tokens, **kwargs)
    if stream_emitter is None:
        return await acollect_stream(chunks)

    async def _tap():
        async for chunk in chunks:
            if chunk.text:
                stream_emitter.emit_delta(chunk.text)
            yield chunk

    return await acollect_stream(_tap())


def _emit_round(stream_emitter, outcome: str, round_idx: int) -> None:
    """Emit a round-boundary event (no-op without an attached emitter)."""
    if stream_emitter is not None:
        stream_emitter.emit_round(outcome, round_idx)
