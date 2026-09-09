"""Framework wiring tests — skeleton insertion, NLG skip, slots not merged, node unchanged.

Post-plan-②: clarify is a skeleton slot declared via module.stages
({"clarify": code}); the default skeleton keeps clarify=None (opt-in).
"""

import pytest

from nexus.engine.chat import _default_skeleton, _handle_node_transition
from nexus.context import DialogueContext
from nexus.model.module import FSMModule, RouteModule
from atoms.stages.nlg import FSMNLG
from nexus.pipeline import resolve_execution_sequence

from stage_stubs import register_stage_stub


def make_fsm_module(stages=None):
    return FSMModule(
        module_code="m_fsm",
        module_nodes=[type("N", (), {"node_code": "n1", "nlu_stage": None,
                                      "nlg_stage": None})()],
        stages=stages,
    )


def _pattern_of(module):
    from nexus.model.pattern import Pattern
    return Pattern(code="pw", name="t", description="t",
                   entry_module_code=module.module_code, modules=[module])


class TestBuildStages:

    def test_default_skeleton_is_six_slots(self):
        skeleton = _default_skeleton(make_fsm_module())
        assert [list(e.keys())[0] for e in skeleton] == [
            "pre_recall", "query", "post_recall", "nlu", "clarify", "nlg",
        ]

    def test_declared_clarify_sits_between_nlu_and_nlg(self):
        class _Clarify:
            stage_name = "my_clarify"

            def execute(self, ctx):
                return ctx

        cl_code = register_stage_stub(_Clarify)
        module = make_fsm_module(stages={"clarify": cl_code})
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_module_code = "m_fsm"
        ctx.current_node_code = "n1"
        ctx.node_map = {"n1": module.module_nodes[0]}

        # skeleton with clarify slot present; nlu/nlg stubbed to no-op codes
        ran = []

        class _Noop:
            stage_name = "noop"

            def execute(self, ctx):
                ran.append(self.stage_name)
                return ctx

        nlu_code = register_stage_stub(_Noop)
        nlg_code = register_stage_stub(_Noop)
        from nexus.model.pattern import Pattern
        pattern = Pattern(
            code="pw2", name="t", description="t",
            entry_module_code="m_fsm", modules=[module],
            stages=[{"nlu": nlu_code}, {"clarify": None}, {"nlg": nlg_code}],
        )
        sequence = resolve_execution_sequence(ctx, module, pattern)
        names = [getattr(s, "stage_name", "?") for _, s in sequence]
        assert names == ["noop", "my_clarify", "nlg(deferred)"]
        for _, stage in sequence:
            stage.execute(ctx)
        assert ran == ["noop", "noop"]  # clarify + nlu/nlg executed

    def test_undeclared_clarify_never_inserted(self):
        module = make_fsm_module()  # no clarify declaration
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_module_code = "m_fsm"
        ctx.current_node_code = "n1"
        ctx.node_map = {"n1": module.module_nodes[0]}

        sequence = resolve_execution_sequence(ctx, module, _pattern_of(module))
        slots = [slot for slot, _ in sequence]
        assert "clarify" not in slots


class TestNlgSkipGuard:

    def test_fsmnlg_skips_when_clarify_triggered(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.metadata["clarify"] = {"triggered": True, "mode": "kb"}
        ctx.nlg_result = {"content": "[clarify:kb] 已生成"}
        calls = []
        FSMNLG._call_llm = lambda self, prompt, cfg=None: calls.append(prompt) or "x"
        try:
            FSMNLG().execute(ctx)
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
        module = make_fsm_module(stages={"clarify": "clarify_default"})
        _handle_node_transition(ctx, module)
        assert ctx.filled_slots == {}
        assert ctx.current_node_code == "n1"
