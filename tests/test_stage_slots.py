"""Pipeline skeleton + three-layer resolution tests (plan-② declarative
stages).

Core contracts (direct counterpart of the pipeline.py design):
- Skeleton: ordered list of single-key dicts ({slot: code-or-None}); the
  empty/None declaration normalizes to the kernel default six-slot skeleton
- Per-slot resolution: node.stages > module.stages > skeleton value >
  builtin default; None after all layers → slot skipped
- Unified dedup: nlu/nlg sharing one code executes once; any other
  duplicate code keeps only the first occurrence
- Malformed skeleton declarations fail fast at construction
"""

from async_utils import arun
import pytest

from nexus.context import DialogueContext
from nexus.model.module import FSMModule, ModuleType, RouteModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.pipeline import (
    DEFAULT_SKELETON_SLOTS,
    default_skeleton,
    is_valid_stage,
    normalize_skeleton,
    resolve_execution_sequence,
)


class _Marker:
    """Marker stage: records the (node, name) at execution time."""

    def __init__(self, name):
        self.stage_name = name

    async def execute(self, ctx):
        ran.append((ctx.current_node_code, self.stage_name))
        return ctx


ran = []  # shared execution log (cleared around each case)


@pytest.fixture(autouse=True)
def _clean_ran():
    ran.clear()
    yield
    ran.clear()


from stage_stubs import register_stage_stub  # noqa: E402


def _marker_code(name, prefix="mk"):
    """Register a _Marker stage under a unique code; returns the code."""
    return register_stage_stub(lambda name=name: _Marker(name),
                               prefix=prefix)


def _fsm_module(stages=None):
    return FSMModule(
        module_code="m1", module_name="m1", module_description="d",
        module_todo_description="t", sub_modules=[],
        module_nodes=[BaseNode(node_code="n1", node_name="节点一")],
        stages=stages,
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


def _pattern(stages=None):
    return Pattern(code="p1", name="t", description="t",
                   entry_module_code="m1",
                   modules=[_fsm_module()],
                   stages=stages)


# ============================================================================
# Skeleton normalization
# ============================================================================

def test_default_skeleton_six_slots():
    skeleton = default_skeleton()
    assert [list(e.keys())[0] for e in skeleton] == DEFAULT_SKELETON_SLOTS
    assert all(list(e.values())[0] is None for e in skeleton)


def test_normalize_skeleton_passes_valid():
    decl = [{"query": "q1"}, {"nlu": None}, {"nlg": "u"}]
    assert normalize_skeleton(decl) == decl


def test_normalize_skeleton_empty_falls_back_to_default():
    assert normalize_skeleton(None) == default_skeleton()
    assert normalize_skeleton([]) == default_skeleton()


def test_normalize_skeleton_malformed_fails_fast():
    with pytest.raises(ValueError):
        normalize_skeleton("not-a-list")
    with pytest.raises(ValueError):
        normalize_skeleton([{"nlu": "a", "nlg": "b"}])  # two keys
    with pytest.raises(ValueError):
        normalize_skeleton(["nlu"])  # not a dict
    with pytest.raises(ValueError):
        normalize_skeleton([{"nlu": 42}])  # non-str non-None code
    # construction propagates the failure
    with pytest.raises(ValueError):
        Pattern(code="px", name="t", description="t", entry_module_code="m1",
                modules=[_fsm_module()], stages=[{"nlu": 42}])


# ============================================================================
# is_valid_stage (duck typing)
# ============================================================================

def test_is_valid_stage():
    assert is_valid_stage(_Marker("x")) is True
    assert is_valid_stage("not a stage") is False
    assert is_valid_stage(None) is False
    class _Broken:
        execute = "not callable"
    assert is_valid_stage(_Broken()) is False


# ============================================================================
# Slot resolution: three layers + skip + builtin tail
# ============================================================================

def test_optional_slot_none_is_skipped():
    ctx = _ctx()
    module = _fsm_module()  # no stages declaration
    pattern = _pattern(stages=[{"query": None}])  # explicitly None
    sequence = resolve_execution_sequence(ctx, module, pattern)
    assert [slot for slot, _ in sequence] == []


def test_module_layer_wins_over_skeleton():
    q_code = _marker_code("mod_query")
    ctx = _ctx()
    module = _fsm_module(stages={"query": q_code})
    pattern = _pattern(stages=[{"query": None}])
    sequence = resolve_execution_sequence(ctx, module, pattern)
    assert [slot for slot, _ in sequence] == ["query"]
    arun(sequence[0][1].execute(ctx))
    assert ran == [("n1", "mod_query")]


def test_node_layer_wins_over_module():
    n2_q = _marker_code("n2_query")
    mod_q = _marker_code("mod_query")
    n2 = BaseNode(node_code="n2", node_name="节点二", stages={"query": n2_q})
    module = _fsm_module(stages={"query": mod_q})
    ctx = _ctx(node_code="n1", node=n2)

    sequence = resolve_execution_sequence(ctx, module, None)
    arun(sequence[0][1].execute(ctx))
    assert ran == [("n1", "mod_query")]  # n1 has no stages: module layer

    ctx.current_node_code = "n2"
    sequence = resolve_execution_sequence(ctx, module, None)
    arun(sequence[0][1].execute(ctx))
    assert ran[-1] == ("n2", "n2_query")  # node layer hits


def test_skeleton_value_is_pattern_layer():
    q_code = _marker_code("pat_query")
    ctx = _ctx()
    module = _fsm_module()
    pattern = _pattern(stages=[{"query": q_code}])
    sequence = resolve_execution_sequence(ctx, module, pattern)
    arun(sequence[0][1].execute(ctx))
    assert ran == [("n1", "pat_query")]


def test_builtin_generate_tail_when_unresolved():
    """nlu/nlg unresolved after the declarative layers → builtin factories
    (atoms.stages warmed; nlg resolves lazily — verify by executing)."""
    import atoms.stages  # noqa: F401
    ctx = _ctx()
    module = _fsm_module()
    pattern = _pattern(stages=[{"nlu": None}, {"nlg": None}])
    sequence = resolve_execution_sequence(ctx, module, pattern)

    from atoms.stages.nlu import FSMNLU
    from atoms.stages.nlg import FSMNLG
    orig_nlu, orig_nlg = FSMNLU.execute, FSMNLG.execute
    async def _nlu(self, ctx):
        ran.append(("n1", "fsm_nlu"))
        return ctx
    async def _nlg(self, ctx):
        ran.append(("n1", "fsm_nlg"))
        return ctx
    FSMNLU.execute, FSMNLG.execute = _nlu, _nlg
    try:
        for _, stage in sequence:
            arun(stage.execute(ctx))
    finally:
        FSMNLU.execute, FSMNLG.execute = orig_nlu, orig_nlg
    assert ran == [("n1", "fsm_nlu"), ("n1", "fsm_nlg")]


def test_builtin_route_generate_tail():
    import atoms.stages  # noqa: F401
    ctx = _ctx(node_code="root", module_code="r1")
    module = _route_module()
    pattern = _pattern(stages=[{"nlu": None}, {"nlg": None}])
    sequence = resolve_execution_sequence(ctx, module, pattern)

    from atoms.stages.nlu import RouteNLU
    from atoms.stages.nlg import RouteNLG
    orig_nlu, orig_nlg = RouteNLU.execute, RouteNLG.execute
    async def _nlu(self, ctx):
        ran.append(("root", "route_nlu"))
        return ctx
    async def _nlg(self, ctx):
        ran.append(("root", "route_nlg"))
        return ctx
    RouteNLU.execute, RouteNLG.execute = _nlu, _nlg
    try:
        for _, stage in sequence:
            arun(stage.execute(ctx))
    finally:
        RouteNLU.execute, RouteNLG.execute = orig_nlu, orig_nlg
    assert ran == [("root", "route_nlu"), ("root", "route_nlg")]


def test_clarify_slot_declared_runs_between_nlu_and_nlg():
    import atoms.stages  # noqa: F401
    cl_code = _marker_code("my_clarify")
    ctx = _ctx()
    module = _fsm_module(stages={"clarify": cl_code})
    pattern = _pattern(stages=[{"nlu": None}, {"clarify": None},
                               {"nlg": None}])
    # nlu/nlg fall to builtin which would call LLM — stub execute
    from atoms.stages.nlu import FSMNLU
    from atoms.stages.nlg import FSMNLG
    orig_nlu, orig_nlg = FSMNLU.execute, FSMNLG.execute
    async def _nlu(self, ctx):
        ran.append(("n1", "fsm_nlu"))
        return ctx
    async def _nlg(self, ctx):
        ran.append(("n1", "fsm_nlg"))
        return ctx
    FSMNLU.execute, FSMNLG.execute = _nlu, _nlg
    try:
        sequence = resolve_execution_sequence(ctx, module, pattern)
        for slot, stage in sequence:
            arun(stage.execute(ctx))
    finally:
        FSMNLU.execute, FSMNLG.execute = orig_nlu, orig_nlg
    assert ran == [("n1", "fsm_nlu"), ("n1", "my_clarify"), ("n1", "fsm_nlg")]


# ============================================================================
# Unified dedup
# ============================================================================

def test_unified_pair_same_code_executes_once():
    u_code = _marker_code("unified")
    ctx = _ctx()
    module = _fsm_module(stages={"nlu": u_code, "nlg": u_code})
    sequence = resolve_execution_sequence(ctx, module, None)
    slots = [slot for slot, _ in sequence]
    assert slots == ["nlu"]  # the nlg entry dropped: unified single execution
    for _, stage in sequence:
        arun(stage.execute(ctx))
    assert ran == [("n1", "unified")]


def test_unified_pair_via_skeleton():
    u_code = _marker_code("unified")
    ctx = _ctx()
    module = _fsm_module()
    pattern = _pattern(stages=[{"nlu": u_code}, {"nlg": u_code}])
    sequence = resolve_execution_sequence(ctx, module, pattern)
    assert [slot for slot, _ in sequence] == ["nlu"]
    for _, stage in sequence:
        arun(stage.execute(ctx))
    assert ran == [("n1", "unified")]


def test_duplicate_code_across_other_slots_keeps_first():
    q_code = _marker_code("shared")
    nlu_code = q_code  # same code on query and nlu: not the unified pair
    ctx = _ctx()
    module = _fsm_module(stages={"query": q_code, "nlu": nlu_code,
                                 "nlg": _marker_code("nlg")})
    pattern = _pattern()
    sequence = resolve_execution_sequence(ctx, module, pattern)
    slots = [slot for slot, _ in sequence]
    assert "nlu" not in slots  # duplicate dropped (declaration error caught by validation)
    assert "query" in slots


def test_unregistered_code_skips_slot():
    ctx = _ctx()
    module = _fsm_module(stages={"query": "no_such_stage"})
    pattern = _pattern(stages=[{"query": None}])  # only query in skeleton
    sequence = resolve_execution_sequence(ctx, module, pattern)
    assert [slot for slot, _ in sequence] == []  # unregistered → skipped


# ============================================================================
# e2e: skeleton via chat()
# ============================================================================

from unittest.mock import patch  # noqa: E402

from nexus.engine.session import Session  # noqa: E402


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
    return arun(chat_fn(query=query, session_id=sid, all_sessions=sessions))


def test_fsm_node_level_stages_via_default_skeleton():
    """Node-level stages take effect under the default skeleton (FSM)."""
    f1_nlu = _marker_code("f1_nlu")
    f1_nlg = _marker_code("f1_nlg")
    n1 = BaseNode(node_code="f1", node_name="节点一",
                  stages={"nlu": f1_nlu, "nlg": f1_nlg})
    m = FSMModule(module_code="m1", module_name="m1", module_description="d",
                  module_todo_description="t", sub_modules=[], module_nodes=[n1])
    pattern = Pattern(code="pf", name="t", description="t",
                      entry_module_code="m1", modules=[m])
    sessions = {}
    _launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        _chat(sessions, "s1", "你好")

    assert ran == [("f1", "f1_nlu"), ("f1", "f1_nlg")]


def test_route_menu_node_nlg_same_turn_e2e():
    """ROUTE e2e: the menu-node-level nlg takes effect in the same turn after
    jump detection switches the node (the ROUTE nlg resolves per current node
    in the ordered sequence — the nlg slot sits after the nlu slot)."""

    root_nlu_code = register_stage_stub(
        lambda: _SelectingNLU("root_nlu"))

    class _SelectingNLU(_Marker):
        async def execute(self, ctx):
            await super().execute(ctx)
            ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            return ctx

    menu_nlu = _marker_code("menu_nlu")
    menu_nlg = _marker_code("menu_nlg")
    root_nlg = _marker_code("root_nlg")

    menu = BaseNode(node_code="menu_a", node_name="菜单A",
                    stages={"nlu": menu_nlu, "nlg": menu_nlg})
    root = BaseNode(node_code="root", node_name="根",
                    stages={"nlu": root_nlu_code, "nlg": root_nlg},
                    sub_nodes=["menu_a"])
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        sub_modules=[], module_nodes=[root, menu])
    pattern = Pattern(code="pr", name="t", description="t",
                      entry_module_code="r1", modules=[route])
    sessions = {}
    _launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        _chat(sessions, "s1", "选A")

    # root turn: nlu from the root layer; after detection switches to menu_a,
    # nlg resolves at the menu layer (the timing-fix point: the nlg slot
    # executes after the node switch)
    assert ran == [("root", "root_nlu"), ("menu_a", "menu_nlg")]
    assert sessions["s1"].cxt.current_module_code == "r1"


def test_route_menu_jump_module_silent_dispatch_e2e():
    """ROUTE e2e: the menu node configures jump_module → detection interrupts
    the remaining stages (source module goes silent, nlg does not execute);
    the chat-layer hop consumes the jump and the target module continues in
    the same turn."""

    class _SelectingNLU(_Marker):
        async def execute(self, ctx):
            await super().execute(ctx)
            ctx.nlu_result = {"next_node": "menu_a", "slots": {}}
            return ctx

    root_nlu_code = register_stage_stub(lambda: _SelectingNLU("root_nlu"))
    menu_nlu = _marker_code("menu_nlu")
    menu_nlg = _marker_code("menu_nlg")
    root_nlg = _marker_code("root_nlg")
    f1_nlu = _marker_code("f1_nlu")
    f1_nlg = _marker_code("f1_nlg")

    menu = BaseNode(node_code="menu_a", node_name="菜单A", jump_module="m1",
                    stages={"nlu": menu_nlu, "nlg": menu_nlg})
    root = BaseNode(node_code="root", node_name="根",
                    stages={"nlu": root_nlu_code, "nlg": root_nlg},
                    sub_nodes=["menu_a"])
    route = RouteModule(module_code="r1", module_name="r",
                        module_description="d", module_todo_description="t",
                        sub_modules=["m1"], module_nodes=[root, menu])
    f1 = BaseNode(node_code="f1", node_name="节点一",
                  stages={"nlu": f1_nlu, "nlg": f1_nlg})
    fsm = FSMModule(module_code="m1", module_name="m1",
                    module_description="d", module_todo_description="t",
                    sub_modules=[], module_nodes=[f1])
    pattern = Pattern(code="pr", name="t", description="t",
                      entry_module_code="r1", modules=[route, fsm])
    sessions = {}
    _launch(pattern, sessions)
    with patch("atoms.executors.loop_executor.build_provider"):
        reply = _chat(sessions, "s1", "选A")

    assert ran == [("root", "root_nlu"), ("f1", "f1_nlu"), ("f1", "f1_nlg")]
    assert sessions["s1"].cxt.current_module_code == "m1"


def test_pattern_skeleton_subset_runs_verbatim():
    """A pattern declaring only [query, nlu] skips the recall/clarify/nlg
    slots entirely (skeleton shape is the author's choice — nlg absent from
    the skeleton gets no builtin tail; only skeleton-declared slots do)."""
    q_code = _marker_code("q")
    nlu_code = _marker_code("nlu")
    pattern = _pattern(stages=[{"query": q_code}, {"nlu": nlu_code}])
    ctx = _ctx()
    module = _fsm_module()
    sequence = resolve_execution_sequence(ctx, module, pattern)
    assert [slot for slot, _ in sequence] == ["query", "nlu"]
