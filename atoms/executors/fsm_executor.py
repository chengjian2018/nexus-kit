"""Default FSM-module executor (moved from
nexus/engine/chat.py::_run_fsm_pipeline, behavior unchanged).

Thin orchestration: node resolution → R3 LLM refresh → stages → next_node
transition. The kernel toolbox functions (_resolve_entry_node /
_refresh_llm_config / _run_stages / _fsm_node_transition) stay in
nexus.engine.chat — they are the R1-R4 patch anchors
(tests patch "nexus.engine.chat.get_llm_config") — so this executor imports
them from the kernel (legal layering: atoms → nexus).
"""

import logging

from nexus.engine.execution import ExecutionContext, ModuleExecutor
from nexus.engine.turn_result import TurnResult

logger = logging.getLogger(__name__)


class DefaultFSMExecutor(ModuleExecutor):
    """FSM executor: one module turn = node resolution → R3 refresh → stages
    → next_node jump.

    FSM produces no jump events (clarify handled inside the loop, node jumps
    at end of turn), so _run_stages always returns None.
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        from nexus.engine.chat import (
            _fsm_node_transition,
            _refresh_llm_config,
            _resolve_entry_node,
            _run_stages,
        )

        cxt = ec.cxt
        module = ec.module
        pattern = ec.pattern

        _resolve_entry_node(cxt, module)

        # R3: after node resolution, refresh the LLM config by module+node
        # (spec §4) — resolved through the chat namespace so the R3 patch
        # anchor keeps working
        _refresh_llm_config_by_node(ec, module)

        await _run_stages(cxt, module, pattern, force_close=ec.force_close)

        # FSM: next_node jump (the clarify-turn guard lives inside the
        # transition function)
        _fsm_node_transition(cxt, module)

        # Terminal node action (reserved channel)
        next_node = pattern.node_map.get(cxt.current_node_code)
        if next_node is not None and getattr(next_node, "is_end", False):
            cxt.actions.append({"conversation_end": True})

        nlg_result = cxt.nlg_result or {}
        return TurnResult(content=nlg_result.get("content", ""))


def _refresh_llm_config_by_node(ec, module):
    """R3 refresh via a session-like shim (the kernel helper reads
    session.pattern_code / session.cxt; ec carries the same data)."""
    from nexus.engine.chat import _refresh_llm_config

    class _Shim:
        """Minimal session stand-in for the kernel R1-R4 helper."""

        def __init__(self, cxt, pattern_code):
            self.cxt = cxt
            self.pattern_code = pattern_code

    _refresh_llm_config(
        _Shim(ec.cxt, ec.cxt.metadata.get("pattern_code", "")),
        module_code=module.module_code,
        node_code=ec.cxt.current_node_code,
    )
