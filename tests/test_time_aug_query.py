"""TimeAugQueryRewriter — tests for deterministic time-augmentation query rewriting.

Contract:
- pure rules, zero LLM: with a time entity, the annotation is appended into
  rewritten_queries[0]
- without a time entity, rewritten_queries = [the original query] (same
  fallback as the LLM version)
- time_base comes from ctx.metadata["time_base"] (current time when not injected)
- slot-mechanism compatible: passes is_valid_stage and is reachable through
  the query slot's two-layer resolution (declared by string code,
  resolved node > pattern)
"""

import time as _time

import pytest

from async_utils import arun

from atoms.augmentation import augment_time
from nexus.context import DialogueContext
from nexus.model.node import BaseNode
from atoms.stages.query import TimeAugQueryRewriter
from nexus.pipeline import is_valid_stage, resolve_execution_sequence

# Fixed base: 2026-09-03 10:00:00 (Thursday) — next Monday = 2026-09-07
TIME_BASE = _time.mktime(_time.strptime("2026-09-03 10:00:00", "%Y-%m-%d %H:%M:%S"))


def _ctx(query="我下周一可以去", time_base=TIME_BASE):
    ctx = DialogueContext(session_id="t", user_query=query)
    if time_base is not None:
        ctx.metadata["time_base"] = time_base
    return ctx


def test_valid_stage_duck_type():
    assert is_valid_stage(TimeAugQueryRewriter())


def test_time_entity_augmented():
    ctx = arun(TimeAugQueryRewriter().execute(_ctx()))
    assert ctx.rewritten_queries == ["我下周一(2026-09-07)可以去"]


def test_no_time_entity_passthrough():
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="这个多少钱")))
    assert ctx.rewritten_queries == ["这个多少钱"]


def test_past_time_not_augmented():
    # entirely in the past (base 2026-09-03): "last Wednesday" = 2026-08-26
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="我上周三去过")))
    assert ctx.rewritten_queries == ["我上周三去过"]


def test_past_time_span_not_augmented():
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="昨天下午三点到五点")))
    assert ctx.rewritten_queries == ["昨天下午三点到五点"]


def test_beyond_two_weeks_not_augmented():
    # badcase: "week after next" = 2026-09-14~2026-09-20, its end is 17 days past the
    # base (2026-09-03) — beyond the 2-week window, keep the original wording
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="我看下下周的")))
    assert ctx.rewritten_queries == ["我看下下周的"]


def test_far_future_month_not_augmented():
    # "next month" = 2026-10, ends ~8 weeks out — beyond the 2-week window
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="下个月呢")))
    assert ctx.rewritten_queries == ["下个月呢"]


def test_next_week_within_window_augmented():
    # "next week" = 2026-09-07~2026-09-13, ends within 2 weeks — still augmented
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="我看下周的")))
    assert ctx.rewritten_queries == ["我看下周(2026-09-07~2026-09-13)的"]


def test_mixed_span_crossing_now_augmented():
    # "last week through this Friday" starts in the past but ends 2026-09-04 (future) — augmented
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="上周到这周五怎么样")))
    assert ctx.rewritten_queries == ["上周到这周五(2026-08-24~2026-09-04)怎么样"]


def test_time_span_augmented():
    ctx = arun(TimeAugQueryRewriter().execute(_ctx(query="明天下午3点到5点有空吗")))
    assert ctx.rewritten_queries == ["明天下午3点到5点(2026-09-04 15:00~17:00)有空吗"]


def test_time_base_defaults_to_now(monkeypatch):
    # Without an injected time_base, augment_time's default (current time)
    # applies — use monkeypatch to verify the time_base passed to augment_time
    # is None
    calls = {}

    def _fake_augment(text, time_base=None):
        calls["time_base"] = time_base
        return text

    monkeypatch.setattr("atoms.stages.query.time_aug.augment_time", _fake_augment)
    ctx = DialogueContext(session_id="t", user_query="下周一发货吗")
    arun(TimeAugQueryRewriter().execute(ctx))
    assert calls["time_base"] is None
    assert ctx.rewritten_queries == ["下周一发货吗"]


def test_query_slot_resolves_to_time_aug_rewriter():
    """Two-layer slot resolution: the node-level "time_aug_query" code is hit
    by the query slot of the pattern skeleton."""
    import atoms.stages  # noqa: F401 -- registers the named stage codes
    from nexus.model.pattern import Pattern

    ctx = DialogueContext(session_id="t", user_query="q")
    ctx.current_node_code = "n1"
    n1 = BaseNode(code="n1", name="节点一",
                  stages={"query": "time_aug_query"})
    pattern = Pattern(code="pt", name="t", description="t",
                      pattern_type="fsm", nodes=[n1],
                      stages=[{"query": None}])
    sequence = resolve_execution_sequence(ctx, n1, pattern)
    assert [(slot, type(stage).__name__) for slot, stage in sequence] == [
        ("query", "TimeAugQueryRewriter")]


def test_query_slot_resolves_via_pattern_skeleton():
    """The same builtin code declared as the skeleton value resolves too (the
    pattern-layer of the two-layer resolution)."""
    import atoms.stages  # noqa: F401 -- registers the named stage codes
    from nexus.model.pattern import Pattern

    ctx = DialogueContext(session_id="t2", user_query="q")
    ctx.current_node_code = "n1"
    n1 = BaseNode(code="n1", name="节点一")
    pattern = Pattern(code="pt2", name="t", description="t",
                      pattern_type="fsm", nodes=[n1],
                      stages=[{"query": "time_aug_query"}])
    sequence = resolve_execution_sequence(ctx, n1, pattern)
    assert [(slot, type(stage).__name__) for slot, stage in sequence] == [
        ("query", "TimeAugQueryRewriter")]


def test_augment_time_consistency():
    """The rewrite result matches a direct augment_time call (passthrough contract)."""
    for query in ("我下周一可以去", "这个多少钱"):
        ctx = arun(TimeAugQueryRewriter().execute(_ctx(query=query)))
        assert ctx.rewritten_queries == [augment_time(query, time_base=TIME_BASE)]
