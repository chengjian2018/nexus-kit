"""
Node — state node of FSM / Route modules.

Each node can independently configure NLU / NLG stages, falling back to module
level or default implementations when unset.
Nodes define the sub-node graph via sub_nodes to enable state-machine transitions.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BaseNode:
    """Base class for dialogue state nodes.

    Attributes:
        node_code: unique node code.
        node_name: node name.
        node_description: scenario description — feeds the NLG cur_node facet and
            the sub-node listing used for routing/intent matching.
        node_todo_description: todo description — feeds the NLU cur_node facet
            (what this node is trying to understand/collect).
        sub_nodes: list of sub-nodes (forms the state-machine transition graph).
        node_slots: slot definitions (prompt asset).
        answer_examples: answer-paradigm examples (prompt asset / marker).
        base_nlu_prompt: node-level NLU prompt template string.
        base_nlg_prompt: node-level NLG prompt template string.
        stages: pipeline slot config ``{slot_name: stage_code}`` — the node
            layer of the three-layer resolution (node > module > skeleton);
            stage codes are strings resolved from the plugin registry
            (kind="stage") at execution time.
    """

    def __init__(
        self,
        node_code: Optional[str] = None,
        node_name: Optional[str] = None,
        node_description: Optional[str] = None,
        node_todo_description: Optional[str] = None,
        sub_nodes: Optional[List[str]] = None,
        node_slots: Optional[dict[str, str]] = None,
        answer_examples: Optional[List[str]] = None,
        stages: Optional[Dict[str, str]] = None,
        base_nlu_prompt: Optional[str] = None,
        base_nlg_prompt: Optional[str] = None,
        is_end: Optional[bool] = False,
        **kwargs,
    ):
        self.node_code = node_code
        self.node_name = node_name
        self.node_description = node_description
        self.node_todo_description = node_todo_description
        self.sub_nodes = sub_nodes or []
        self.base_nlu_prompt = base_nlu_prompt
        self.base_nlg_prompt = base_nlg_prompt
        self.answer_examples = answer_examples

        # Pipeline slot config (node layer of node > module > skeleton;
        # codes are strings resolved via the plugin registry — declarative,
        # no object references)
        self.stages = dict(stages) if stages else {}

        self.node_slots = node_slots

        self.is_end = is_end

        for key, value in (kwargs or {}).items():
            setattr(self, key, value)

    # ------------------------------------------------------------------
    # Prompt context formatting — reusable by NLU / NLG / recall / rewrite stages
    # ------------------------------------------------------------------

    def to_prompt_text(self) -> str:
        """Format this node as full prompt-ready text (code / name / description / todo / slots).

        Used by retrieval-oriented stages (query rewrite / recall); NLU / NLG use
        their stage-specific variants below.
        """
        parts = []
        if self.node_code:
            parts.append(f"节点编码: {self.node_code}")
        if self.node_name:
            parts.append(f"节点名称: {self.node_name}")
        if self.node_description:
            parts.append(f"节点描述: {self.node_description}")
        if self.node_todo_description:
            parts.append(f"代办描述: {self.node_todo_description}")
        if self.node_slots:
            parts.append(
                f"槽位定义: {json.dumps(self.node_slots, ensure_ascii=False)}"
            )

        return "\n".join(parts) if parts else "暂无当前节点信息"

    def to_nlu_prompt_text(self) -> str:
        """NLU-stage cur_node text: name + todo description + slot definitions.

        NLU cares about "what this node is trying to collect/decide" — the todo
        description drives intent judgment and the slot templates drive extraction.
        """
        parts = []
        if self.node_name:
            parts.append(f"节点名称: {self.node_name}")
        if self.node_todo_description:
            parts.append(f"代办描述: {self.node_todo_description}")
        if self.node_slots:
            parts.append(
                f"槽位定义: {json.dumps(self.node_slots, ensure_ascii=False)}"
            )

        return "\n".join(parts) if parts else "暂无当前节点信息"

    def to_nlg_prompt_text(self) -> str:
        """NLG-stage cur_node text: name + node description.

        NLG cares about "what scenario this node is in" — the node description
        provides the grounding context for phrasing the reply.
        """
        parts = []
        if self.node_name:
            parts.append(f"节点名称: {self.node_name}")
        if self.node_description:
            parts.append(f"节点描述: {self.node_description}")

        return "\n".join(parts) if parts else "暂无当前节点信息"

    def format_slots(self) -> str:
        """Format this node's slot definitions as prompt-ready text (slot: node_slots)."""
        if not self.node_slots:
            return "暂无槽位定义"
        return json.dumps(self.node_slots, ensure_ascii=False)

    def format_sub_nodes(self, node_map: Dict[str, BaseNode]) -> str:
        """Format the sub-node list (state-machine transition targets) as prompt-ready text.

        ``node_map`` maps node_code → node instance, typically ``ctx.node_map``.
        """
        if not self.sub_nodes:
            return "暂无后续节点信息"

        parts = []
        for sub_code in self.sub_nodes:
            sub_node = node_map.get(sub_code)
            if sub_node is not None:
                desc = f"- {sub_code}"
                if sub_node.node_name:
                    desc += f": {sub_node.node_name}"
                if sub_node.node_description:
                    desc += f"（{sub_node.node_description}）"
                parts.append(desc)
            else:
                parts.append(f"- {sub_code}")

        return "\n".join(parts)

    def format_answer_examples(self) -> str:
        """Format the node's answer examples as prompt-ready text."""
        if not self.answer_examples:
            return "暂无回答范式"

        parts = []
        for i, example in enumerate(self.answer_examples, 1):
            parts.append(f"示例 {i}: {example}")

        return "\n".join(parts)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} code={self.node_code!r}>"