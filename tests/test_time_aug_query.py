"""TimeAugQueryRewriter — tests for deterministic time-augmentation query rewriting.

Contract:
- pure rules, zero LLM: with a time entity, the annotation is appended into
  rewritten_queries[0]
- without a time entity, rewritten_queries = [the original query] (same
  fallback as the LLM version)
- time_base comes from ctx.metadata["time_base"] (current time when not injected)
- slot-mechanism compatible: passes is_valid_stage and is reachable through
  the query slot's three-layer resolution (declared by string code since
  plan-②)
"""

import time as _time

import pytest

from atoms.augmentation import augment_time
from nexus.context import DialogueContext
from nexus.model.node import BaseNode
from nexus.model.module import FSMModule
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
    ctx = TimeAugQueryRewriter().execute(_ctx())
    assert ctx.rewritten_queries == ["我下周一(2026-09-07)可以去"]


def test_no_time_entity_passthrough():
    ctx = TimeAugQueryRewriter().execute(_ctx(query="这个多少钱"))
    assert ctx.rewritten_queries == ["这个多少钱"]


def test_time_span_augmented():
    ctx = TimeAugQueryRewriter().execute(_ctx(query="明天下午3点到5点有空吗"))
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
    TimeAugQueryRewriter().execute(ctx)
    assert calls["time_base"] is None
    assert ctx.rewritten_queries == ["下周一发货吗"]


def test_query_slot_resolves_to_time_aug_rewriter():
    """Three-layer slot resolution: a module-level "time_aug_query" code is
    hit by the query slot of the skeleton."""
    import atoms.stages  # noqa: F401 -- registers the named stage codes
    ctx = DialogueContext(session_id="t", user_query="q")
    ctx.current_module_code = "m1"
    ctx.current_node_code = "n1"
    module = FSMModule(
        module_code="m1", module_name="m1", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="n1", node_name="节点一")],
        stages={"query": "time_aug_query"},
    )
    sequence = _resolve_with_skeleton(ctx, module)
    assert [(slot, type(stage).__name__) for slot, stage in sequence] == [
        ("query", "TimeAugQueryRewriter")]


def _resolve_with_skeleton(ctx, module):
    from nexus.pipeline import normalize_skeleton
    from nexus.model.pattern import Pattern
    pattern = Pattern(code="pt", name="t", description="t",
                      entry_module_code="m1", modules=[module],
                      stages=[{"query": None}])
    return resolve_execution_sequence(ctx, module, pattern)


def test_augment_time_consistency():
    """The rewrite result matches a direct augment_time call (passthrough contract)."""
    for query in ("我下周一可以去", "这个多少钱"):
        ctx = TimeAugQueryRewriter().execute(_ctx(query=query))
        assert ctx.rewritten_queries == [augment_time(query, time_base=TIME_BASE)]
