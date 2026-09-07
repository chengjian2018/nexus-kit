"""Pipeline slots (pre_recall/query/post_recall/generate) and three-layer lazy-resolution tests.

Core contracts (direct counterpart of the stage_slots.py design as the single source of truth):
- Three-layer priority node > module > pattern; generate dual form (single / dict with exactly nlu+nlg)
- Validation failure degrades the whole layer (uniform across slots); all three layers empty →
  recall/rewrite no-op, generate builtin
- generate expands into lazy sub-parts: nlu/nlg each independently resolve three layers at their
  execution moment (under ROUTE, nlg resolves at the menu node — the core timing-fix assertion)
- Calling execute on a slot directly must fail fast
"""

import pytest

from nexus.context import DialogueContext
from nexus.model.module import FSMModule, ModuleType, RouteModule
from nexus.model.node import BaseNode
from nexus.pipeline import (
    GenerateSlot,
    PostRecallSlot,
    PreRecallSlot,
    QuerySlot,
    is_valid_stage,
    normalize_generate,
    resolve_stage,
)


class _Marker:
    """Duck-typed marker stage: records the (node, name) at execution time."""

    def __init__(self, name):
        self.stage_name = name

    def execute(self, ctx):
        ran.append((ctx.current_node_code, self.stage_name))
        return ctx


ran = []  # Execution log shared by the whole _Marker class (cleared around each case)


@pytest.fixture(autouse=True)
def _clean_ran():
    ran.clear()
    yield
    ran.clear()


def _fsm_module(generate=None, query=None, pre_recall=None, post_recall=None,
                enable_clarify=False, clarify_stage=None):
    return FSMModule(
        module_code="m1", module_name="m1", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="n1", node_name="节点一")],
        generate=generate, query=query, pre_recall=pre_recall,
        post_recall=post_recall, enable_clarify=enable_clarify,
        clarify_stage=clarify_stage,
    )


def _route_module(**kw):
    return RouteModule(
        module_code="r1", module_name="r", module_description="d",
        module_todo_description="t", sub_modules=[], **kw
    )


def _ctx(node_code="n1", node=None, module_code="m1"):
    ctx = DialogueContext(session_id="t", user_query="q")
    ctx.current_module_code = module_code
    ctx.current_node_code = node_code
    ctx.node_map = {"n1": BaseNode(node_code="n1", node_name="节点一"),
                    "n2": node or BaseNode(node_code="n2", node_name="节点二"),
                    "root": BaseNode(node_code="root", node_name="根"),
                    "menu_a": BaseNode(node_code="menu_a", node_name="菜单A")}
    ctx.module_map = {module_code: _fsm_module()}
    return ctx


def _pattern(generate=None, query=None):
    from nexus.model.pattern import Pattern
    return Pattern(code="p1", name="t", description="t",
                   entry_module_code="m1",
                   modules=[_fsm_module()],
                   generate=generate, query=query)


# ============================================================================
# is_valid_stage / normalize_generate
# ============================================================================

def test_is_valid_stage():
    assert is_valid_stage(_Marker("x")) is True
    assert is_valid_stage("not a stage") is False
    assert is_valid_stage(None) is False
    class _Broken:
        execute = "not callable"
    assert is_valid_stage(_Broken()) is False


def test_normalize_generate_forms():
    nlu, nlg, single = _Marker("nlu"), _Marker("nlg"), _Marker("single")
    assert normalize_generate({"nlu": nlu, "nlg": nlg}) == ("dict", nlu, nlg)
    assert normalize_generate(single) == ("single", single, None)
    # Invalid: missing key / extra key / invalid value / wrong type
    assert normalize_generate({"nlu": nlu}) is None
    assert normalize_generate({"nlg": nlg}) is None
    assert normalize_generate({"nlu": nlu, "nlg": nlg, "extra": 1}) is None
    assert normalize_generate({"nlu": nlu, "nlg": "bad"}) is None
    assert normalize_generate("bad") is None
    assert normalize_generate(None) is None


# ============================================================================
# Recall/rewrite slots: three-layer resolution + degrade + no-op
# ============================================================================

def test_query_slot_three_layers_and_noop():
    ctx = _ctx()
    assert resolve_stage(QuerySlot(), ctx, _fsm_module(), None) == []
    out = resolve_stage(QuerySlot(), ctx,
                        _fsm_module(query=_Marker("mod_query")), None)
    assert [s.stage_name for s in out] == ["mod_query"]


def test_query_slot_node_over_module_and_lazy_resolution():
    n2 = BaseNode(node_code="n2", node_name="节点二", query=_Marker("n2_query"))
    module = _fsm_module(query=_Marker("mod_query"))
    ctx = _ctx(node_code="n1", node=n2)

    out = resolve_stage(QuerySlot(), ctx, module, None)
    out[0].execute(ctx)
    assert ran == [("n1", "mod_query")]

    ctx.current_node_code = "n2"  # after switching nodes, resolution re-runs → node layer hits
    out = resolve_stage(QuerySlot(), ctx, module, None)
    out[0].execute(ctx)
    assert ran[-1] == ("n2", "n2_query")


def test_query_slot_invalid_layers_degrade_to_noop():
    """node/module layers all invalid → warning + no-op (no exception raised)."""
    ctx = _ctx()
    ctx.node_map["n1"] = BaseNode(node_code="n1", node_name="节点一",
                                  query="bad")
    module = _fsm_module(query=42)
    assert resolve_stage(QuerySlot(), ctx, module, None) == []


def test_recall_slots_share_same_semantics():
    ctx = _ctx()
    module = _fsm_module(pre_recall=_Marker("pre"), post_recall=None)
    out = resolve_stage(PreRecallSlot(), ctx, module, None)
    assert [s.stage_name for s in out] == ["pre"]
    assert resolve_stage(PostRecallSlot(), ctx, module, None) == []


# ============================================================================
# GenerateSlot: structural expansion + per-part three-layer resolution + degrade + builtin
# ============================================================================

def test_generate_expansion_shapes_by_stage_name():
    """Expansion shapes: FSM default / FSM+clarify / ROUTE."""
    class _Clarify:
        stage_name = "my_clarify"
        def execute(self, ctx):
            return ctx

    fsm = _fsm_module()
    names = [s.stage_name for s in resolve_stage(GenerateSlot(), _ctx(), fsm, None)]
    assert names == ["generate_nlu_part", "generate_nlg_part"]

    cl = _fsm_module(enable_clarify=True, clarify_stage=_Clarify())
    names = [s.stage_name for s in resolve_stage(GenerateSlot(), _ctx(), cl, None)]
    assert names == ["generate_nlu_part", "my_clarify", "generate_nlg_part"]

    route = _route_module()
    ctx = _ctx(module_code="r1")
    names = [s.stage_name for s in resolve_stage(GenerateSlot(), ctx, route, None)]
    # ROUTE and FSM default to the same shape: menu-node advance/jump detection is done by the
    # chat layer after the nlu part
    assert names == ["generate_nlu_part", "generate_nlg_part"]


def test_generate_parts_execute_dict_from_node_layer():
    """dict form: the nlu/nlg parts each execute the node-layer config."""
    gen = {"nlu": _Marker("node_nlu"), "nlg": _Marker("node_nlg")}
    ctx = _ctx()
    ctx.node_map["n1"] = BaseNode(node_code="n1", node_name="节点一",
                                  generate=gen)
    module = _fsm_module(generate={"nlu": _Marker("m"), "nlg": _Marker("m")})

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("n1", "node_nlu"), ("n1", "node_nlg")]


def test_generate_parts_single_stage_runs_once():
    """single form: the nlu part executes the stage, the nlg part is a no-op."""
    ctx = _ctx()
    module = _fsm_module(generate=_Marker("unified"))

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("n1", "unified")]


def test_generate_invalid_node_layer_degrades_to_module():
    """node-layer dict missing nlg (invalid) → the whole layer degrades to the module layer."""
    ctx = _ctx()
    ctx.node_map["n1"] = BaseNode(node_code="n1", node_name="节点一",
                                  generate={"nlu": _Marker("broken")})
    module = _fsm_module(generate=_Marker("mod_unified"))

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("n1", "mod_unified")]


def test_generate_all_layers_empty_falls_to_builtin():
    from atoms.stages.nlu import FSMNLU
    from atoms.stages.nlg import FSMNLG
    # builtin real stages would call the LLM — here we only verify class assembly, no stubbed execution:
    names = [type(p).__name__ for p in
             resolve_stage(GenerateSlot(), _ctx(module_code="r1"),
                           _route_module(), None)]
    assert names == ["_GenerateNLUPart", "_GenerateNLGPart"]
    # FSM builtin smoke: the nlu part resolves FSMNLU (monkeypatch its execute to avoid the LLM)
    orig = FSMNLU.execute
    FSMNLU.execute = lambda self, ctx: ran.append(("builtin", "fsm_nlu")) or ctx
    orig_nlg = FSMNLG.execute
    FSMNLG.execute = lambda self, ctx: ran.append(("builtin", "fsm_nlg")) or ctx
    try:
        ctx = _ctx()
        for part in resolve_stage(GenerateSlot(), ctx, _fsm_module(), None):
            part.execute(ctx)
    finally:
        FSMNLU.execute = orig
        FSMNLG.execute = orig_nlg
    assert ran == [("builtin", "fsm_nlu"), ("builtin", "fsm_nlg")]


def test_generate_builtin_route_executes_route_stages():
    """ROUTE builtin smoke: with all three layers empty, RouteNLU/RouteNLG execute (stubbed, without LLM)."""
    from atoms.stages.nlu import RouteNLU
    from atoms.stages.nlg import RouteNLG
    orig_nlu = RouteNLU.execute
    orig_nlg = RouteNLG.execute
    RouteNLU.execute = lambda self, ctx: ran.append(("builtin", "route_nlu")) or ctx
    RouteNLG.execute = lambda self, ctx: ran.append(("builtin", "route_nlg")) or ctx
    try:
        ctx = _ctx(node_code="root", module_code="r1")
        ctx.node_map["root"] = BaseNode(node_code="root", node_name="根")
        module = _route_module()
        ctx.module_map = {"r1": module}
        module.module_nodes = [ctx.node_map["root"]]
        for part in resolve_stage(GenerateSlot(), ctx, module, None):
            part.execute(ctx)
    finally:
        RouteNLU.execute = orig_nlu
        RouteNLG.execute = orig_nlg
    assert ran == [("builtin", "route_nlu"), ("builtin", "route_nlg")]


def test_generate_single_same_stage_at_root_and_menu_runs_once():
    """single-form guard (same object): root and menu layers resolve to the same single stage → executes only once."""
    unified = _Marker("shared_unified")
    ctx = _ctx(node_code="root")
    ctx.node_map["root"] = BaseNode(node_code="root", node_name="根",
                                    generate=unified)
    ctx.node_map["menu_a"] = BaseNode(node_code="menu_a", node_name="菜单A",
                                      generate=unified)
    module = _route_module()
    ctx.module_map = {"m1": module}

    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)

    assert ran == [("root", "shared_unified")]


def test_generate_single_menu_stage_skipped_until_next_turn():
    """single is always executed once by the nlu part: after the root-layer dict's nlu runs in
    the nlu part, the chat layer detects the menu switch (manually simulated here); the menu
    layer is a single → the nlg part is a no-op (the menu version takes effect next turn)."""
    ctx = _ctx(node_code="root", module_code="r1")
    ctx.node_map["root"] = BaseNode(
        node_code="root", node_name="根",
        generate={"nlu": _Marker("root_nlu"), "nlg": _Marker("root_nlg")})
    ctx.node_map["menu_a"] = BaseNode(node_code="menu_a", node_name="菜单A",
                                      generate=_Marker("menu_unified"))
    module = _route_module()
    ctx.module_map = {"r1": module}
    ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
    module.module_nodes = [ctx.node_map["root"], ctx.node_map["menu_a"]]

    parts = resolve_stage(GenerateSlot(), ctx, module, None)
    parts[0].execute(ctx)
    ctx.current_node_code = "menu_a"  # simulate the chat layer's jump detection advancing to the menu node
    parts[1].execute(ctx)

    # the root dict nlu runs in the nlu part; the menu single does not run this turn (root_nlg
    # does not either — the nlg part re-resolves to the menu-layer single → no-op)
    assert ran == [("root", "root_nlu")]
    assert ctx.current_node_code == "menu_a"
    # Next turn: the nlu part resolves at the menu node → the menu single takes effect
    ctx.nlu_result = {}
    for part in resolve_stage(GenerateSlot(), ctx, module, None):
        part.execute(ctx)
    assert ran == [("root", "root_nlu"), ("menu_a", "menu_unified")]


def test_generate_pattern_layer_used_when_node_module_unset():
    pattern = _pattern(generate=_Marker("pat_unified"))
    ctx = _ctx()
    for part in resolve_stage(GenerateSlot(), ctx, _fsm_module(), pattern):
        part.execute(ctx)
    assert ran == [("n1", "pat_unified")]


# ============================================================================
# ROUTE timing fix (core): the nlg part resolves after the node switch
# ============================================================================

def test_route_menu_node_nlg_resolves_after_advance():
    """ROUTE: root executes the nlu part; after the chat layer detects the menu switch (manually
    simulated here), the nlg part resolves at the menu-node layer (core timing-fix assertion)."""
    ctx = _ctx(node_code="root", module_code="r1")
    ctx.node_map["root"] = BaseNode(
        node_code="root", node_name="根",
        generate={"nlu": _Marker("root_nlu"), "nlg": _Marker("root_nlg")})
    ctx.node_map["menu_a"] = BaseNode(
        node_code="menu_a", node_name="菜单A",
        generate={"nlu": _Marker("menu_nlu"), "nlg": _Marker("menu_nlg")})
    ctx.module_map = {"r1": _route_module()}
    # Detection requires nlu_result to point at a valid menu node
    ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
    ctx.module_map["r1"].module_nodes = [
        ctx.node_map["root"], ctx.node_map["menu_a"]]

    parts = resolve_stage(GenerateSlot(), ctx, ctx.module_map["r1"], None)
    parts[0].execute(ctx)
    ctx.current_node_code = "menu_a"  # simulate the chat layer's jump detection advancing to the menu node
    parts[1].execute(ctx)

    # nlu came from the root layer; the node has switched; nlg comes from the menu_a layer (timing fix)
    assert ran == [("root", "root_nlu"), ("menu_a", "menu_nlg")]
    assert ctx.current_node_code == "menu_a"


# ============================================================================
# Non-slot passthrough + slot fail fast
# ============================================================================

def test_non_slot_stage_passthrough():
    concrete = _Marker("concrete")
    out = resolve_stage(concrete, _ctx(), _fsm_module(), None)
    assert out == [concrete]


def test_slot_direct_execute_raises():
    with pytest.raises(NotImplementedError):
        GenerateSlot().execute(_ctx())


# ============================================================================
# Data-layer attributes: node / module / pattern, three layers, four slots
# ============================================================================

def test_data_layer_slot_attributes():
    from nexus.model.pattern import Pattern

    gen = {"nlu": _Marker("nlu"), "nlg": _Marker("nlg")}
    node = BaseNode(node_code="n1", generate=gen, query=_Marker("q"))
    module = _fsm_module(generate=gen, pre_recall=_Marker("pre"))
    pattern = Pattern(code="p1", name="t", description="t",
                      entry_module_code="m1",
                      modules=[_fsm_module()], generate=_Marker("pat_gen"),
                      post_recall=_Marker("pat_post"))

    assert node.generate is gen
    assert node.query.stage_name == "q"
    assert module.generate is gen
    assert module.pre_recall.stage_name == "pre"
    assert pattern.generate.stage_name == "pat_gen"
    assert pattern.post_recall.stage_name == "pat_post"


# ============================================================================
# e2e: default skeleton and pattern.stages skeleton via chat()
# ============================================================================

from unittest.mock import patch

from nexus.engine.session import Session
from nexus.model.pattern import Pattern


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
    session.cxt.module_map = pattern.module_map
    session.cxt.node_map = pattern.node_map
    session.cxt.metadata["llm_override"] = {"code": "x", "model": "m"}
    sessions[sid] = session
    return session


def _chat(sessions, sid, query):
    from nexus.engine.chat import chat as chat_fn
    return chat_fn(query=query, session_id=sid, all_sessions=sessions)


def test_fsm_node_level_generate_via_default_skeleton():
    """Node-level generate dict takes effect under the default skeleton (FSM)."""
    n1 = BaseNode(node_code="f1", node_name="节点一",
                  generate={"nlu": _Marker("f1_nlu"), "nlg": _Marker("f1_nlg")})
    m = FSMModule(module_code="m1", module_name="m1", module_description="d",
                  module_todo_description="t", sub_modules=[], module_nodes=[n1])
    pattern = Pattern(code="pf", name="t", description="t",
                      entry_module_code="m1", modules=[m])
    sessions = {}
    _launch(pattern, sessions)
    with patch("nexus.engine.loop.build_provider"):
        _chat(sessions, "s1", "你好")

    assert ran == [("f1", "f1_nlu"), ("f1", "f1_nlg")]


def test_route_menu_node_generate_nlg_same_turn_e2e():
    """ROUTE e2e: the menu-node-level nlg takes effect in the same turn after jump detection
    switches the node (timing fix).

    The menu has no jump_module → detection only advances the node without jumping modules;
    the nlg part resolves at the menu layer.
    """
    class _SelectingNLU(_Marker):
        """Root-layer nlu: records execution and writes an nlu_result pointing at the menu node
        (the chat layer advances nodes by next_node; isomorphic to test_llm_refresh._StubNLU)."""

        def execute(self, ctx):
            super().execute(ctx)
            ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            return ctx

    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    generate={"nlu": _Marker("menu_nlu"),
                              "nlg": _Marker("menu_nlg")})
    root = BaseNode(node_code="root", node_name="根",
                    generate={"nlu": _SelectingNLU("root_nlu"),
                              "nlg": _Marker("root_nlg")},
                    sub_nodes=["menu_a"])
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        sub_modules=[], module_nodes=[root, menu])
    pattern = Pattern(code="pr", name="t", description="t",
                      entry_module_code="r1", modules=[route])
    sessions = {}
    _launch(pattern, sessions)
    with patch("nexus.engine.loop.build_provider"):
        _chat(sessions, "s1", "选A")

    # root turn: nlu uses the root layer; after detection switches to menu_a, nlg uses the menu
    # layer (the timing-fix point)
    assert ran == [("root", "root_nlu"), ("menu_a", "menu_nlg")]
    # No jump_module → no module jump; reset back to root at end of turn
    assert sessions["s1"].cxt.current_module_code == "r1"


def test_route_menu_jump_module_silent_dispatch_e2e():
    """ROUTE e2e: the menu node configures jump_module → detection interrupts the remaining
    stages (source module goes silent, nlg does not execute); the chat-layer hop consumes the
    jump and the target module continues in the same turn."""
    class _SelectingNLU(_Marker):
        def execute(self, ctx):
            super().execute(ctx)
            ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            return ctx

    menu = BaseNode(node_code="menu_a", node_name="菜单A", jump_module="m1",
                    generate={"nlu": _Marker("menu_nlu"),
                              "nlg": _Marker("menu_nlg")})
    root = BaseNode(node_code="root", node_name="根",
                    generate={"nlu": _SelectingNLU("root_nlu"),
                              "nlg": _Marker("root_nlg")},
                    sub_nodes=["menu_a"])
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        sub_modules=["m1"], module_nodes=[root, menu])
    f1 = BaseNode(node_code="f1", node_name="节点一",
                  generate={"nlu": _Marker("f1_nlu"), "nlg": _Marker("f1_nlg")})
    fsm = FSMModule(module_code="m1", module_name="m1",
                    module_description="d", module_todo_description="t",
                    sub_modules=[], module_nodes=[f1])
    pattern = Pattern(code="pr", name="t", description="t",
                      entry_module_code="r1", modules=[route, fsm])
    sessions = {}
    _launch(pattern, sessions)
    with patch("nexus.engine.loop.build_provider"):
        reply = _chat(sessions, "s1", "选A")

    # After root nlu, menu_a.jump_module=m1 is detected → interrupt (root_nlg/menu_nlg do not run)
    # m1 continues in the same turn: f1's nlu/nlg
    assert ran == [("root", "root_nlu"), ("f1", "f1_nlu"), ("f1", "f1_nlg")]
    assert sessions["s1"].cxt.current_module_code == "m1"


def test_pattern_stages_verbatim_and_mixed_slots():
    """Concrete pattern.stages stages run verbatim; GenerateSlot still resolves three layers (node-level hit)."""
    class _Fixed:
        def __init__(self, name):
            self.stage_name = name

        def execute(self, ctx):
            ran.append((ctx.current_node_code, self.stage_name))
            if self.stage_name == "pre":
                ctx.nlu_result = {"next_node": "", "slots": {}}
            return ctx

    n1 = BaseNode(node_code="f1", node_name="节点一",
                  generate=_Marker("node_gen"))
    m = FSMModule(module_code="m1", module_name="m1", module_description="d",
                  module_todo_description="t", sub_modules=[], module_nodes=[n1])
    pattern = Pattern(code="pm", name="t", description="t",
                      entry_module_code="m1", modules=[m])
    pattern.stages = [_Fixed("pre"), GenerateSlot()]
    sessions = {}
    _launch(pattern, sessions)
    with patch("nexus.engine.loop.build_provider"):
        reply = _chat(sessions, "s1", "你好")

    assert ran == [("f1", "pre"), ("f1", "node_gen")]
