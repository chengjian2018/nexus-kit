"""Time-augmentation query rewrite stage — pure rules, zero LLM.

Reuses ``augmentation.augment_time`` (jionlp time parsing) to append a readable
time annotation after time entities in the original text; the rewritten result
is written to ``ctx.rewritten_queries``:

    "I can go next Monday" -> "I can go next Monday(2026-09-07)"

An alternative to the LLM-based ``QueryRewriter`` (query.py), selectable via the
``query`` slot attribute on pattern/module/node (three-layer deferred resolution
in stage_slots.py). Does not inherit ``BaseQueryRewriter``: that base class is
bound to the LLM flow (prompt_build / _call_llm / retry); a pure-rule rewrite
only needs ``PipelineStage.execute``.
"""

import logging
from typing import Optional

from atoms.augmentation import augment_time
from nexus.context import DialogueContext, PipelineStage

logger = logging.getLogger(__name__)


class TimeAugQueryRewriter(PipelineStage):
    """Deterministic query rewrite: time-entity augmentation; returns the text
    unchanged when it contains no time entity.

    The base timestamp for relative times (today / next week etc.) comes from
    ``ctx.metadata["time_base"]`` (injected by the channel/caller, e.g. the
    moment the message was sent); falls back to the current time when absent.
    """

    stage_name = "time_aug_query_rewrite"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        time_base: Optional[float] = ctx.metadata.get("time_base")
        augmented = augment_time(ctx.user_query, time_base=time_base)

        ctx.rewritten_queries = [augmented]
        logger.info(
            "TimeAug Query Rewrite 完成: session=%s, augmented=%s",
            ctx.session_id, augmented != ctx.user_query,
        )
        return ctx
