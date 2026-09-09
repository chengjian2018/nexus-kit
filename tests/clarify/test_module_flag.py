"""Tests for the clarify slot declaration (the plan-② replacement of the
enable_clarify flag) + the clarify-intent directive in the FSM NLU prompt."""


def test_default_off():
    """No stages declaration → no clarify slot (opt-in by declaration)."""
    from nexus.model.module import FSMModule

    m = FSMModule(module_code="m1")
    assert "clarify" not in (m.stages or {})


def test_declared_via_stages():
    from nexus.model.module import FSMModule

    m = FSMModule(module_code="m1",
                  stages={"clarify": "clarify_default"})
    assert m.stages["clarify"] == "clarify_default"


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
