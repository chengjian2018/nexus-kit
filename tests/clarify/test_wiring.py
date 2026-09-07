"""Framework wiring tests — pipeline insertion, NLG skip, slots not merged, node unchanged."""

import pytest

from nexus.engine.chat import _default_skeleton, _handle_node_transition
from nexus.context import DialogueContext
from nexus.model.module import FSMModule, RouteModule
from atoms.stages.nlg import FSMNLG
from nexus.pipeline import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    resolve_stage,
)


def make_fsm_module(enable_clarify):
    return FSMModule(
        module_code="m_fsm",
        module_nodes=[type("N", (), {"node_code": "n1", "nlu_stage": None,
                                      "nlg_stage": None})()],
        enable_clarify=enable_clarify,
    )


class TestBuildStages:

    def test_default_skeleton_is_four_slots(self):
        stages = _default_skeleton(make_fsm_module(False))
        assert [type(s).__name__ for s in stages] == [
            "PreRecallSlot", "QuerySlot", "PostRecallSlot", "GenerateSlot",
        ]

    def test_enabled_module_clarify_inserted_between_generate_parts(self):
        class _Clarify:
            stage_name = "my_clarify"

            def execute(self, ctx):
                return ctx

        module = make_fsm_module(True)
        module.clarify_stage = _Clarify()
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_module_code = "m_fsm"
        ctx.current_node_code = "n1"
        ctx.node_map = {"n1": module.module_nodes[0]}

        names = [s.stage_name for s in
                 resolve_stage(GenerateSlot(), ctx, module, None)]

        assert names == ["generate_nlu_part", "my_clarify",
                         "generate_nlg_part"]

    def test_route_module_never_has_clarify_stage(self):
        route = RouteModule(module_code="m_route")
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_module_code = "m_route"
        ctx.current_node_code = "n1"
        ctx.node_map = {"n1": type("N", (), {"node_code": "n1"})()}

        names = [s.stage_name for s in
                 resolve_stage(GenerateSlot(), ctx, route, None)]

        # ROUTE and FSM are the same shape by default: menu-node advance /
        # jump detection is done by the chat layer after the nlu part
        assert names == ["generate_nlu_part", "generate_nlg_part"]


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
        module = make_fsm_module(True)
        _handle_node_transition(ctx, module)
        assert ctx.filled_slots == {}
        assert ctx.current_node_code == "n1"
