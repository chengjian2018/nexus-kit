"""
Dialogue system base types — PipelineStage, SessionMessage, DialogueContext

All stages and modules depend on these standard types to keep session storage
and context passing consistent.
"""

from __future__ import annotations

import inspect
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)



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
    replay guard in nexus/engine/messages.py) — no new fields/table columns added;
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

    # Cumulative count of message_sink write failures (per process lifetime).
    # A missed row makes DB < memory permanently (no backfill path exists);
    # compression checks this counter when it abandons on DB/memory mismatch,
    # and the turn-end snapshot logs it — the silent-degradation path must
    # stay observable (audit finding M-1).
    sink_failure_count: int = 0

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
    llm_config: Optional[Dict[str, Any]] = None

    # AGENT graph state board (plan-⑧): turn-scoped workflow data + the
    # suspension cursor. Reserved keys (engine-managed):
    #   "__paused_node__"     : code of the node whose executor returned
    #                           wait_human — the next user message resumes there
    #   "__step__"            : step counter surviving a suspension (max_steps
    #                           budget accounting across turns)
    #   "__fanout_results__"  : plan-⑨ fan-out results board — rebuilt
    #                           (overwritten) on every fanout_start; the join
    #                           node reads entries {branch_id, node_code, ok,
    #                           content, extra, error?} in completion order
    # Everything else is free for node executors to stash workflow data
    # (subtask lists, intermediate findings, approval records). The board is
    # cleared when the graph terminates; while paused it persists (sessions
    # table graph_state column — process-restart safe).
    graph_state: Dict[str, Any] = field(default_factory=dict)

    # Actions reserved for this turn (e.g. sends / transitions / external calls the reply should
    # trigger besides the text). Stages/handlers may append; the chat layer snapshots per turn
    # (see TurnLifecycle in nexus/engine/context_lifecycle.py — per-turn reset).
    # Dict-form actions are snapshotted verbatim into ChatResult.actions.
    actions: List[Any] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def add_message(
        self,
        role: str,
        content: str,
        stage: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a standardized message to history (async since the asyncio
        rewrite: the message_sink is a coroutine — the store persists each
        message on its aiosqlite worker).

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
                result = self.message_sink(msg)
                if inspect.iscoroutine(result):
                    await result
            except Exception:
                self.sink_failure_count += 1
                logger.exception(
                    "message_sink 写入失败（不影响对话；DB 将落后于内存，压缩会因"
                    "数量不齐而放弃）: session=%s role=%s stage=%s 累计失败=%d",
                    self.session_id, msg.role, msg.stage,
                    self.sink_failure_count,
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

    Priority: node.config[prompt_attr] > *default_template* (the pre-merge
    node/module attribute layers collapsed into the node's config bag).

    Args:
        ctx: current dialogue context.
        prompt_attr: prompt key in the node's config (e.g. ``base_nlu_prompt``).
        default_template: fallback template (may be None to keep each consumer's built-in).
    """
    node = ctx.get_current_node()
    if node is not None:
        node_prompt = node.get_prompt(prompt_attr)
        if node_prompt:
            return node_prompt

    return default_template


