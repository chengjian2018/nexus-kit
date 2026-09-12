"""Pipeline skeleton + two-layer resolution tests (plan-⑧ declarative
stages; the module layer is gone).

Core contracts (direct counterpart of the pipeline.py design):
- Skeleton: ordered list of single-key dicts ({slot: code-or-None}); the
  empty/None declaration normalizes to the kernel default six-slot skeleton
- Per-slot resolution: node.stages > pattern skeleton value > builtin
  default; None after all layers → slot skipped
- Unified dedup: nlu/nlg sharing one code executes once; any other
  duplicate code keeps only the first occurrence
- Malformed skeleton declarations fail fast at construction (FSM Pattern)
"""

from async_utils import arun
import pytest

from nexus.context import DialogueContext
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


def _node(code="n1", name="节点一", **kw):
    return BaseNode(code=code, name=name, **kw)


def _fsm_pattern(stages=None, nodes=None) -> Pattern:
    return Pattern(code="p1", name="t", description="t",
                   pattern_type="fsm",
                   nodes=nodes if nodes is not None else [_node()],
                   stages=stages)


def _ctx(node_code="n1"):
    ctx = DialogueContext(session_id="t", user_query="q")
    ctx.current_node_code = node_code
    return ctx


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
        _fsm_pattern(stages=[{"nlu": 42}])


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
# Slot resolution: two layers + skip + builtin tail
# ============================================================================

def test_optional_slot_none_is_skipped():
    pattern = _fsm_pattern(stages=[{"query": None}])  # explicitly None
    sequence = resolve_execution_sequence(_ctx(), pattern.nodes[0], pattern)
    assert [slot for slot, _ in sequence] == []


def test_skeleton_value_resolves_without_node_declaration():
    q_code = _marker_code("pat_query")
    pattern = _fsm_pattern(stages=[{"query": q_code}])
    sequence = resolve_execution_sequence(_ctx(), pattern.nodes[0], pattern)
    assert [slot for slot, _ in sequence] == ["query"]
    arun(sequence[0][1].execute(_ctx()))
    assert ran == [("n1", "pat_query")]


def test_node_layer_wins_over_skeleton():
    n1_q = _marker_code("n1_query")
    skeleton_q = _marker_code("skeleton_query")
    n1 = _node(code="n1", stages={"query": n1_q})
    n2 = _node(code="n2", stages={"query": n1_q})
    pattern = _fsm_pattern(stages=[{"query": skeleton_q}],
                           nodes=[n1, n2])
    ctx = _ctx(node_code="n1")
    # n1 declares stages: the node layer wins over the skeleton value
    sequence = resolve_execution_sequence(ctx, n1, pattern)
    arun(sequence[0][1].execute(ctx))
    assert ran == [("n1", "n1_query")]


def test_node_without_declaration_falls_to_skeleton_value():
    skeleton_q = _marker_code("skeleton_query")
    n1 = _node(code="n1", stages={"query": _marker_code("n1_query")})
    n2 = _node(code="n2")  # no stages declaration
    pattern = _fsm_pattern(stages=[{"query": skeleton_q}], nodes=[n1, n2])
    ctx = _ctx(node_code="n2")
    sequence = resolve_execution_sequence(ctx, n2, pattern)
    arun(sequence[0][1].execute(ctx))
    assert ran == [("n2", "skeleton_query")]


def test_builtin_generate_tail_when_unresolved():
    """nlu/nlg unresolved after the declarative layers → builtin factories
    (atoms.stages warmed; resolved lazily — verify by executing)."""
    import atoms.stages  # noqa: F401
    pattern = _fsm_pattern(stages=[{"nlu": None}, {"nlg": None}])
    ctx = _ctx()
    sequence = resolve_execution_sequence(ctx, pattern.nodes[0], pattern)

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


def test_builtin_generate_registered_for_fsm_only():
    """builtin generate is registered for the fsm pattern type only (the
    route family is gone); an agent-pattern stages declaration raises at
    construction, so the tail never applies there."""
    from nexus.pipeline import builtin_generate_default
    assert builtin_generate_default("fsm") is not None
    assert builtin_generate_default("route") is None
    with pytest.raises(ValueError, match="stages"):
        Pattern(code="pa", name="t", description="t", pattern_type="agent",
                nodes=[_node()], stages=[{"nlu": None}])


def test_clarify_slot_declared_runs_between_nlu_and_nlg():
    import atoms.stages  # noqa: F401
    cl_code = _marker_code("my_clarify")
    pattern = _fsm_pattern(
        stages=[{"nlu": None}, {"clarify": None}, {"nlg": None}])
    # clarify declared at the NODE layer (the plan-⑧ replacement of the
    # module-level enable_clarify)
    pattern.nodes[0].stages = {"clarify": cl_code}

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
        ctx = _ctx()
        sequence = resolve_execution_sequence(ctx, pattern.nodes[0], pattern)
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
    n1 = _node(stages={"nlu": u_code, "nlg": u_code})
    pattern = _fsm_pattern(nodes=[n1])
    sequence = resolve_execution_sequence(_ctx(), n1, pattern)
    slots = [slot for slot, _ in sequence]
    assert slots == ["nlu"]  # the nlg entry dropped: unified single execution
    for _, stage in sequence:
        arun(stage.execute(_ctx()))
    assert ran == [("n1", "unified")]


def test_unified_pair_via_skeleton():
    u_code = _marker_code("unified")
    pattern = _fsm_pattern(stages=[{"nlu": u_code}, {"nlg": u_code}])
    sequence = resolve_execution_sequence(_ctx(), pattern.nodes[0], pattern)
    assert [slot for slot, _ in sequence] == ["nlu"]
    for _, stage in sequence:
        arun(stage.execute(_ctx()))
    assert ran == [("n1", "unified")]


def test_duplicate_code_across_other_slots_keeps_first():
    q_code = _marker_code("shared")
    nlu_code = q_code  # same code on query and nlu: not the unified pair
    n1 = _node(stages={"query": q_code, "nlu": nlu_code,
                       "nlg": _marker_code("nlg")})
    pattern = _fsm_pattern(nodes=[n1])
    sequence = resolve_execution_sequence(_ctx(), n1, pattern)
    slots = [slot for slot, _ in sequence]
    assert "nlu" not in slots  # duplicate dropped (declaration error caught by validation)
    assert "query" in slots


def test_unregistered_code_skips_slot():
    n1 = _node(stages={"query": "no_such_stage"})
    pattern = _fsm_pattern(stages=[{"query": None}], nodes=[n1])
    sequence = resolve_execution_sequence(_ctx(), n1, pattern)
    assert [slot for slot, _ in sequence] == []  # unregistered → skipped


# ============================================================================
# e2e: skeleton via chat()
# ============================================================================

from unittest.mock import patch  # noqa: E402

from nexus.engine.session import Session  # noqa: E402


def _launch(pattern, sessions, sid="s1"):
    session = Session(session_id=sid, pattern_code=pattern.code)
    session.pattern = pattern
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
    n1 = _node(code="f1", name="节点一",
               stages={"nlu": f1_nlu, "nlg": f1_nlg})
    pattern = _fsm_pattern(nodes=[n1])  # default six-slot skeleton
    sessions = {}
    _launch(pattern, sessions)
    with patch("nexus.engine.chat.get_llm_config",
               return_value={"code": "x", "model": "m"}):
        _chat(sessions, "s1", "你好")

    assert ran == [("f1", "f1_nlu"), ("f1", "f1_nlg")]


def test_pattern_skeleton_subset_runs_verbatim():
    """A pattern declaring only [query, nlu] skips the recall/clarify/nlg
    slots entirely (skeleton shape is the author's choice — nlg absent from
    the skeleton gets no builtin tail; only skeleton-declared slots do)."""
    q_code = _marker_code("q")
    nlu_code = _marker_code("nlu")
    pattern = _fsm_pattern(stages=[{"query": q_code}, {"nlu": nlu_code}])
    sequence = resolve_execution_sequence(_ctx(), pattern.nodes[0], pattern)
    assert [slot for slot, _ in sequence] == ["query", "nlu"]
