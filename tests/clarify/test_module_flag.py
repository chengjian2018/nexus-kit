"""Tests for the enable_clarify module flag + the clarify-intent directive in the FSM NLU prompt."""


def test_default_disabled():
    from nexus.model.module import FSMModule

    m = FSMModule(module_code="m1")
    assert m.enable_clarify is False


def test_explicit_enabled():
    from nexus.model.module import FSMModule

    m = FSMModule(module_code="m1", enable_clarify=True)
    assert m.enable_clarify is True


def test_kwargs_style_enabled():
    """Declarative patterns pass kwargs; the flag must take effect the same way."""
    from nexus.model.module import FSMModule

    m = FSMModule(module_code="m1", **{"enable_clarify": True})
    assert m.enable_clarify is True


def test_fsm_nlu_prompt_contains_clarify_protocol():
    from atoms.stages._prompts import FSM_NLU_DEFAULT_PROMPT

    assert '"clarify"' in FSM_NLU_DEFAULT_PROMPT
    assert "topic" in FSM_NLU_DEFAULT_PROMPT
    assert "keywords" in FSM_NLU_DEFAULT_PROMPT
