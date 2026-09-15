"""Default FSM-pattern executor.

Thin orchestration: node resolution → R3 LLM refresh → stages (two-layer
node > pattern skeleton) → next_node transition. The kernel toolbox
functions (_resolve_entry_node / _refresh_llm_config / _run_stages /
_fsm_node_transition) stay in nexus.engine.chat — they are the R1-R4 patch
anchors (tests patch "nexus.engine.chat.get_llm_config") — so this executor
imports them from the kernel (legal layering: atoms → nexus).
"""

import logging

from nexus.engine.execution import ExecutionContext, NodeExecutor
from nexus.engine.turn_result import TurnResult

logger = logging.getLogger(__name__)


class DefaultFSMExecutor(NodeExecutor):
    """FSM executor: one pattern turn = node resolution → R3 refresh →
    stages → next_node jump (one node per turn; no budget).

    FSM produces no control-flow events (clarify handled inside the loop,
    node jumps at end of turn).
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        from nexus.engine.chat import (
            _fsm_node_transition,
            _refresh_llm_config,
            _resolve_entry_node,
            _run_stages,
        )

        cxt = ec.cxt
        pattern = ec.pattern

        _resolve_entry_node(cxt, pattern)
        node = cxt.get_current_node()

        # R3: after node resolution, refresh the LLM config by node —
        # resolved through the chat namespace so the R3 patch
        # anchor keeps working
        _refresh_llm_config_by_node(ec)

        node_before = cxt.current_node_code
        await _run_stages(cxt, node, pattern, force_close=ec.force_close)

        # next_node jump (the clarify-turn guard lives inside the
        # transition function)
        _fsm_node_transition(cxt, pattern)
        _emit_node_jump(ec.stream, node_before, cxt.current_node_code)

        # Terminal node action (reserved channel)
        cur_node = pattern.node_map.get(cxt.current_node_code)
        if cur_node is not None and getattr(cur_node, "is_end", False):
            cxt.actions.append({"conversation_end": True})
            _emit_trace(ec.stream, "conversation_end",
                        node_code=cxt.current_node_code or "")

        # FSM reply: when the stage streamed it natively (unified
        # reply-field tap / NLG token stream), the deltas already reached
        # the consumer — emit only what was NOT forwarded
        nlg_result = cxt.nlg_result or {}
        content = nlg_result.get("content", "")
        _emit_reply_delta(ec.stream, content)
        return TurnResult(content=content)


def _emit_trace(stream_emitter, event: str, **data) -> None:
    """Emit a trace event (no-op without an attached emitter)."""
    if stream_emitter is not None:
        stream_emitter.emit_trace(event, **data)


def _emit_reply_delta(stream_emitter, content: str) -> None:
    """Forward the reply as a delta — unless the stage already streamed it.

    The unified/NLG stages forward reply text natively while the LLM runs
    (stream_llm_reply records what was forwarded in the
    current_streamed_reply contextvar, same task context); identical text
    here would duplicate on the consumer's screen. Fallback / replaced
    wording still emits in full.
    """
    from nexus.engine.streaming import current_streamed_reply

    streamed = current_streamed_reply.get()
    if not content or content == streamed or streamed.strip() == content:
        return
    if stream_emitter is not None:
        stream_emitter.emit_delta(content)


def _emit_node_jump(stream_emitter, before: str, after: str) -> None:
    """node_jump trace — only when the node actually moved (suppress noise
    on stay-in-place turns so exact event-sequence pins stay intact)."""
    if stream_emitter is not None and after and after != before:
        stream_emitter.emit_trace(
            "node_jump", node_code=after,
            from_node=before or "", to_node=after)


def _refresh_llm_config_by_node(ec):
    """R3 refresh via a session-like shim (the kernel helper reads
    session.pattern / session.pattern_code / session.cxt; ec carries the
    same data — pattern_code included, for the app layered lookup)."""
    from nexus.engine.chat import _refresh_llm_config

    class _Shim:
        """Minimal session stand-in for the kernel R1-R4 helper."""

        def __init__(self, cxt, pattern_code, pattern):
            self.cxt = cxt
            self.pattern_code = pattern_code
            self.pattern = pattern

    _refresh_llm_config(
        _Shim(ec.cxt, ec.cxt.metadata.get("pattern_code", ""), ec.pattern),
        node_code=ec.cxt.current_node_code,
    )
