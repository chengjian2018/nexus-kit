"""Structural tests for the three clarify prompt templates."""

from atoms.stages.clarify.prompts import (
    CLARIFY_FALLBACK_PROMPT,
    CLARIFY_KB_PROMPT,
    CLARIFY_MIXED_PROMPT,
    CLARIFY_PROMPTS,
)

SLOTS = ["{__query__}", "{__topic__}", "{__keywords__}", "{__recall_info__}",
         "{__cur_node__}", "{__history__}", "{__task_info__}"]


def test_kb_prompt_has_all_slots_and_kb_style():
    for slot in SLOTS:
        assert slot in CLARIFY_KB_PROMPT, f"KB 模板缺槽位 {slot}"
    assert "知识库" in CLARIFY_KB_PROMPT


def test_fallback_prompt_has_all_slots_and_no_recall_dependency():
    for slot in SLOTS:
        assert slot in CLARIFY_FALLBACK_PROMPT, f"FALLBACK 模板缺槽位 {slot}"
    # The fallback track must not depend on recall content to answer
    # (it must still generate when recall is empty)
    assert "知识库召回内容" in CLARIFY_FALLBACK_PROMPT


def test_mixed_prompt_has_all_slots():
    for slot in SLOTS:
        assert slot in CLARIFY_MIXED_PROMPT, f"MIXED 模板缺槽位 {slot}"


def test_registry_maps_all_modes():
    assert set(CLARIFY_PROMPTS) == {"kb", "fallback", "mixed"}
    assert CLARIFY_PROMPTS["kb"] == CLARIFY_KB_PROMPT
    assert CLARIFY_PROMPTS["fallback"] == CLARIFY_FALLBACK_PROMPT
    assert CLARIFY_PROMPTS["mixed"] == CLARIFY_MIXED_PROMPT
