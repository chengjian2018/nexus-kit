"""Agent-hooks interface contract tests (plan-⑧ node form).

The hooks machinery is retained (6 points — on_transfer went with the
defer/transfer machinery — / event classes carrying node_code / declaration
resolution / dispatcher signatures) but the default behavior is a no-op
pass-through: with no hooks package declared, every point costs nothing and
every dispatcher returns its input unchanged. These tests pin THAT contract
(the signature and the pass-through), not hook behaviors.
"""

import logging

from nexus.engine.agent_hooks import (
    HOOK_POINTS,
    AgentStartEvent,
    LLMCallEvent,
    RewriteToolCall,
    ToolCallEvent,
    ToolResultEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
    rewrite_tool_call,
    rewrite_tool_result,
)


class _Node:
    def __init__(self, agent_hooks=None):
        self.code = "n"
        self.plugins = {"agent_hooks": agent_hooks} if agent_hooks else {}


class _Pattern:
    def __init__(self, agent_hooks=None):
        self.code = "p"
        self.plugins = {"agent_hooks": agent_hooks} if agent_hooks else {}


# ---------------------------------------------------------------------------
# Point inventory
# ---------------------------------------------------------------------------

def test_hook_points_inventory():
    assert HOOK_POINTS == (
        "on_agent_start", "on_llm_call", "on_llm_response",
        "on_tool_call", "on_tool_result", "on_agent_end",
    )


# ---------------------------------------------------------------------------
# Declaration resolution: str code / legacy dict / degradation
# ---------------------------------------------------------------------------

def test_no_declaration_resolves_empty():
    assert resolve_agent_hooks(_Node(), _Pattern()) == {}


def test_node_replaces_pattern_wholesale():
    hooks_map = {"on_agent_start": [lambda e: None]}
    resolved = resolve_agent_hooks(_Node(hooks_map),
                                   _Pattern({"on_llm_call": [lambda e: None]}))
    assert set(resolved) == {"on_agent_start"}


def test_pattern_level_declares_when_node_silent():
    resolved = resolve_agent_hooks(
        _Node(), _Pattern({"on_llm_call": [lambda e: None]}))
    assert set(resolved) == {"on_llm_call"}


def test_legacy_dict_form_still_resolves():
    """Transitional: the inline {point: [hook]} dict keeps working."""
    seen = []
    resolved = resolve_agent_hooks(
        _Pattern({"on_agent_end": [lambda e: seen.append(e)]}))
    assert list(resolved) == ["on_agent_end"]
    for hook in resolved["on_agent_end"]:
        hook(object())
    assert seen


def test_str_code_resolves_via_plugin_registry():
    from nexus.registry.plugins import registry as plugin_registry

    def _package():
        return {"on_agent_start": [lambda e: None]}

    if not plugin_registry.has("agent_hooks", "contract_pkg"):
        plugin_registry.register("agent_hooks", "contract_pkg", _package)
    resolved = resolve_agent_hooks(_Pattern("contract_pkg"))
    assert set(resolved) == {"on_agent_start"}


def test_unknown_str_code_degrades_to_empty(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_agent_hooks(_Pattern("ghost_pkg")) == {}
    assert any("未注册" in r.message for r in caplog.records)


def test_unknown_point_and_non_callable_degrade(caplog):
    with caplog.at_level(logging.WARNING):
        resolved = resolve_agent_hooks(_Pattern({
            "on_ghost_point": [lambda e: None],
            "on_llm_call": ["not-callable"],
        }))
    # the only entry of on_llm_call is non-callable: the whole point drops
    assert resolved == {}
    assert any("未知点位" in r.message for r in caplog.records)
    assert any("非 callable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Dispatcher pass-through semantics (the no-op default)
# ---------------------------------------------------------------------------

def test_fire_with_empty_hooks_is_noop():
    fire({}, "on_agent_end", AgentStartEvent(session_id="s",
                                             node_code="n", cxt=None))


def test_collect_fragments_empty_returns_empty():
    assert collect_fragments({}, AgentStartEvent(
        session_id="s", node_code="n", cxt=None)) == []


def test_rewrite_tool_call_empty_hooks_returns_original():
    event = ToolCallEvent(session_id="s", node_code="n", round_idx=0,
                          tool_name="t", args={"a": 1})
    name, args, audit = rewrite_tool_call({}, event, {"t"})
    assert name == "t" and args == {"a": 1} and audit is None


def test_rewrite_tool_result_empty_hooks_returns_original():
    event = ToolResultEvent(session_id="s", node_code="n", round_idx=0,
                            tool_name="t", tool_call_id="c",
                            result="raw")
    result, audit = rewrite_tool_result({}, event)
    assert result == "raw" and audit is None


def test_fire_swallows_hook_exception():
    def _boom(_event):
        raise RuntimeError("hook blew up")

    hooks = {"on_agent_end": [_boom]}
    fire(hooks, "on_agent_end", LLMCallEvent(session_id="s",
                                             node_code="n", round_idx=0,
                                             messages=[], model="x"))
    # no raise: exception containment is part of the contract


# ---------------------------------------------------------------------------
# Event classes carry the fields the loop points read (signature pinning)
# ---------------------------------------------------------------------------

def test_event_classes_field_shape():
    e = AgentStartEvent(session_id="s", node_code="n", cxt=None)
    assert e.session_id == "s" and e.node_code == "n"
    e2 = ToolCallEvent(session_id="s", node_code="n", round_idx=1,
                       tool_name="t", args={})
    assert e2.round_idx == 1 and e2.tool_name == "t"
    r = RewriteToolCall(name="t2", args={"x": 1})
    assert r.name == "t2"
