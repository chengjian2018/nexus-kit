"""ModuleJumpEvent mechanism: detection inside the stage loop + hop-consumption rerouting.

Covers the three sources and fault tolerance of chat.ModuleJumpChannel.detect_after_stage:
- NLU directly outputs the jump_module field (nlu_jump)
- next_node hits a node's jump_module config (route_menu, advancing the menu first + R4)
- unknown target / self-loop -> ignored, remaining stages keep running
  (LLM hallucination tolerance)
"""

from unittest.mock import patch

from nexus.engine.chat import ModuleJumpChannel
from nexus.engine.session import Session
from nexus.context import DialogueContext, ModuleJumpEvent, PipelineStage
from nexus.model.module import AgentModule, FSMModule, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern


# ============================================================================
# Scaffolding
# ============================================================================

class _JumpNLU(PipelineStage):
    """Stub NLU that writes nlu_result: next_node / jump_module injected by the test case."""

    stage_name = "jump_nlu"

    def __init__(self, nlu_result):
        self.nlu_result = nlu_result

    async def execute(self, ctx):
        ctx.nlu_result = dict(self.nlu_result)
        return ctx


class _MarkerNLG(PipelineStage):
    stage_name = "marker_nlg"

    async def execute(self, ctx):
        ctx.nlg_result = {"content": "nlg_ran"}
        return ctx


def _route_with_menu(menu_jump=None):
    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    jump_module=menu_jump)
    root = BaseNode(node_code="root", node_name="根", sub_nodes=["menu_a"])
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        module_nodes=[root, menu])
    target = FSMModule(
        module_code="m1", module_name="m", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="f1", node_name="F1")])
    pattern = Pattern(code="pj", name="t", description="t",
                      entry_module_code="r1",
                      modules=[route] if menu_jump is None
                      else [route, target],
                      sub_modules=None)
    return pattern, route, target


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    session.cxt.current_module_code = "r1"
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from nexus.engine.chat import chat as chat_fn
    from async_utils import arun
    return arun(chat_fn(query=query, session_id=sid, all_sessions=sessions))


# ============================================================================
# ModuleJumpChannel.detect_after_stage unit tests (offline)
# ============================================================================

class TestDetectJump:
    def _ctx(self):
        ctx = DialogueContext(session_id="t", user_query="q")
        ctx.current_module_code = "r1"
        ctx.current_node_code = "root"
        return ctx

    def test_nlu_jump_field_wins(self):
        """nlu_result.jump_module hits directly -> nlu_jump event."""
        ctx = self._ctx()
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"next_node": "", "jump_module": "m1",
                          "reason": "售后", "slots": {}}
        event = ModuleJumpChannel.detect_after_stage(ctx, _route_mod(), before_nlu=None)
        assert event is not None
        assert event.target_module_code == "m1"
        assert event.source == "nlu_jump"
        assert event.reason == "售后"

    def test_node_jump_module_after_advance(self):
        """next_node hits a node with jump_module -> switch the node first, then a route_menu event."""
        ctx = self._ctx()
        menu = BaseNode(node_code="menu_a", node_name="A", jump_module="m1")
        root = BaseNode(node_code="root", node_name="R", sub_nodes=["menu_a"])
        ctx.node_map = {"root": root, "menu_a": menu}
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
        route = RouteModule(module_code="r1", module_name="r",
                            module_description="d", module_todo_description="t",
                            module_nodes=[root, menu])
        event = ModuleJumpChannel.detect_after_stage(ctx, route, before_nlu=None)
        assert event is not None
        assert event.target_module_code == "m1"
        assert event.source == "route_menu"
        assert ctx.current_node_code == "menu_a"

    def test_self_jump_ignored(self):
        """Jump target is the current module (self-loop) -> ignored."""
        ctx = self._ctx()
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"next_node": "", "jump_module": "r1", "slots": {}}
        assert ModuleJumpChannel.detect_after_stage(ctx, _route_mod(), None) is None

    def test_unknown_target_ignored(self):
        """Target not in module_map (LLM hallucination) -> ignored."""
        ctx = self._ctx()
        ctx.module_map = {"r1": object()}
        ctx.nlu_result = {"next_node": "", "jump_module": "ghost", "slots": {}}
        assert ModuleJumpChannel.detect_after_stage(ctx, _route_mod(), None) is None

    def test_unchanged_nlu_result_skipped(self):
        """nlu_result not updated by this stage (same object) -> no detection (avoids false positives on hop continuation)."""
        ctx = self._ctx()
        ctx.module_map = {"r1": object(), "m1": object()}
        ctx.nlu_result = {"jump_module": "m1", "slots": {}}
        assert ModuleJumpChannel.detect_after_stage(ctx, _route_mod(), ctx.nlu_result) is None


def _route_mod():
    return RouteModule(module_code="r1", module_name="r",
                       module_description="d", module_todo_description="t",
                       module_nodes=[BaseNode(node_code="root",
                                              node_name="R")])


# ============================================================================
# e2e: stage-loop interruption + hop consumption
# ============================================================================

def test_nlu_jump_breaks_stages_and_reroutes_same_turn():
    """NLU outputs jump_module -> the remaining stages are interrupted (NLG does
    not run), the source module stays silent, and the chat layer's hop
    consumption reroutes to the target module to continue in the same turn."""
    from stage_stubs import register_stage_stub

    jump_nlu_code = register_stage_stub(
        lambda: _JumpNLU({"next_node": "", "jump_module": "m1",
                          "reason": "选车", "slots": {"brand": "A"}}))
    marker_nlg_code = register_stage_stub(_MarkerNLG)

    menu = BaseNode(node_code="menu_a", node_name="菜单A")
    root = BaseNode(node_code="root", node_name="根", sub_nodes=["menu_a"],
                    stages={"nlu": jump_nlu_code, "nlg": marker_nlg_code})
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        module_nodes=[root, menu])

    ran = []

    class _TargetNLG(PipelineStage):
        stage_name = "target_nlg"

        async def execute(self, ctx):
            ran.append("target_nlg")
            ctx.nlg_result = {"content": "已为您切换到目标模块"}
            return ctx

    class _FSMNLUStub(PipelineStage):
        stage_name = "fsm_nlu"

        async def execute(self, ctx):
            ran.append("fsm_nlu")
            ctx.nlu_result = {"next_node": "", "slots": {}}
            return ctx

    target_nlg_code = register_stage_stub(_TargetNLG)
    fsm_nlu_code = register_stage_stub(_FSMNLUStub)

    target = FSMModule(
        module_code="m1", module_name="m", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="f1", node_name="F1",
                               stages={"nlg": target_nlg_code})],
        stages={"nlu": fsm_nlu_code, "nlg": target_nlg_code})

    pattern = Pattern(code="pj1", name="t", description="t",
                      entry_module_code="r1", modules=[route, target])
    sessions = {}
    _launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        reply = _chat(sessions, "s1", "我要买车")

    assert reply == "已为您切换到目标模块"
    assert "marker_nlg" not in ran
    assert ran == ["fsm_nlu", "target_nlg"]
    assert sessions["s1"].cxt.current_module_code == "m1"
    # Slots are merged along with the jump (the target module takes over the context)
    assert sessions["s1"].cxt.filled_slots.get("brand") == "A"


def test_jump_event_via_actions_snapshot_when_hops_exhausted():
    """Over the hop limit: the second hop's jump event is consumed onto the last
    target, then force_close wraps up.

    FSM produces no events (detection is ROUTE-only); the loop is created by
    the ROUTE module repeatedly outputting jump_module: r1 ->(route_menu) m1
    (consumed by hop1) -> the next turn re-enters r1 ->(nlu_jump) m1 (consumed
    by hop2) -> over the limit, force_close.
    """
    from nexus.engine.chat import chat_turn
    from stage_stubs import register_stage_stub

    # The root NLU jumps to m1 the first time; on re-entry to r1 it jumps
    # straight via jump_module (creating the loop)
    class _LoopRouteNLU(PipelineStage):
        stage_name = "loop_route_nlu"

        def __init__(self):
            self.calls = 0

        async def execute(self, ctx):
            self.calls += 1
            # First time via the menu node (route_menu), afterwards directly
            # via nlu_jump (covers both sources)
            if self.calls == 1:
                ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            else:
                ctx.nlu_result = {"next_node": "", "jump_module": "m1",
                                  "slots": {}}
            return ctx

    loop_nlu_code = register_stage_stub(_LoopRouteNLU)
    marker_nlg_code = register_stage_stub(_MarkerNLG)

    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    jump_module="m1")
    root = BaseNode(node_code="root", node_name="根", sub_nodes=["menu_a"],
                    stages={"nlu": loop_nlu_code, "nlg": marker_nlg_code})
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        module_nodes=[root, menu])

    ran = []

    class _TargetNLG(PipelineStage):
        stage_name = "target_nlg"

        async def execute(self, ctx):
            ran.append("target_nlg")
            ctx.nlg_result = {"content": "target 回复"}
            return ctx

    class _TargetNLU(PipelineStage):
        stage_name = "target_nlu"

        async def execute(self, ctx):
            ctx.nlu_result = {"next_node": "", "slots": {}}
            return ctx

    target_nlg_code = register_stage_stub(_TargetNLG)
    target_nlu_code = register_stage_stub(_TargetNLU)

    target = FSMModule(
        module_code="m1", module_name="m", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="f1", node_name="F1")],
        stages={"nlu": target_nlu_code, "nlg": target_nlg_code})

    pattern = Pattern(code="pj2", name="t", description="t",
                      entry_module_code="r1", modules=[route, target],
                      max_hops=2)
    sessions = {}
    _launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        from async_utils import arun
        result = arun(chat_turn("选A", "s1", sessions))

    # force_close lands on the last target m1 (FSM turns do not detect jumps;
    # stages run to completion and produce the reply)
    assert result.text == "target 回复"
    assert sessions["s1"].cxt.current_module_code == "m1"
    # The event channel is empty (after the over-limit pending event is
    # consumed, force_close produces no further events)
    assert not [a for a in result.actions if "module_jump" in a]
