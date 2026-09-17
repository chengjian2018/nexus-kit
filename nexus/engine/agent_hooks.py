"""Agent loop hooks — hook-point events + declaration parsing + dispatcher.

**Status: mechanism live with one in-repo package.** The 7 loop points
stay wired (see atoms/executors/loop_executor.py); ``atoms/hooks/
tool_guard.py`` is the first shipped hooks package (kind="agent_hooks",
code="tool_guard" — P4 dangerous-op announce, observe-only). A pattern declares it
via ``plugins={"agent_hooks": "tool_guard"}``; with no declaration every
point remains a zero-overhead pass-through (the tested contract in
tests/test_agent_hooks_contract.py).

Point inventory (P1-P7, consumed at the loop's hook points):

===== ================ =====================================================
Point Event            Semantics (when implemented)
===== ================ =====================================================
P1    on_agent_start   inject: returns Optional[str] fragments, appended as
                        extension-context blocks by the messages builder
P2    on_llm_call      observe: before each LLM call (messages read-only)
P3    on_llm_response  observe: after each LLM response
P4    on_tool_call     mutate: returns Optional[RewriteToolCall]
                        (name/args rewrite, guarded by allowed_names)
P5    on_tool_result   mutate: returns Optional[str] (result rewrite)
P6    (removed)        the defer/transfer machinery is gone with the module
                        layer — deliberately no hook point
P7    on_agent_end     observe: exits (reply / max_rounds)
===== ================ =====================================================

Declaration (pattern level, node level overriding it wholesale — the same
precedence as the other plugin slots). Forms: a plugin code string
(kind="agent_hooks", resolving to the map or a zero-arg factory of it), a
zero-arg callable returning the map, or the inline legacy dict::

    pattern.agent_hooks = "my_hooks_pkg"
    # or inline: {"on_tool_call": [fix_tool_alias], ...}

Error semantics (defensive, part of the retained contract): hook exceptions
are always swallowed, logged, and the original value kept; the dialogue is
never blocked; a mutating hook failing = continue with the value as it was
before entering the chain.

Boundary with the sibling extension points: replacing the final messages
belongs to messages_builder; swapping the whole executor belongs to the
executor plugin — hooks are additive observation/mutation, never a second
replacement path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# All legal hook points (consumed at the hook points in loop.run_agent)
HOOK_POINTS = (
    "on_agent_start",    # P1: entering the loop, before building the system prompt (inject)
    "on_llm_call",       # P2: before each turn's LLM call (observe)
    "on_llm_response",   # P3: after each turn's LLM response (observe)
    "on_tool_call",      # P4: before a single tool execution (mutate: name/args)
    "on_tool_result",    # P5: after a single tool execution, before writing history (mutate: result string)
    "on_agent_end",      # P7: exits: reply / max_rounds (observe)
)

HookMap = Dict[str, List[Callable[..., Any]]]


# ============================================================================
# Event types (one per hook point; all carry session_id / node_code)
# ============================================================================

@dataclass
class AgentStartEvent:
    """P1: entering the loop, before building the system prompt. Returns an Optional[str] injected fragment.

    cxt is passed by reference (read-only discipline): the hook reads slots/metadata from it to
    decide what data to fetch but does not write back — injected output travels only via the
    return value, avoiding cross-turn state leakage.
    """

    session_id: str
    node_code: str
    cxt: Any


@dataclass
class LLMCallEvent:
    """P2: before each turn's LLM call. Observation (return value ignored). messages is a reference (read-only discipline)."""

    session_id: str
    node_code: str
    round_idx: int
    messages: List[Dict[str, Any]]
    model: str


@dataclass
class LLMResponseEvent:
    """P3: after each turn's LLM response (content/tool_calls already parsed). Observation."""

    session_id: str
    node_code: str
    round_idx: int
    content: str
    tool_calls: List[Dict[str, Any]]


@dataclass
class ToolCallEvent:
    """P4: before a single tool execution. Returns Optional[RewriteToolCall] (chained: the previous
    hook's rewrite is reflected into this event before it is fed to the next hook)."""

    session_id: str
    node_code: str
    round_idx: int
    tool_name: str
    args: Dict[str, Any]


@dataclass
class RewriteToolCall:
    """Rewrite return value for P4: only the given fields take effect (partial rewrite); None = no change."""

    name: Optional[str] = None
    args: Optional[Dict[str, Any]] = None


@dataclass
class ToolResultEvent:
    """P5: after a single tool execution, before writing history. Returns Optional[str] replacing the
    result (chained like P4; the rewritten result goes to both the LLM backfill and the store — no fork)."""

    session_id: str
    node_code: str
    round_idx: int
    tool_name: str
    tool_call_id: str
    result: str


@dataclass
class AgentEndEvent:
    """P7: the loop exits. Observation.

    outcome: "reply" (direct answer, reply is the exit text) /
    "max_rounds" (round limit exceeded, reply is the fallback text).
    """

    session_id: str
    node_code: str
    rounds: int
    outcome: str
    reply: Optional[str] = None


# ============================================================================
# Declaration parsing (node replaces pattern wholesale; invalid config degrades to skip)
# ============================================================================

def resolve_agent_hooks(node: Any, pattern: Any = None) -> HookMap:
    """Resolve the effective hooks: a non-empty node-level declaration
    (node.plugins["agent_hooks"]) replaces wholesale, else the pattern level
    (pattern.plugins["agent_hooks"]).

    The declaration is a **string code** (plugin registry
    kind="agent_hooks") resolving to a hooks package (a callable returning
    the {point: [hook,...]} dict, or the dict itself). The legacy dict form
    is still accepted inline (transitional). Defensive validation (same
    degradation style as stage_slots): unknown form / unregistered code /
    non-dict package / unknown point name / non-callable entry → warning
    and skip, no raise.
    """
    raw = ((getattr(node, "plugins", None) or {}).get("agent_hooks")
           if node is not None else None)
    if not raw and pattern is not None:
        raw = (getattr(pattern, "plugins", None) or {}).get("agent_hooks")
    if not raw:
        return {}

    from nexus.registry.plugins import registry as plugin_registry

    if isinstance(raw, str):
        if plugin_registry.has("agent_hooks", raw):
            raw = plugin_registry.resolve("agent_hooks", raw)
        else:
            logger.warning(
                "[agent_hooks] agent_hooks=%r 未注册（kind=agent_hooks），"
                "忽略", raw,
            )
            return {}
    elif callable(raw):
        raw = raw()

    if not isinstance(raw, dict):
        logger.warning(
            "[agent_hooks] agent_hooks 解析结果须为 {点位: [hook,...]} dict，忽略: %r", raw,
        )
        return {}

    hooks: HookMap = {}
    for point, entries in raw.items():
        if point not in HOOK_POINTS:
            logger.warning(
                "[agent_hooks] 未知点位 %r（合法点位: %s），跳过",
                point, ", ".join(HOOK_POINTS),
            )
            continue
        if not isinstance(entries, (list, tuple)):
            entries = [entries]
        valid = [h for h in entries if callable(h)]
        dropped = len(entries) - len(valid)
        if dropped:
            logger.warning(
                "[agent_hooks] 点位 %s 含 %d 个非 callable 条目，已跳过",
                point, dropped,
            )
        if valid:
            hooks[point] = valid
    return hooks


# ============================================================================
# Dispatcher (exception containment: swallow + log + fall back to the original value)
# ============================================================================

def fire(hooks: HookMap, point: str, event: Any) -> None:
    """Generic dispatch for observation points: run hooks one by one; exceptions are swallowed and logged (return value unused)."""
    for hook in hooks.get(point, ()):
        try:
            hook(event)
        except Exception:
            logger.exception(
                "[agent_hooks] %s hook 异常（忽略，不影响对话）", point,
            )


def collect_fragments(hooks: HookMap, event: AgentStartEvent) -> List[str]:
    """P1 dispatch: collect injected fragments (declaration order); an exception hook's fragment is dropped."""
    fragments: List[str] = []
    for hook in hooks.get("on_agent_start", ()):
        try:
            frag = hook(event)
        except Exception:
            logger.exception(
                "[agent_hooks] on_agent_start hook 异常，丢弃该片段", 
            )
            continue
        if frag:
            fragments.append(str(frag))
    return fragments


def _normalize_rewrite(rw: Any) -> Optional[RewriteToolCall]:
    """Accept both RewriteToolCall and a same-shaped dict as return forms; anything else counts as no rewrite."""
    if isinstance(rw, RewriteToolCall):
        return rw
    if isinstance(rw, dict):
        name = rw.get("name")
        args = rw.get("args")
        if name is None and args is None:
            return None
        return RewriteToolCall(name=name, args=args)
    return None


def rewrite_tool_call(
    hooks: HookMap, event: ToolCallEvent, allowed_names: set,
) -> Tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
    """P4 dispatch: chained rewrite of name/args (hook₁'s rewrite is reflected into the event before feeding hook₂).

    Guard (rule 2 at the dispatcher layer): a rewritten name not in
    allowed_names (this execution's resolved set) → reject that rename +
    warning; the legal parts of the same return value are applied as usual
    (name rejected, args applied). The dispatcher only guarantees "a rewrite
    cannot make things worse" — if the name fallen back to after rejection
    is still illegal (the original call was a hallucinated name to begin
    with), the loop main flow's final validation catches it as the fallback.

    Returns:
        (final_name, final_args, original): original is
        ``{"name": original name, "args": original args}``, non-None only when a rewrite actually
        happened (for audit).
    """
    orig_name, orig_args = event.tool_name, event.args
    for hook in hooks.get("on_tool_call", ()):
        try:
            rw = _normalize_rewrite(hook(event))
        except Exception:
            logger.exception(
                "[agent_hooks] on_tool_call hook 异常，保留当前值继续",
            )
            continue
        if rw is None:
            continue

        if rw.args is not None:
            event.args = rw.args
        if rw.name is not None and rw.name != event.tool_name:
            if rw.name not in allowed_names:
                logger.warning(
                    "[agent_hooks] 改写 name '%s' 不在本轮可用工具中，拒绝改名"
                    "（保留 '%s'）", rw.name, event.tool_name,
                )
            else:
                event.tool_name = rw.name

    original = None
    if event.tool_name != orig_name or event.args is not orig_args:
        original = {"name": orig_name, "args": orig_args}
    return event.tool_name, event.args, original


def rewrite_tool_result(
    hooks: HookMap, event: ToolResultEvent,
) -> Tuple[str, Optional[str]]:
    """P5 dispatch: chained rewrite of the result string.

    Returns:
        (final_result, original): original is non-None only when a rewrite actually happened.
    """
    orig_result = event.result
    for hook in hooks.get("on_tool_result", ()):
        try:
            new = hook(event)
        except Exception:
            logger.exception(
                "[agent_hooks] on_tool_result hook 异常，保留当前值继续",
            )
            continue
        if new is None:
            continue
        event.result = new if isinstance(new, str) else str(new)
    original = None if event.result is orig_result else orig_result
    return event.result, original
