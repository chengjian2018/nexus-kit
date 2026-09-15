"""Tool-call context — contextvar injection for tool handlers.

Tool handlers receive only ``args`` (``nexus/registry/tools.py::dispatch``):
no DialogueContext, no node, no pattern. That minimal contract is right for
the vast majority of tools, but context-bound tools (currently
``delegate_task``) need two things from the caller: which llm_config to use,
and which toolsets the executing pattern authorized. The default loop
executor publishes an immutable snapshot here around its tool rounds; the
handler side reads it with ``current_tool_context()`` — None when the call
arrived outside an agent loop, in which case the caller must carry its own
fallback.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Dict, FrozenSet, Iterator, Optional


@dataclass(frozen=True)
class ToolCallContext:
    """Immutable snapshot of the calling agent loop's position.

    llm_config:     the executing node's ``cxt.llm_config`` (provider code /
                    model / temperature / max_tokens / ...).
    allow_toolsets: the executing pattern's ``allow_toolset`` — the
                    authorization boundary a sub-agent inherits.
    in_subagent:    True while a delegate_task sub-loop is running (recursion
                    guard; v1 depth = 1 — sub-agents cannot delegate).
    in_workflow:    True while a run_workflow topology is running — its leaf
                    sub-agents can neither delegate nor start workflows.
    session_id:     the executing session — the scoping key for session-bound
                    tools (read_tasks/write_tasks). Empty string = detached
                    call (direct dispatch outside an agent loop); sub-agents
                    and workflow leaves inherit the parent session's id via
                    replace(), so they share one task list per session.
    skills_dir:     the executing pattern's effective skill scan root
                    (nexus/skills.py resolution) — how the load_skill /
                    read_skill_file handlers find the skill directory.
                    Empty string = detached call, falls back to the
                    configured global root.
    pattern_code:   the executing pattern's code — the locating key for the
                    app-overlaid tool guardrails (settings merges the app
                    guardrails section over the global one by this code).
                    Empty string = detached call, falls back to the global
                    guardrails; sub-agents and workflow leaves inherit the
                    parent's code via replace().
    enabled_skills: the executing node's resolved skill set (use_skills ∩
                    allow_skills) — the load-side authorization boundary:
                    a skill name outside it is rejected with an error
                    backfill. None (never set) = detached call, any scanned
                    skill may be loaded (read-only knowledge; the执行面
                    authorization still lives in the tool 三层收口).
    """

    llm_config: Dict[str, Any]
    allow_toolsets: FrozenSet[str]
    in_subagent: bool = False
    in_workflow: bool = False
    session_id: str = ""
    skills_dir: str = ""
    pattern_code: str = ""
    enabled_skills: Optional[FrozenSet[str]] = None


_CURRENT: ContextVar[Optional[ToolCallContext]] = ContextVar(
    "nexus_tool_call_context", default=None)


def current_tool_context() -> Optional[ToolCallContext]:
    """Return the ambient ToolCallContext, or None outside an agent loop."""
    return _CURRENT.get()


def ambient_pattern_code() -> str:
    """The ambient context's pattern_code, or "" outside an agent loop —
    the locating key guardrail handlers pass to the settings accessors
    (empty code = no app overlay, the global section applies)."""
    ctx = _CURRENT.get()
    return ctx.pattern_code if ctx is not None else ""


@contextmanager
def tool_call_context(llm_config, allow_toolsets, session_id: str = "",
                      skills_dir: str = "",
                      pattern_code: str = "",
                      enabled_skills: Optional[FrozenSet[str]] = None
                      ) -> Iterator[ToolCallContext]:
    """Publish the caller's position for the duration of the block.

    Values are copied into a fresh frozen snapshot, so later mutation of the
    caller's llm_config dict cannot leak into handlers mid-block.
    ``pattern_code`` keys the app guardrails overlay; callers that don't
    carry one (custom executors predating the field) default to "" — the
    global guardrails, never an error.
    """
    ctx = ToolCallContext(
        llm_config=dict(llm_config or {}),
        allow_toolsets=frozenset(allow_toolsets or []),
        session_id=str(session_id or ""),
        skills_dir=str(skills_dir or ""),
        pattern_code=str(pattern_code or ""),
        enabled_skills=frozenset(enabled_skills or ())
        if enabled_skills is not None else None,
    )
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


@contextmanager
def subagent_scope(base: ToolCallContext) -> Iterator[None]:
    """Mark the ambient context as inside a delegate_task sub-loop.

    Inner tool dispatches then observe ``in_subagent=True``; a nested
    delegate_task call refuses immediately (structural exclusion of the
    ``subagent`` toolset from the sub-agent pool is the first line of
    defense — this is the second).
    """
    token = _CURRENT.set(replace(base, in_subagent=True))
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def workflow_scope(base: ToolCallContext) -> Iterator[None]:
    """Mark the ambient context as inside a run_workflow topology.

    Leaf sub-agents dispatched within observe both flags set; delegate_task
    and run_workflow both refuse to nest (v1 depth = 1). Pool-level
    exclusion of the ``subagent``/``workflow`` toolsets is the first line
    of defense — this is the second.
    """
    token = _CURRENT.set(replace(base, in_subagent=True, in_workflow=True))
    try:
        yield
    finally:
        _CURRENT.reset(token)
