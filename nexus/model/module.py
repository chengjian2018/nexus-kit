"""
Module — abstract base class for dialogue modules.

Modules come in three types with different dialogue flows:
- AGENT  : pure LLM agent replies directly, no state machine
- FSM    : finite state machine with transitions across node layers (NLU → NLG path)
- ROUTE  : root router + intent menu for top-level dispatch (NLU → NLG path)

Each module can configure NLU / NLG / Agent stage instances at its own level;
the framework default implementation is used when unset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModuleType(Enum):
    """Module type enum."""
    AGENT = "agent"   # pure LLM agent replies directly
    FSM = "fsm"       # finite state machine (multi-layer node transitions)
    ROUTE = "route"   # root router + intent menu


@dataclass
class ModuleLink:
    """Adjacency declaration: one edge in A's sub_modules.

    One field, two responsibilities: it declares the legal transfer targets
    (the transfer-graph edge set) and defines the projection thickness of B
    within A's context (knowledge/tool lending configuration).
    """

    target: str
    lend_knowledge: bool = True
    lend_tools: Optional[List[str]] = None

    def __post_init__(self):
        self.lend_tools = self.lend_tools or []


def _normalize_links(sub_modules: Optional[List[Any]]) -> List[ModuleLink]:
    """Normalize a mixed list of str / ModuleLink into List[ModuleLink].

    The str form (legacy compat) is auto-wrapped with lend_knowledge=True
    and lend_tools=[].
    """
    links: List[ModuleLink] = []
    for item in sub_modules or []:
        if isinstance(item, ModuleLink):
            links.append(item)
        elif isinstance(item, str):
            links.append(ModuleLink(target=item))
        else:
            raise ValueError(f"sub_modules 元素必须是 str 或 ModuleLink: {item!r}")
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
        agent_stage: module-level Agent stage instance (optional, default when unset).
        messages_builder: custom LLM messages builder for AGENT modules, signature
            ``(system_prompt, cxt) -> messages`` list; when unset the default
            build applies (system + user/assistant history); falls back to the
            default with a warning when not callable
            (consumer: chat/messages.py).
        agent_hooks: agent loop hooks declaration (``{point: [hook,...]}``);
            when non-empty it wholesale-replaces the pattern-level declaration
            (consumer: chat/agent_hooks.py).
        generate/pre_recall/query/post_recall: pipeline slot config (node level
            has highest priority).
        enable_clarify: dual-track clarify switch; when True the FSM module
            integrates ClarifyStage (see stages/clarify/).
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
        generate: Optional[Any] = None,
        pre_recall: Optional[Any] = None,
        query: Optional[Any] = None,
        post_recall: Optional[Any] = None,
        agent_stage: Optional[Any] = None,
        messages_builder: Optional[Any] = None,
        agent_hooks: Optional[Any] = None,
        executor: Optional[str] = None,
        enable_clarify: bool = False,
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

        # Pipeline slot config (three-layer priority node > module > pattern,
        # resolved lazily at execution time by stage_slots.resolve_stage;
        # generate accepts a single stage or a {"nlu":…, "nlg":…} dict)
        self.generate = generate
        self.pre_recall = pre_recall
        self.query = query
        self.post_recall = post_recall

        self.sub_modules = _normalize_links(sub_modules)
        self.answer_examples = answer_examples or []

        self.agent_stage = agent_stage

        # AGENT module messages-builder slot (consumer lives in chat/messages.py;
        # like agent_stage, a module-level pluggable component declaration)
        self.messages_builder = messages_builder

        # Agent loop hooks slot: when non-empty, wholesale-replaces the
        # pattern-level declaration (no merge, same semantics as the stage
        # slots; form and consumer: chat/agent_hooks.py)
        self.agent_hooks = agent_hooks

        # Executor plugin declaration (kind="executor"): module-level
        # override with the highest priority (module.executor >
        # pattern.executor_<type> > type default code); resolved at runtime
        # from the plugin registry by the chat layer
        self.executor = executor

        self.enable_clarify = enable_clarify

        self.is_end = is_end

        for legacy in ("nlu_stage", "nlg_stage"):
            if legacy in (kwargs or {}):
                logger.warning(
                    "[module] %s=%r 已废弃：槽位配置请改用 generate="
                    "{'nlu':…, 'nlg':…} 或单 stage（stage_slots.py）",
                    legacy, kwargs[legacy],
                )

        # Extra attributes
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

