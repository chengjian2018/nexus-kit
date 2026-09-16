"""Node — the single dialogue unit of the two-layer model.

The former three-layer structure (Pattern → Module → Node) collapsed into
Pattern → Node: everything a module used to carry that still matters lives
here (``use_tools`` / prompt assets / stages overrides / plugins slots).
One field, two compile-time semantics selected by ``pattern.pattern_type``:

- ``sub_nodes``: FSM = the next_node legal-value set (state-machine
  transitions, advanced one node per turn); AGENT = the static graph's
  adjacency edges (conditional edges are expressed by the node executor's
  routing output ``next``, mapped back onto sub_nodes).
- ``slots``: business-slot definitions — **FSM only** (the Pattern
  constructor raises when an AGENT-pattern node declares slots; AGENT nodes
  keep business state in ``config`` free fields instead, outside the
  filled_slots lifecycle).

Prompt assets (base_prompt / base_nlu_prompt / base_nlg_prompt / llm
overrides etc.) ride ``config`` — the dict built from the ``config`` param
plus ``**kwargs`` (explicit kwargs win on the same key).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from nexus.model.plugins_field import normalize_plugins

logger = logging.getLogger(__name__)


class BaseNode:
    """Base class for dialogue nodes (the merged module+node unit).

    Attributes:
        code: unique node code.
        name: node name.
        description: scenario description — feeds the NLG cur_node facet.
        task_description: todo description — feeds the NLU cur_node facet
            (what this node is trying to understand/collect).
        sub_nodes: successor codes (FSM transition set / AGENT adjacency).
        answer_examples: answer-paradigm examples (prompt asset; consumed
            verbatim by rule executors — mainly an FSM/ROUTE-era asset, kept
            available for AGENT rule nodes).
        stages: pipeline slot config ``{slot_name: stage_code}`` — the node
            layer of the two-layer resolution (node > pattern skeleton);
            FSM patterns only.
        slots: business-slot definitions ``{slot_name: description}`` (FSM only).
        use_tools: tool names this node may call — **empty = no tools**
            (deny-by-default; intersected with pattern.allow_toolset).
        use_skills: skill names this node may load — **empty = no skills**
            (deny-by-default; intersected with pattern.allow_skills;
            scanned by nexus/skills.py — a data asset, no registry).
        is_end: terminal marker (FSM end node / AGENT graph terminal).
        plugins: unified plugin declarations (loop / messages_builder /
            agent_hooks — see nexus/model/plugins_field.py); the node
            layer over the pattern layer.
        config: free-form declaration bag (prompt assets etc.) — the single
            home of ``**kwargs`` extras.
    """

    def __init__(
        self,
        code: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        task_description: Optional[str] = None,
        sub_nodes: Optional[List[str]] = None,
        answer_examples: Optional[List[str]] = None,
        stages: Optional[Dict[str, str]] = None,
        slots: Optional[Dict[str, str]] = None,
        use_tools: Optional[List[str]] = None,
        use_skills: Optional[List[str]] = None,
        is_end: Optional[bool] = False,
        plugins: Optional[Dict[str, str]] = None,
        config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        self.code = code
        self.name = name
        self.description = description
        self.task_description = task_description
        self.sub_nodes = list(sub_nodes or [])
        self.answer_examples = list(answer_examples or [])

        # Pipeline slot config (node layer of node > pattern skeleton;
        # codes are strings resolved via the plugin registry — declarative,
        # no object references). FSM patterns only (validated by Pattern).
        self.stages = dict(stages) if stages else {}

        # Business-slot definitions (FSM only — Pattern fails fast on
        # AGENT-pattern nodes declaring slots)
        self.slots = dict(slots) if slots else {}

        # Tool allowlist: empty = NO tools (deny-by-default)
        self.use_tools = list(use_tools or [])

        # Skill allowlist: empty = NO skills (deny-by-default; intersected
        # with pattern.allow_skills — resolution lives in nexus/skills.py)
        self.use_skills = list(use_skills or [])

        self.is_end = bool(is_end)

        # Unified plugin declarations (node layer over pattern layer)
        self.plugins = normalize_plugins(plugins)

        # config single source: explicit param first, then **kwargs extras
        # (kwargs win on the same key — they are the explicit sugar)
        self.config: Dict[str, Any] = dict(config or {})
        self.config.update(kwargs)

    # ------------------------------------------------------------------
    # Prompt-context formatting — reusable by NLU / NLG / recall / rewrite stages
    # ------------------------------------------------------------------

    def get_prompt(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a prompt asset from config (base_prompt / base_nlu_prompt /
        base_nlg_prompt / ... — the pre-merge constructor params now live here)."""
        value = self.config.get(key)
        return value if value else default

    def to_prompt_text(self) -> str:
        """Format this node as full prompt-ready text (code / name / description / todo / slots)."""
        parts = []
        if self.code:
            parts.append(f"节点编码: {self.code}")
        if self.name:
            parts.append(f"节点名称: {self.name}")
        if self.description:
            parts.append(f"节点描述: {self.description}")
        if self.task_description:
            parts.append(f"代办描述: {self.task_description}")
        if self.slots:
            parts.append(f"槽位定义: {json.dumps(self.slots, ensure_ascii=False)}")
        return "\n".join(parts) if parts else "暂无当前节点信息"

    def to_nlu_prompt_text(self) -> str:
        """NLU-stage cur_node text: name + todo description + slot definitions."""
        parts = []
        if self.name:
            parts.append(f"节点名称: {self.name}")
        if self.task_description:
            parts.append(f"代办描述: {self.task_description}")
        if self.slots:
            parts.append(f"槽位定义: {json.dumps(self.slots, ensure_ascii=False)}")
        return "\n".join(parts) if parts else "暂无当前节点信息"

    def to_nlg_prompt_text(self) -> str:
        """NLG-stage cur_node text: name + node description."""
        parts = []
        if self.name:
            parts.append(f"节点名称: {self.name}")
        if self.description:
            parts.append(f"节点描述: {self.description}")
        return "\n".join(parts) if parts else "暂无当前节点信息"

    def format_slots(self) -> str:
        """Format this node's slot definitions as prompt-ready text."""
        if not self.slots:
            return "暂无槽位定义"
        return json.dumps(self.slots, ensure_ascii=False)

    def format_sub_nodes(self, node_map: Dict[str, "BaseNode"]) -> str:
        """Format the successor list as prompt-ready text (FSM transition
        targets / AGENT adjacency — both serve as NLU's next candidates)."""
        if not self.sub_nodes:
            return "暂无后续节点信息"

        parts = []
        for sub_code in self.sub_nodes:
            sub_node = node_map.get(sub_code)
            if sub_node is not None:
                desc = f"- {sub_code}"
                if sub_node.name:
                    desc += f": {sub_node.name}"
                if sub_node.description:
                    desc += f"（{sub_node.description}）"
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
        return f"<{type(self).__name__} code={self.code!r}>"
