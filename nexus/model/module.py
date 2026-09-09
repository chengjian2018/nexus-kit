"""
Module — abstract base class for dialogue modules.

Modules come in three types with different dialogue flows:
- AGENT  : pure LLM agent replies directly, no state machine
- FSM    : finite state machine with transitions across node layers (NLU → NLG path)
- ROUTE  : root router + intent menu for top-level dispatch (NLU → NLG path)

All fields are declarative (str / bool / list / dict — no object references):
pipeline slots go through ``stages: {slot_name: stage_code}`` (strings,
resolved from the plugin registry at execution time), adjacency edges through
``sub_modules: List[Dict]`` (the ModuleLink dataclass is gone; dict shape
{"target": str, "lend_knowledge": bool, "lend_tools": [str]}), and pluggable
callables (messages_builder / agent_hooks) through string codes registered in
the plugin registry.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModuleType(Enum):
    """Module type enum."""

    AGENT = "agent"   # pure LLM agent replies directly
    FSM = "fsm"       # finite state machine (multi-layer node transitions)
    ROUTE = "route"   # root router + intent menu


def _normalize_links(sub_modules: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Normalize sub_modules into List[Dict] adjacency declarations.

    Accepted element forms:
    - dict (canonical): keys target / lend_knowledge (default True) /
      lend_tools (default []); unknown keys are kept as-is
    - str (legacy shorthand): auto-wrapped with lend_knowledge=True,
      lend_tools=[]
    """
    links: List[Dict[str, Any]] = []
    for item in sub_modules or []:
        if isinstance(item, dict):
            link = dict(item)  # copy: never mutate the caller's declaration
            link.setdefault("lend_knowledge", True)
            link.setdefault("lend_tools", [])
            link["lend_tools"] = list(link["lend_tools"] or [])
            links.append(link)
        elif isinstance(item, str):
            links.append({"target": item, "lend_knowledge": True,
                          "lend_tools": []})
        else:
            raise ValueError(
                f"sub_modules 元素必须是 str 或 dict（target/lend_knowledge/"
                f"lend_tools）: {item!r}"
            )
    return links


class BaseModule:
    """Base class for dialogue modules.

    Each module represents an independent dialogue capability unit; it may
    contain nodes (FSM type) or run directly as an agent (AGENT type).

    Attributes:
        type: module type, determines the dialogue flow.
        module_code: unique module code.
        module_name: module name.
        module_description: module description.
        module_todo_description: module todo description.
        module_nodes: list of nodes in the module (used by FSM/ROUTE types).
        use_tools: list of tools available to the module.
        base_prompt: module base prompt (used by AGENT type).
        stages: pipeline slot config ``{slot_name: stage_code}`` — the module
            layer of node > module > skeleton.
        sub_modules: adjacency declarations ``[{"target": str,
            "lend_knowledge": bool, "lend_tools": [str]}]`` — the transfer-graph
            edge set and the knowledge/tool lending (projection thickness).
        executor: executor plugin code (module-level override, highest
            priority; module.executor > pattern.executor_<family> > type
            default).
        enable_project: projection switch (plan-⑥, see above; default True —
            projection is the only branch with default behavior once
            transfer is gone).
        messages_builder: messages-builder plugin code (kind=
            "messages_builder"); module-level overrides the pattern-level
            declaration.
        agent_hooks: agent-hooks plugin code (kind="agent_hooks");
            module-level wholesale-replaces the pattern-level declaration.
    """

    type: ModuleType = ModuleType.AGENT

    def __init__(
        self,
        module_code: Optional[str] = None,
        module_name: Optional[str] = None,
        module_description: Optional[str] = None,
        module_todo_description: Optional[str] = None,
        module_nodes: Optional[List[Any]] = None,
        sub_modules: Optional[List[Any]] = None,
        use_tools: Optional[List[Any]] = None,
        base_prompt: Optional[str] = None,
        base_nlu_prompt: Optional[str] = None,
        base_nlg_prompt: Optional[str] = None,
        stages: Optional[Dict[str, str]] = None,
        executor: Optional[str] = None,
        enable_project: bool = True,
        agent_stage: Optional[str] = None,
        messages_builder: Optional[str] = None,
        agent_hooks: Optional[str] = None,
        is_end: Optional[bool] = False,
        answer_examples: Optional[List[str]] = None,
        **kwargs,
    ):
        self.module_code = module_code
        self.module_name = module_name
        self.module_description = module_description
        self.module_todo_description = module_todo_description
        self.module_nodes = module_nodes or []
        self.use_tools = use_tools or []
        self.base_prompt = base_prompt
        self.base_nlu_prompt = base_nlu_prompt
        self.base_nlg_prompt = base_nlg_prompt

        # Pipeline slot config (module layer of node > module > skeleton;
        # codes are strings resolved via the plugin registry — declarative,
        # no object references)
        self.stages = dict(stages) if stages else {}

        self.sub_modules = _normalize_links(sub_modules)
        self.answer_examples = answer_examples or []

        # Executor plugin declaration (kind="executor"): module-level
        # override with the highest priority
        self.executor = executor

        # Projection switch (plan-⑥): True = this module serves its parent's
        # context via knowledge projection (the parent answers this turn
        # with the projected knowledge; a defer_to_module call schedules the
        # deferred base switch); False = this module is a jump target
        # (same-turn handoff via ModuleJumpEvent). A module that has handed
        # off / deferred is force-projected afterwards (anti-ping-pong, via
        # cxt.metadata["forced_projection"] — the shared Pattern/Module
        # singletons are never mutated)
        self.enable_project = enable_project

        # agent_stage slot (kind="stage"): kept as a string code for custom
        # agent-stage declarations; resolved by executors that consume it
        self.agent_stage = agent_stage

        # AGENT module messages-builder slot (kind="messages_builder";
        # module-level overrides the pattern-level declaration)
        self.messages_builder = messages_builder

        # Agent loop hooks slot (kind="agent_hooks"; module-level
        # wholesale-replaces the pattern-level declaration)
        self.agent_hooks = agent_hooks

        self.is_end = is_end

        for key, value in (kwargs or {}).items():
            setattr(self, key, value)

        self._init_node()

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"code={self.module_code!r} type={self.type.value!r}>"
        )

    def _init_node(self):
        self.node_name = self.module_name
        self.node_code = self.module_code
        self.node_description = self.module_description
        self.node_todo_description = self.module_todo_description

    def to_projection_text(self) -> str:
        """Module header projection: injected into the agent prompt of adjacent
        modules (inject primitive).

        Contains only the four header fields (name/description/todo/
        answer_examples), not internal flow prompts — flow depth is never
        projected; going deeper requires transfer (spec §1.2).
        """
        parts = []
        if self.module_name:
            parts.append(f"- 定义：【{self.module_name}】{self.module_description or ''}")
        if self.module_todo_description:
            parts.append(f"- 职责：{self.module_todo_description}")
        if self.answer_examples:
            examples = "；".join(self.answer_examples)
            parts.append(f"- 回答范式：「{examples}」")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Convenience subclasses
# ---------------------------------------------------------------------------

class AgentModule(BaseModule):
    """Pure Agent module — replies directly via LLM, no state machine."""

    type = ModuleType.AGENT


class FSMModule(BaseModule):
    """Finite state machine module — multi-layer node transitions, NLU → NLG path."""

    type = ModuleType.FSM


class RouteModule(BaseModule):
    """Route module — root router + intent menu, for top-level dispatch."""

    type = ModuleType.ROUTE
