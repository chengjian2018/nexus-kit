"""
Dialogue system base types — PipelineStage, SessionMessage, DialogueContext

All stages and modules depend on these standard types to keep session storage
and context passing consistent.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)



# ============================================================================
# Module jump event (same-turn reroute primitive)
# ============================================================================

@dataclass
class ModuleJumpEvent:
    """A module jump intent — produced inside a stage / agent turn, consumed
    uniformly by the chat layer.

    Producers (write to ``cxt.actions``):
    - NLU in-turn detection: ``nlu_result.jump_module`` points at another module
    - ROUTE menu node config: ``node.jump_module`` (after next_node hits)
    - custom executor plugins (agent-as-tool / delegate recipes — see
      ARCHITECTURE.md's module-movement section)

    Consumer (chat layer hop loop): reroutes as long as the target exists in
    module_map (writes current_module_code, clears current_node_code) —
    adjacency boundaries are deliberately blurred.

    Also keeps compatibility with dict-form actions (e.g.
    ``{"conversation_end": True}``): elements of other types in the actions
    list are snapshotted verbatim into ChatResult.actions.
    """

    target_module_code: str
    reason: str = ""      # Transfer context: picked up by the target module (injected into its prompt)
    source: str = ""      # nlu_jump / route_menu / handoff_tool

    def to_dict(self) -> Dict[str, Any]:
        """Observation form: snapshotted into ChatResult.actions / rendered by the cli."""
        return {
            "module_jump": {
                "target": self.target_module_code,
                "reason": self.reason,
                "source": self.source,
            }
        }


# ============================================================================
# Deferred module switch (end-of-turn base switch primitive, plan-⑥)
# ============================================================================

@dataclass
class DeferredModuleSwitch:
    """A deferred base switch — projection-served turn's exit signal.

    Semantics (mutually exclusive with the same-turn ModuleJumpEvent): the
    current module answered this turn using the target's projected knowledge
    (enable_project=True adjacency); the LLM flagged via the defer_to_module
    tool that deeper flow belongs to the target. Consumption happens at END
    of turn (after the hop loop, before end_turn): current_module_code is
    rewritten to the target, so the NEXT turn runs on the target as its
    base. The event also snapshots into ChatResult.actions for observability.

    Producers: the default loop executor's defer_to_module tool (and custom
    executors writing this event).
    Consumer: chat_turn_stream's end-of-turn apply (existence-checked; a
    hallucinated target warns and keeps the current base).
    """

    target_module_code: str
    reason: str = ""
    source: str = "projection"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_switch": {
                "target": self.target_module_code,
                "reason": self.reason,
                "source": self.source,
            }
        }


# ============================================================================
# Pipeline stage base class
# ============================================================================

class PipelineStage(ABC):
    """A pluggable step in the Pipeline.

    Each stage implements ``execute(ctx) -> ctx`` and can be freely combined in Pattern.stages.
    """

    stage_name: str = ""

    @abstractmethod
    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        """Run this stage's logic and return the modified context
        (async since the asyncio rewrite — stages may drive LLM/recall I/O)."""
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.stage_name!r}>"


# ============================================================================
# Standardized session message
# ============================================================================

@dataclass
class SessionMessage:
    """Standardized session message format.

    All messages produced by stages use this format to keep session storage consistent.

    tool-trace conventions (replayed to the LLM in full across turns; see the
    replay guard in chat/messages.py) — no new fields/table columns added;
    everything rides on the existing JSON channels:
    - assistant tool round: content is the ``{"content": str, "tool_calls": [...]}``
      JSON payload (encoded by ``encode_tool_call_content`` / parsed by ``decode``)
    - tool row: ``metadata["tool_call_id"]`` links to the LLM tool_call id
    - role ``summary``: LLM summary row produced by history compression
      (wrapped as user-role untrusted on replay)
    """

    role: Literal["system", "user", "assistant", "tool", "summary"]
    content: str
    stage: str = ""  # source stage: pre_recall / query_rewrite / nlu / nlg / agent / state_update
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "stage": self.stage,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionMessage":
        return cls(
            role=d.get("role", "user"),
            content=d.get("content", ""),
            stage=d.get("stage", ""),
            metadata=d.get("metadata", {}),
        )


def encode_tool_call_content(
    content: str, tool_calls: List[Dict[str, Any]]
) -> str:
    """JSON payload encoding for assistant tool-round content (Customer-Agent's convention)."""
    return json.dumps(
        {"content": content, "tool_calls": tool_calls}, ensure_ascii=False)


def decode_tool_call_content(
    content: str,
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Parse an assistant row's JSON payload into ``(text, tool_calls)``; returns None for non-tool rounds.

    Non-tool-round criteria: not JSON / no ``tool_calls`` key / empty list
    (the loop only encodes when tool_calls is non-empty; an empty list is
    treated as a plain text row).
    """
    if not content or not content.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("tool_calls"):
        return None
    calls = payload["tool_calls"]
    if not isinstance(calls, list):
        return None
    return str(payload.get("content") or ""), calls


# ============================================================================
# Dialogue context (data carrier throughout the Pipeline)
# ============================================================================

@dataclass
class DialogueContext:
    """Dialogue context flowing through the whole Pipeline.

    Every PipelineStage receives and returns this object; all intermediate results are stored here.

    # metadata key conventions (self-managed by stages / the chat layer):
    #   served_by_projection : Dict{module, source}  a projection-borrowed answering turn: borrower module and source domain
    #   clarify              : Dict                  set and cleared each turn by ClarifyStage
    """

    session_id: str
    user_query: str

    # Session history (standardized message list)
    history: List[SessionMessage] = field(default_factory=list)

    # Index of this turn's user row in history (snapshot taken by begin_turn
    # before adding the user message; default_build_messages uses it to split
    # cross-turn history / explicit query / rows appended within this turn's hops)
    turn_history_start: int = 0

    # Per-message write-through persistence hook (injected by SessionStore.attach;
    # None = disabled). Exceptions are swallowed and logged on the add_message
    # side — they must never block the dialogue.
    message_sink: Optional[Any] = None

    # Recall results before query rewrite
    pre_recall_results: List[Dict[str, Any]] = field(default_factory=list)

    # Query list after rewrite
    rewritten_queries: List[str] = field(default_factory=list)

    # Recall results after query rewrite
    post_recall_results: List[Dict[str, Any]] = field(default_factory=list)

    # NLU result: {"intent": str, "slots": {...}, "confidence": float}
    nlu_result: Optional[Dict[str, Any]] = None

    # NLG result
    nlg_result: Optional[Dict[str, Any]] = None

    # Agent direct reply result
    agent_result: Optional[Dict[str, Any]] = None

    # Current state
    current_module_code: Optional[str] = None
    current_node_code: Optional[str] = None
    filled_slots: Dict[str, Any] = field(default_factory=dict)

    # Extra metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Task base info
    task_basic_info: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Pipeline infrastructure (injected by the pipeline runner; stages need not store it)
    # ------------------------------------------------------------------
    node_map: Dict[str, Any] = field(default_factory=dict)
    module_map: Dict[str, Any] = field(default_factory=dict)
    llm_config: Optional[Dict[str, Any]] = None

    # Actions reserved for this turn (e.g. sends / transitions / external calls the reply should
    # trigger besides the text). Stages/handlers may append; the chat layer snapshots per turn
    # (see TurnLifecycle in chat/context_lifecycle.py — per-turn reset).
    # Module jumps are carried as ModuleJumpEvent instances (consumed by the chat
    # layer's hop loop, which then reroutes); other dict-form actions are
    # snapshotted verbatim into ChatResult.actions.
    actions: List[Any] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def add_message(
        self,
        role: str,
        content: str,
        stage: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a standardized message to history.

        Tool traces go through the content/metadata payload (see the
        SessionMessage docstring): assistant tool-round content is encoded
        with ``encode_tool_call_content`` first; a tool row's
        ``tool_call_id`` goes in metadata.
        """
        msg = SessionMessage(
            role=role,
            content=content,
            stage=stage,
            metadata=metadata or {},
        )
        self.history.append(msg)
        if self.message_sink is not None:
            try:
                self.message_sink(msg)
            except Exception:
                logger.exception(
                    "message_sink 写入失败（不影响对话）: session=%s role=%s stage=%s",
                    self.session_id, msg.role, msg.stage,
                )

    def format_history(self, max_turns: int = 10) -> str:
        """Format the last N turns as text for prompt injection."""
        # Keep only user and assistant messages with displayable text, drop
        # system / tool / summary. Assistant tool-round content is a JSON payload;
        # take the inner text (skip the whole row if the inner text is empty —
        # tool rounds usually don't speak to the user)
        lines: List[str] = []
        for msg in self.history:
            if msg.role not in ("user", "assistant"):
                continue
            text = msg.content
            decoded = decode_tool_call_content(msg.content)
            if decoded is not None:
                text = decoded[0]
            if text:
                lines.append(f"{msg.role}: {text}")
        recent = lines[-max_turns * 2 :]  # user + assistant come in pairs
        if not recent:
            return "（暂无历史对话）"
        return "\n".join(recent)
        recent = filtered[-max_turns * 2 :]  # user + assistant come in pairs
        if not recent:
            return "（暂无历史对话）"
        lines = []
        for msg in recent:
            lines.append(f"{msg.role}: {msg.content}")
        return "\n".join(lines)

    def format_slots(self) -> str:
        """Format filled slots as JSON for prompt injection."""
        if self.filled_slots:
            return json.dumps(self.filled_slots, ensure_ascii=False, indent=2)
        return "{}"

    def format_recall_info(self) -> str:
        """Format recall results for prompt injection; post-rewrite results take priority."""
        results = self.post_recall_results or self.pre_recall_results
        if results:
            return json.dumps(results, ensure_ascii=False, indent=2)
        return "暂无召回信息"

    def format_rewritten_queries(self) -> str:
        """Format rewrite results as text for prompt injection."""
        if self.rewritten_queries:
            return "\n".join(self.rewritten_queries)
        return ""

    # ------------------------------------------------------------------
    # Current node / module accessors
    # ------------------------------------------------------------------

    def get_next_node(self):
        nlu_result = self.nlu_result or {}
        next_node = nlu_result.get("next_node", "")
        if next_node not in self.node_map:
            return None
        return self.node_map[next_node]


    def get_current_node(self) -> Optional[Any]:
        """Return the current node instance from node_map (None when unset)."""
        if not self.current_node_code:
            return None
        return self.node_map.get(self.current_node_code)

    def get_current_module(self) -> Optional[Any]:
        """Return the current module instance from module_map (None when unset)."""
        if not self.current_module_code:
            return None
        return self.module_map.get(self.current_module_code)

    # ------------------------------------------------------------------
    # Node / module slot formatting — delegation to the data layer
    # (node.py / module.py own the formatting; ctx only resolves "which node/module")
    # ------------------------------------------------------------------

    def format_nlg_next_node(self, stage: str = "nlg") -> str:
        """Format the current node as prompt-ready text (slot: cur_node).

        Stage-specific variants — NLU and NLG need different facets of the node:
        - "nlu": name + todo description + slot definitions (what to collect/decide)
        - "nlg": name + node description (what scenario the reply is grounded in)
        - "full": all fields (used by retrieval stages: query rewrite / recall)

        Args:
            stage: which stage's facet to format ("nlu" / "nlg" / "full").
        """
        node = self.get_next_node()
        if node is None:
            return "暂无当前节点信息"

        formatters = {
            "nlu": node.to_nlu_prompt_text,
            "nlg": node.to_nlg_prompt_text,
        }
        formatter = formatters.get(stage, node.to_prompt_text)
        return formatter()
    
    def format_cur_node(self, stage: str = "nlu") -> str:
        """Format the current node as prompt-ready text (slot: cur_node).

        Stage-specific variants — NLU and NLG need different facets of the node:
        - "nlu": name + todo description + slot definitions (what to collect/decide)
        - "nlg": name + node description (what scenario the reply is grounded in)
        - "full": all fields (used by retrieval stages: query rewrite / recall)

        Args:
            stage: which stage's facet to format ("nlu" / "nlg" / "full").
        """
        node = self.get_current_node()
        if node is None:
            return "暂无当前节点信息"

        formatters = {
            "nlu": node.to_nlu_prompt_text,
            "nlg": node.to_nlg_prompt_text,
        }
        formatter = formatters.get(stage, node.to_prompt_text)
        return formatter()

    def format_next_nodes(self) -> str:
        """Format the current node's sub-node list as prompt-ready text (slot: next_node)."""
        node = self.get_current_node()
        return (
            node.format_sub_nodes(self.node_map)
            if node is not None
            else "暂无后续节点信息"
        )

    def format_jump_modules(self) -> str:
        """Format the jumpable module list as prompt-ready text (slot: jump_modules).

        Lists all modules in module_map except the current one (code + name +
        description) for the NLU prompt to reference when emitting the
        jump_module field. Boundaries are deliberately blurred: no adjacency
        graph is consulted — any target in module_map is a legal jump.
        """
        parts = []
        for code, module in self.module_map.items():
            if code == self.current_module_code:
                continue
            name = getattr(module, "module_name", "") or code
            desc = getattr(module, "module_description", "") or ""
            seg = f"- {code}（{name}）"
            if desc:
                seg += f"：{desc}"
            parts.append(seg)
        return "\n".join(parts) if parts else "暂无可跳转模块"

    def format_answer_pattern(self) -> str:
        """Format the current node's answer examples as prompt-ready text (slot: answer_pattern)."""
        node = self.get_current_node()
        return (
            node.format_answer_examples()
            if node is not None
            else "暂无回答范式"
        )

    def format_task_info(self) -> str:
        """Format the task info as prompt-ready text (slot: task_info).

        Reads ``task_basic_info`` first; falls back to ``metadata["task_info"]``
        (the key written by the launch layer from the dialogue request).
        """
        task_info = self.task_basic_info or self.metadata.get("task_info") or {}

        parts = []
        for key, value in task_info.items():
            parts.append(f"{key}: {value}")

        return "\n".join(parts) if parts else "暂无任务基础信息"


# ============================================================================
# Prompt slot plumbing — fixed slot vocabulary shared by all stages
# ============================================================================

# Fixed slot vocabulary: every stage's prompt template shares the same set of
# {__key__} placeholders. Concatenation logic lives in the layer that owns the
# data:
#   - node layer  : cur_node (stage-specific facet) / next_node / answer_pattern  (node.py)
#   - module layer: task_info                              (module.py)
#   - ctx layer   : query / query_rewrite / recall_info / history / filled_slots
# Stages (nlu / nlg / query / recaller) only map "slot name -> data-layer
# formatting method"; they no longer implement concatenation themselves.
#
# cur_node stage-facet conventions:
#   - nlu : name + todo_description + slots   — understanding task: judge intent, extract slots per template
#   - nlg : name + description                — generation task: scenario the reply is grounded in
#   - full: all fields                        — retrieval stages (query rewrite / recall)

def fill_prompt_template(template: str, slots: Dict[str, str]) -> str:
    """Replace ``{__key__}`` placeholders in the template with their values.

    Uses ``str.replace()`` one by one; keys absent from the template are safely ignored.
    """
    for key, value in slots.items():
        template = template.replace(f"{{__{key}__}}", value)
    return template


def resolve_prompt_template(
    ctx: DialogueContext,
    prompt_attr: str,
    default_template: Optional[str],
) -> Optional[str|None]:
    """Resolve a stage's prompt template by priority.

    Priority: node level > module level > *default_template*.

    Args:
        ctx: current dialogue context.
        prompt_attr: override attribute name on node/module (e.g. ``base_nlu_prompt``).
        default_template: fallback template (may be None to keep each consumer's built-in).
    """
    node = ctx.get_current_node()
    if node is not None:
        node_prompt = getattr(node, prompt_attr, None)
        if node_prompt:
            return node_prompt

    module = ctx.get_current_module()
    if module is not None:
        module_prompt = getattr(module, prompt_attr, None)
        if module_prompt:
            return module_prompt

    return default_template


