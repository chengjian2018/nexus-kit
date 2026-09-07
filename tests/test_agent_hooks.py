"""agent_hooks unit tests: declaration resolution / dispatcher chaining and guards / defensive degradation.

Integration tests for the loop mount side (P1 injection, P4/P5 rewrites,
main-flow tool-name validation) live in test_agent_hooks_loop.py (added in commit 2/3).
"""

import logging

from nexus.engine.agent_hooks import (
    AgentStartEvent,
    RewriteToolCall,
    ToolCallEvent,
    ToolResultEvent,
    collect_fragments,
    fire,
    resolve_agent_hooks,
    rewrite_tool_call,
    rewrite_tool_result,
)


class _Mod:
    def __init__(self, agent_hooks=None):
        self.module_code = "m"
        self.agent_hooks = agent_hooks


class _Pat:
    def __init__(self, agent_hooks=None):
        self.code = "p"
        self.agent_hooks = agent_hooks


def _tc_event(name="t", args=None):
    return ToolCallEvent(session_id="s", module_code="m", round_idx=0,
                         tool_name=name, args=args if args is not None else {})


def _tr_event(result="r"):
    return ToolResultEvent(session_id="s", module_code="m", round_idx=0,
                           tool_name="t", tool_call_id="c1", result=result)


# ---------------------------------------------------------------------------
# resolve_agent_hooks: module wholesale replacement / fallback / defensive degradation
# ---------------------------------------------------------------------------

def test_resolve_module_replaces_pattern_wholesale():
    """A non-empty module.agent_hooks replaces wholesale — pattern-level hooks at the same point do not take effect (no merge)."""
    pat_hook = lambda e: None
    mod_hook = lambda e: None
    hooks = resolve_agent_hooks(_Mod({"on_tool_call": [mod_hook]}),
                                _Pat({"on_tool_call": [pat_hook],
                                      "on_agent_end": [pat_hook]}))
    assert hooks == {"on_tool_call": [mod_hook]}


def test_resolve_falls_back_to_pattern_when_module_empty():
    hooks = resolve_agent_hooks(_Mod(None), _Pat({"on_agent_end": [print]}))
    assert hooks == {"on_agent_end": [print]}


def test_resolve_both_empty_returns_empty():
    assert resolve_agent_hooks(_Mod(), _Pat()) == {}


def test_resolve_non_dict_degrades():
    assert resolve_agent_hooks(_Mod("not-a-dict"), _Pat()) == {}


def test_resolve_unknown_point_and_non_callable_degrade(caplog):
    """Unknown hook points / non-callable entries: skipped with a warning, no raise."""
    good = lambda e: None
    with caplog.at_level(logging.WARNING):
        hooks = resolve_agent_hooks(
            _Mod({"on_tool_call": [good, "nope"], "on_no_such_point": [good]}))
    assert hooks == {"on_tool_call": [good]}
    assert "未知点位" in caplog.text
    assert "非 callable" in caplog.text


def test_resolve_single_callable_normalized_to_list():
    good = lambda e: None
    hooks = resolve_agent_hooks(_Mod({"on_transfer": good}))
    assert hooks == {"on_transfer": [good]}


# ---------------------------------------------------------------------------
# dispatcher: fire / collect_fragments / rewrite_tool_call / rewrite_tool_result
# ---------------------------------------------------------------------------

def test_fire_swallows_hook_exception():
    """An observation-point hook raising: swallowed and the next hook runs, nothing propagates to the caller."""
    seen = []

    def boom(e):
        raise RuntimeError("hook bug")

    def ok(e):
        seen.append(e)

    fire({"on_agent_end": [boom, ok]}, "on_agent_end", "evt")
    assert seen == ["evt"]


def test_collect_fragments_declaration_order_and_failure_drop():
    """Fragments are collected in declaration order; a failing hook's fragment is dropped without affecting the rest."""
    def one(e):
        return "A"

    def two(e):
        raise ValueError("fetch failed")

    def three(e):
        return "B"

    def none_ret(e):
        return None

    frags = collect_fragments(
        {"on_agent_start": [one, two, three, none_ret]},
        AgentStartEvent(session_id="s", module_code="m", cxt=None),
    )
    assert frags == ["A", "B"]


def test_rewrite_tool_call_chain_and_audit():
    """Chained: after hook₁ rewrites args, hook₂ sees the rewritten value; the original audit is complete."""
    seen_args = []

    def h1(e):
        return RewriteToolCall(args={"city": "杭州"})

    def h2(e):
        seen_args.append(e.args)
        return RewriteToolCall(name="t2")

    ev = _tc_event(name="t", args={"city": "北京"})
    name, args, original = rewrite_tool_call(
        {"on_tool_call": [h1, h2]}, ev, allowed_names={"t", "t2"})
    assert seen_args == [{"city": "杭州"}]
    assert (name, args) == ("t2", {"city": "杭州"})
    assert original == {"name": "t", "args": {"city": "北京"}}


def test_rewrite_tool_call_rejects_unknown_name_keeps_args():
    """Rename target not in allowed_names: rename rejected, args still applied (partial rewrite)."""
    def h(e):
        return RewriteToolCall(name="ghost_tool", args={"a": 1})

    ev = _tc_event(name="t")
    name, args, original = rewrite_tool_call(
        {"on_tool_call": [h]}, ev, allowed_names={"t"})
    assert name == "t"
    assert args == {"a": 1}
    assert original == {"name": "t", "args": {}}


def test_rewrite_tool_call_rejects_reserved_prefix():
    """Rename to a transfer_to_ prefixed name: rejected (prevents smuggled control flow)."""
    def h(e):
        return RewriteToolCall(name="transfer_to_x")

    ev = _tc_event(name="t")
    name, _args, original = rewrite_tool_call(
        {"on_tool_call": [h]}, ev, allowed_names={"t"},
        reserved_prefix="transfer_to_")
    assert name == "t"
    assert original is None


def test_rewrite_tool_call_hook_failure_keeps_current(caplog):
    def boom(e):
        raise RuntimeError("bug")

    def fix(e):
        return RewriteToolCall(name="t2")

    ev = _tc_event(name="t")
    with caplog.at_level(logging.ERROR):
        name, _args, original = rewrite_tool_call(
            {"on_tool_call": [boom, fix]}, ev, allowed_names={"t", "t2"})
    assert name == "t2"
    assert original == {"name": "t", "args": {}}


def test_rewrite_tool_call_accepts_dict_return_shape():
    """Tolerates a dict return shape ({"name":…, "args":…}); both empty is treated as no rewrite."""
    def h(e):
        return {"args": {"x": 1}}

    ev = _tc_event(name="t")
    name, args, original = rewrite_tool_call(
        {"on_tool_call": [h]}, ev, allowed_names={"t"})
    assert (name, args) == ("t", {"x": 1})
    assert original == {"name": "t", "args": {}}


def test_rewrite_tool_result_chain_and_audit():
    orig_seen = []

    def h1(e):
        return e.result.replace("raw", "v1")

    def h2(e):
        orig_seen.append(e.result)
        return "v2"

    ev = _tr_event(result="raw")
    final, original = rewrite_tool_result({"on_tool_result": [h1, h2]}, ev)
    assert orig_seen == ["v1"]      # chained: h2 sees h1's output
    assert final == "v2"
    assert original == "raw"


def test_rewrite_tool_result_no_rewrite_returns_none_audit():
    final, original = rewrite_tool_result({"on_tool_result": [lambda e: None]},
                                          _tr_event("raw"))
    assert (final, original) == ("raw", None)
