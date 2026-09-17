"""
DialogueContext field lifecycle management — the single owner of per-turn processing / cross-turn retention / incremental updates.

cxt survives across turns on the Session; field lifecycle policy used to be scattered across
the start of chat() and the transition logic of individual handlers. This module consolidates
the policy into declarative sets: change the set and you change the policy — no more scattering.

Four field categories (see the TurnLifecycle class attributes):
- PERSISTENT     : never deleted across turns (the session state itself)
- PER_TURN_RESET : reset at the start of every turn (this turn's temporary output)
- INCREMENTAL    : incremental updates (history appends, filled_slots merges); this module provides the entry points
- STAGE_MANAGED  : self-managed by the stage mechanism; the lifecycle layer never touches them
"""

import logging
from typing import Any, Dict

from nexus.context import DialogueContext

logger = logging.getLogger(__name__)


class TurnLifecycle:
    """The single owner of DialogueContext field lifecycle.

    Call-order contract (the chat-layer orchestrator is responsible for honoring it):
    1. ``begin_turn``   — exactly once per turn, before the turn's executor
                           runs (FSM stages / AGENT graph runtime)
    2. ``merge_slots``  — on demand during the FSM end-of-turn transition (incrementally merges nlu slots)
    3. ``end_turn``     — exactly once per turn, after the reply is produced

    The set is the policy: to change a field's category, edit the declaration below — no flow code changes.
    """

    # -- Cross-turn retention: never deleted ---------------------------------
    # Mostly documentation (begin_turn explicitly never touches these), stating the invariant:
    # this is the session state itself; deleting it by mistake loses state.
    PERSISTENT_FIELDS = (
        "history",              # appended incrementally (end_turn); never cleared wholesale
        "current_node_code",    # maintained by node transition (FSM) / the graph
                                # runtime's position mirror (AGENT)
        "filled_slots",         # merged incrementally (merge_slots)
        "task_basic_info",      # injected at launch; read-only throughout
        "session_id",
        "node_map",             # topology map injected at launch
        # AGENT graph state board: survives turns WHILE the graph is
        # suspended (the paused cursor IS the cross-turn state); the graph
        # runtime itself clears it on termination and re-initializes on a
        # fresh run — begin_turn never touches it
        "graph_state",
    )
    PERSISTENT_METADATA_KEYS = (
        "bargain_settings",
        "task_info",
        "llm_override",
        "pattern_code",
    )

    # -- Per-turn reset: zeroed at turn start --------------------------------
    # This turn's temporary output. Today some fields only stay clean because stages overwrite
    # them; here they are zeroed explicitly, tightening the semantics from "leftover but usually
    # overwritten" to "clean every turn".
    PER_TURN_RESULT_FIELDS = ("nlu_result", "nlg_result", "agent_result")
    PER_TURN_LIST_FIELDS = (
        "pre_recall_results",
        "rewritten_queries",
        "post_recall_results",
        "actions",              # action channel rebuilt every turn (chat layer snapshots
                                # it into ChatResult at turn end)
    )
    PER_TURN_METADATA_KEYS = (
        "unified",
    )

    # -- Stage self-managed: reset at turn start -----------------------------
    # clarify is set by ClarifyStage each turn — it should never leak across
    # turns, so it is cleared at turn start.
    STAGE_MANAGED_METADATA_KEYS = ("clarify",)

    # ------------------------------------------------------------------
    # Turn boundary
    # ------------------------------------------------------------------

    def begin_turn(self, cxt: DialogueContext, user_query: str) -> None:
        """Turn start: overwrite user_query, reset per-turn fields, clear stage-managed bookkeeping.

        Must be called exactly once per turn (before the turn's executor
        runs; never mid-run — it would wipe this turn's in-flight output).

        Also snapshots ``turn_history_start = len(history)`` (the length before adding the user
        message): default_build_messages uses it to split cross-turn history / explicit query /
        this turn's graph-run rows. That marker is a turn marker derived by begin_turn and does not
        belong to the four categories below.
        """
        cxt.user_query = user_query
        cxt.turn_history_start = len(cxt.history)

        for field_name in self.PER_TURN_RESULT_FIELDS:
            setattr(cxt, field_name, None)
        for field_name in self.PER_TURN_LIST_FIELDS:
            setattr(cxt, field_name, [])
        for key in self.PER_TURN_METADATA_KEYS:
            cxt.metadata.pop(key, None)
        for key in self.STAGE_MANAGED_METADATA_KEYS:
            cxt.metadata.pop(key, None)

        logger.debug(
            "begin_turn: session=%s query=%r（每轮字段已重置）",
            cxt.session_id, user_query,
        )

    async def end_turn(self, cxt: DialogueContext, response_text: str) -> None:
        """Turn end: append the assistant message to history (the incremental-update entry point)."""
        await cxt.add_message("assistant", response_text, stage="chat")

    # ------------------------------------------------------------------
    # Incremental updates
    # ------------------------------------------------------------------

    def merge_slots(self, cxt: DialogueContext, slots: Dict[str, Any]) -> None:
        """Incremental merge: nlu slots merged into filled_slots (later writes override the same key).

        Used by the FSM end-of-turn transition (chat._fsm_node_transition);
        consolidates the two duplicate copies previously in the chat layer.
        """
        if slots:
            cxt.filled_slots.update(slots)
            logger.info("槽位更新: %s", slots)
