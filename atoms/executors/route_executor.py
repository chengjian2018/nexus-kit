"""Default ROUTE-module executor (moved from
nexus/engine/chat.py::_run_route_pipeline, behavior unchanged).

Thin orchestration: node resolution → R3 LLM refresh → stages (with jump
detection) → end-of-turn reset to root. On a jump turn, return silently
(the chat layer's hop loop reroutes; the target module resolves its own
entry node).
"""

import logging

from nexus.engine.execution import ExecutionContext, ModuleExecutor
from nexus.engine.turn_result import TurnResult

from atoms.executors.fsm_executor import _emit_reply_delta  # noqa: E402

logger = logging.getLogger(__name__)


class DefaultRouteExecutor(ModuleExecutor):
    """ROUTE executor: root router + intent menu dispatch.

    Module jumps are detected inside _run_stages (NLU jump_module field /
    menu node jump_module config); this executor does no jump_module
    dispatch of its own. After jumping to an AGENT/FSM module, the target
    carries the following turns across turns (agent via history, FSM via
    node position) without returning to routing — the end-of-turn reset to
    root happens only while still parked in the ROUTE module (menu nodes
    have no sub_nodes; without the reset the next turn's routing candidates
    would be empty). ROUTE does not wire the clarify slot in its apps (only FSM modules
    declare it in practice; a ROUTE module declaring clarify would get it
    resolved the same way via the skeleton),
    and begin_turn already cleared clarify at start of turn, so there is no
    clarify-turn branch.
    """

    async def execute(self, ec: "ExecutionContext") -> TurnResult:
        from nexus.engine.chat import (
            _refresh_llm_config,
            _resolve_entry_node,
            _run_stages,
        )
        from nexus.engine.context_lifecycle import TurnLifecycle

        cxt = ec.cxt
        module = ec.module
        pattern = ec.pattern

        _resolve_entry_node(cxt, module)

        # R3: after node resolution, refresh the LLM config by module+node
        # (via a session shim — see fsm_executor for the pattern)
        from atoms.executors.fsm_executor import _refresh_llm_config_by_node
        _refresh_llm_config_by_node(ec, module)

        node_before = cxt.current_node_code
        jump_event = await _run_stages(cxt, module, pattern,
                                       force_close=ec.force_close)

        # Jump turn: slots were already merged at the detection point; the hop
        # loop reroutes to the target module to continue in the same turn
        # (menu advancement already switched the node — surface it before
        # the module_jump so consumers see route → target ordering)
        if jump_event is not None:
            _emit_route_hit(ec.stream, module.module_code,
                            node_before, cxt.current_node_code)
            return TurnResult()

        _emit_route_hit(ec.stream, module.module_code,
                        node_before, cxt.current_node_code)

        # Slot merge (incremental: via the lifecycle entry point)
        lifecycle = TurnLifecycle()
        slots = (cxt.nlu_result or {}).get("slots", {})
        lifecycle.merge_slots(cxt, slots)

        # End-of-turn reset to root (including force_close: jump detection is
        # skipped, but menu nodes have no sub_nodes — without the reset the
        # next turn's routing candidates would be empty)
        root_code = (module.module_nodes[0].node_code
                     if module.module_nodes else None)
        parked = cxt.current_node_code
        cxt.current_node_code = root_code
        logger.info("ROUTE 模块保持 root 节点: %s", root_code)
        # route_root trace — only when a menu node was actually parked (a
        # root→root no-op turn stays silent, same suppression contract as
        # route_hit / node_jump)
        if parked and parked != root_code:
            _emit_trace(ec.stream, "route_root",
                        module_code=module.module_code,
                        node_code=root_code or "",
                        from_node=parked, to_node=root_code or "")

        nlg_result = cxt.nlg_result or {}
        content = nlg_result.get("content", "")
        _emit_reply_delta(ec.stream, content)
        return TurnResult(content=content)


def _emit_trace(stream_emitter, event: str, **data) -> None:
    """Emit a trace event (no-op without an attached emitter)."""
    if stream_emitter is not None:
        stream_emitter.emit_trace(event, **data)


def _emit_route_hit(stream_emitter, module_code: str,
                    before: str, after: str) -> None:
    """route_hit trace — only when the turn actually landed on a menu node
    (root→root stays silent; suppression keeps exact event-sequence pins
    intact)."""
    if stream_emitter is not None and after and after != before:
        stream_emitter.emit_trace(
            "route_hit", module_code=module_code, node_code=after,
            from_node=before or "", to_node=after)
