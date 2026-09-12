"""Tests for the clarify slot declaration (the plan-② replacement of the
enable_clarify flag; plan-⑧ moved the declaration from the module layer to
node.stages) + the clarify-intent directive in the FSM NLU prompt."""


def test_default_off():
    """No stages declaration → no clarify slot (opt-in by declaration)."""
    from nexus.model.node import BaseNode

    n = BaseNode(code="n1")
    assert "clarify" not in (n.stages or {})


def test_declared_via_node_stages():
    from nexus.model.node import BaseNode

    n = BaseNode(code="n1", stages={"clarify": "clarify_default"})
    assert n.stages["clarify"] == "clarify_default"


def test_declared_within_fsm_pattern_skeleton():
    """The pattern skeleton must carry the clarify slot for the node-level
    declaration to run (two-layer resolution: node > skeleton)."""
    from nexus.model.node import BaseNode
    from nexus.model.pattern import Pattern

    n = BaseNode(code="n1", stages={"clarify": "clarify_default"})
    p = Pattern(code="pf", name="t", description="t", pattern_type="fsm",
                nodes=[n], stages=[{"nlu": None}, {"clarify": None},
                                   {"nlg": None}])
    assert [list(e.keys())[0] for e in p.stages] == ["nlu", "clarify", "nlg"]


def test_builtin_code_registered():
    """The builtin clarify code resolves to the default ClarifyStage assembly."""
    import atoms.stages  # noqa: F401
    from nexus.registry.plugins import registry as plugin_registry
    from atoms.stages.clarify import ClarifyStage

    assert plugin_registry.has("stage", "clarify_default")
    assert isinstance(plugin_registry.resolve("stage", "clarify_default"),
                      ClarifyStage)


def test_fsm_nlu_prompt_contains_clarify_protocol():
    from atoms.stages._prompts import FSM_NLU_DEFAULT_PROMPT

    assert '"clarify"' in FSM_NLU_DEFAULT_PROMPT
    assert "topic" in FSM_NLU_DEFAULT_PROMPT
    assert "keywords" in FSM_NLU_DEFAULT_PROMPT
