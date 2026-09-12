"""Framework wiring tests — skeleton insertion, NLG skip, slots not merged,
node unchanged (plan-⑧ two-layer form: clarify is a skeleton slot declared
via node.stages ({"clarify": code}); the default skeleton keeps clarify=None
(opt-in), the pattern skeleton must carry the slot for it to run).
"""

from async_utils import arun
import pytest

from nexus.engine.chat import _fsm_node_transition
from nexus.context import DialogueContext
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.pipeline import default_skeleton, resolve_execution_sequence

from stage_stubs import register_stage_stub


def make_fsm_pattern(stages=None, node_stages=None):
    n1 = BaseNode(code="n1", name="n1", stages=node_stages)
    return Pattern(code="pw", name="t", description="t",
                   pattern_type="fsm", nodes=[n1], stages=stages)


class TestBuildStages:

    def test_default_skeleton_is_six_slots(self):
        skeleton = default_skeleton()
        assert [list(e.keys())[0] for e in skeleton] == [
            "pre_recall", "query", "post_recall", "nlu", "clarify", "nlg",
        ]

    def test_declared_clarify_sits_between_nlu_and_nlg(self):
        class _Clarify:
            stage_name = "my_clarify"

            async def execute(self, ctx):
                return ctx

        cl_code = register_stage_stub(_Clarify)
        # skeleton with a clarify slot present; nlu/nlg stubbed to no-op codes
        ran = []

        class _Noop:
            stage_name = "noop"

            async def execute(self, ctx):
                ran.append(self.stage_name)
                return ctx

        nlu_code = register_stage_stub(_Noop)
        nlg_code = register_stage_stub(_Noop)
        # the clarify declaration lives on the NODE (the plan-⑧ replacement
        # of the module-level enable_clarify)
        pattern = make_fsm_pattern(
            stages=[{"nlu": nlu_code}, {"clarify": None}, {"nlg": nlg_code}],
            node_stages={"clarify": cl_code},
        )
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.node_map = pattern.node_map

        sequence = resolve_execution_sequence(ctx, pattern.nodes[0], pattern)
        names = [getattr(s, "stage_name", "?") for _, s in sequence]
        assert names == ["noop", "my_clarify", "noop"]
        for _, stage in sequence:
            arun(stage.execute(ctx))
        assert ran == ["noop", "noop"]  # nlu + nlg executed (clarify is a no-op here)

    def test_undeclared_clarify_never_inserted(self):
        pattern = make_fsm_pattern()  # default skeleton, no clarify declaration
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.node_map = pattern.node_map

        sequence = resolve_execution_sequence(ctx, pattern.nodes[0], pattern)
        slots = [slot for slot, _ in sequence]
        assert "clarify" not in slots

    def test_node_clarify_without_skeleton_slot_never_runs(self):
        """The node layer only overrides slots the skeleton carries: a node
        declaring clarify under a skeleton without the slot never runs it."""
        cl_code = register_stage_stub(lambda: type("_C", (), {
            "stage_name": "never",
            "execute": staticmethod(lambda ctx: ctx)})())
        pattern = make_fsm_pattern(
            stages=[{"nlu": None}, {"nlg": None}],   # no clarify slot
            node_stages={"clarify": cl_code},
        )
        ctx = DialogueContext(session_id="t2", user_query="q")
        ctx.current_node_code = "n1"
        ctx.node_map = pattern.node_map
        sequence = resolve_execution_sequence(ctx, pattern.nodes[0], pattern)
        assert "clarify" not in [slot for slot, _ in sequence]


class TestNlgSkipGuard:

    def test_fsmnlg_skips_when_clarify_triggered(self):
        from atoms.stages.nlg import FSMNLG
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        ctx.nlg_result = {"content": "[clarify:kb] 已生成"}
        calls = []
        FSMNLG._call_llm = lambda self, prompt, cfg=None: calls.append(prompt) or "x"
        try:
            arun(FSMNLG().execute(ctx))
        finally:
            del FSMNLG._call_llm
        assert calls == []
        assert ctx.nlg_result == {"content": "[clarify:kb] 已生成"}


class TestTransitionGuard:

    def test_clarify_turn_skips_slot_merge_and_keeps_node(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_node_code = "n1"
        ctx.nlu_result = {"next_node": "clarify",
                          "slots": {"topic": "费用", "keywords": ["收费"]}}
        ctx.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        pattern = make_fsm_pattern(node_stages={"clarify": "clarify_default"})
        _fsm_node_transition(ctx, pattern)
        assert ctx.filled_slots == {}
        assert ctx.current_node_code == "n1"
