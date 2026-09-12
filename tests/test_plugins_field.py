"""Tests for the plugins declaration field (nexus/model/plugins_field.py,
plan-⑧ slot table).

- normalize_plugins: unknown slot / illegal value fail fast; values are
  str/None only (the callable transition window is closed)
- slot table: loop / fsm / messages_builder / agent_hooks / llm
- Pattern sugar: the agent_hooks param folds into the plugins dict (dict
  value wins); node plugins carry the same normalization
- label helper: executor family slots report executor_<family>
"""

import pytest

from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern
from nexus.model.plugins_field import (
    PLUGIN_KINDS,
    normalize_plugins,
    plugins_slot_label,
)


def test_slot_table():
    assert set(PLUGIN_KINDS) == {
        "loop", "fsm", "messages_builder", "agent_hooks", "llm"}
    assert PLUGIN_KINDS["loop"] == "executor"
    assert PLUGIN_KINDS["fsm"] == "executor"
    assert PLUGIN_KINDS["messages_builder"] == "messages_builder"
    assert PLUGIN_KINDS["agent_hooks"] == "agent_hooks"
    assert PLUGIN_KINDS["llm"] == ""  # settings-resolved, no registry kind


def test_normalize_plugins_accepts_str_and_none():
    merged = normalize_plugins(
        {"loop": "default_loop", "fsm": None, "llm": "openai"})
    assert merged == {"loop": "default_loop", "fsm": None, "llm": "openai"}


def test_unknown_slot_raises():
    with pytest.raises(ValueError, match="槽位名非法"):
        normalize_plugins({"route": "default_route"})


def test_callable_value_raises():
    # the transitional callable window is closed (plan-⑧)
    with pytest.raises(ValueError, match="str/None"):
        normalize_plugins({"messages_builder": lambda n, c, e: []})


def test_legacy_fills_missing_dict_wins():
    merged = normalize_plugins(
        {"agent_hooks": "pkg_b"},
        legacy={"agent_hooks": "pkg_a", "messages_builder": "mb_x"},
    )
    assert merged == {"agent_hooks": "pkg_b", "messages_builder": "mb_x"}


def test_legacy_none_skipped():
    merged = normalize_plugins({}, legacy={"agent_hooks": None})
    assert merged == {}


def test_pattern_agent_hooks_sugar_folds_in():
    p = Pattern(code="ph", name="n", description="d", agent_hooks="pkg_a")
    assert p.plugins["agent_hooks"] == "pkg_a"
    p2 = Pattern(code="ph2", name="n", description="d",
                 plugins={"agent_hooks": "pkg_b"}, agent_hooks="pkg_a")
    assert p2.plugins["agent_hooks"] == "pkg_b"  # dict 值胜出


def test_node_plugins_normalized():
    node = BaseNode(code="x", plugins={"loop": "my_exec"})
    assert node.plugins == {"loop": "my_exec"}
    with pytest.raises(ValueError, match="槽位名非法"):
        BaseNode(code="x", plugins={"bad_slot": "y"})
    with pytest.raises(ValueError, match="str/None"):
        BaseNode(code="x", plugins={"llm": {"model": "m"}})


def test_slot_label():
    assert plugins_slot_label("loop") == "executor_loop"
    assert plugins_slot_label("fsm") == "executor_fsm"
    assert plugins_slot_label("llm") == "llm"
