"""Pattern — the top layer of the two-layer model.

``pattern_type`` is the engine's dispatch key:

- ``"fsm"``   : the FSM executor pulls the stages pipeline (two-layer
  resolution node > pattern skeleton) and advances **one node per turn**
  via next_node (cycles are natural semantics, no budget).
- ``"agent"`` : the graph runtime runs the **whole graph per user message**
  from entry (conditional edges = node executors' routing output ``next``,
  step budget ``config.max_steps``, wait_human suspension/resumption).
  ROUTE-era apps (intent menu + turn-end reset to root) are expressed as
  an AGENT graph: a root routing node + conditional edges.

Hard cut (no compat layer): ``modules`` / ``entry_module_code`` /
``max_hops`` and the whole module layer are gone — whatever still matters
lives on the Pattern and its nodes.

``config`` is the single source of truth: the explicit constructor params
(stages / plugins / agent_hooks / allow_toolset / pattern_type /
entry_node_code / max_steps) are sugar folded into it at construction —
when both are given for the same key, the explicit param wins.
"""

import logging
from typing import Any, Dict, List, Optional

from nexus.model.node import BaseNode
from nexus.model.plugins_field import normalize_plugins
from nexus.pipeline import normalize_skeleton

logger = logging.getLogger(__name__)

# Legal pattern_type values (default AGENT: a single-node chat graph is the
# most common minimal app)
PATTERN_TYPES = ("fsm", "agent")
DEFAULT_PATTERN_TYPE = "agent"

# AGENT graph step budget default (config.max_steps; one node execution per
# step — aligns with the global loop.max_tool_rounds guard scale)
DEFAULT_MAX_STEPS = 10

# Runtime fan-out width default (config.max_fanout — the per-dimension
# guard bounding one sends' worker-instance count)
DEFAULT_MAX_FANOUT = 8


class Pattern:
    def __init__(self,
                 code,
                 name: str,
                 description: str,
                 pattern_type: Optional[str] = None,
                 entry_node_code: Optional[str] = None,
                 nodes: Optional[List[Any]] = None,
                 stages: Optional[List[Dict[str, str]]] = None,
                 plugins: Optional[Dict[str, str]] = None,
                 agent_hooks: Optional[str] = None,
                 allow_toolset: Optional[List[str]] = None,
                 allow_skills: Optional[List[str]] = None,
                 config: Optional[Dict[str, Any]] = None,
                 **kwargs):
        self.code = code
        self.name = name
        self.description = description

        # ------------------------------------------------------------------
        # config as the single source of truth: explicit params are sugar
        # (explicit wins on the same key); other **kwargs free fields
        # (prompt assets etc.) go into config as well
        # ------------------------------------------------------------------
        cfg: Dict[str, Any] = dict(config or {})
        cfg.update(kwargs)

        # ------------------------------------------------------------------
        # pattern_type (the compile-time-validated dispatch key)
        # ------------------------------------------------------------------
        ptype = pattern_type if pattern_type is not None else cfg.get("pattern_type")
        if ptype is None:
            ptype = DEFAULT_PATTERN_TYPE
        if not isinstance(ptype, str) or ptype not in PATTERN_TYPES:
            raise ValueError(
                f"pattern_type 非法: {ptype!r}（合法: {PATTERN_TYPES}，"
                f"缺省 {DEFAULT_PATTERN_TYPE!r}）"
            )
        self.pattern_type = ptype
        cfg["pattern_type"] = ptype

        # ------------------------------------------------------------------
        # nodes: a list of objects (the Python declaration main path) or
        # dicts (the YAML shape, constructed in place); empty = auto-create
        # one default node code=pattern.code (the minimal single-node AGENT
        # graph)
        # ------------------------------------------------------------------
        node_list: List[BaseNode] = []
        for item in (nodes if nodes is not None else []):
            if isinstance(item, BaseNode):
                node_list.append(item)
            elif isinstance(item, dict):
                node_list.append(BaseNode(**dict(item)))
            else:
                raise ValueError(
                    f"nodes 元素必须是 BaseNode 或 dict: {item!r}"
                )
        if not node_list:
            node_list = [BaseNode(code=code, name=name,
                                  description=description)]

        self.node_map: Dict[str, BaseNode] = {}
        for node in node_list:
            if not node.code:
                raise ValueError(
                    f"存在 code 为空的节点（name={node.name!r}）"
                )
            if node.code in self.node_map:
                raise ValueError(f"节点 code 重复: {node.code!r}")
            self.node_map[node.code] = node
        self.nodes: List[BaseNode] = node_list

        # ------------------------------------------------------------------
        # Compile-time graph validation (fail fast, per the repo's
        # construction-time validation tradition)
        # ------------------------------------------------------------------
        for node in self.nodes:
            for target in node.sub_nodes:
                if target not in self.node_map:
                    raise ValueError(
                        f"悬空边: 节点 {node.code!r}.sub_nodes → {target!r}"
                        f"（目标不在 nodes 中）"
                    )
        if ptype == "agent":
            for node in self.nodes:
                if node.slots:
                    raise ValueError(
                        f"AGENT pattern 的节点 {node.code!r} 声明了 slots"
                        f"（仅 FSM pattern 可用；AGENT 节点的业务状态走 config）"
                    )
            declared_stages = stages if stages is not None else cfg.get("stages")
            if declared_stages:
                raise ValueError(
                    f"pattern {code!r}（agent）声明了 stages——stages 管线"
                    f"仅 FSM pattern 可用，AGENT 节点行为走 plugins"
                )

        # ------------------------------------------------------------------
        # stages skeleton (FSM only; agent is always empty)
        # ------------------------------------------------------------------
        if ptype == "fsm":
            declared_stages = stages if stages is not None else cfg.get("stages")
            self.stages = normalize_skeleton(declared_stages)
        else:
            self.stages = []
        cfg["stages"] = self.stages

        # ------------------------------------------------------------------
        # plugins (slot table loop/fsm/messages_builder/agent_hooks, values
        # narrowed to str/None; the top-level agent_hooks param is sugar)
        # ------------------------------------------------------------------
        declared_plugins = plugins if plugins is not None else cfg.get("plugins")
        self.plugins = normalize_plugins(
            declared_plugins,
            legacy={"agent_hooks": agent_hooks},
        )
        cfg["plugins"] = self.plugins

        # ------------------------------------------------------------------
        # allow_toolset (toolset-level authorization: empty = no toolsets at
        # all; see the three-layer tool authorization)
        # ------------------------------------------------------------------
        declared_toolsets = (allow_toolset if allow_toolset is not None
                             else cfg.get("allow_toolset"))
        self.allow_toolset = list(declared_toolsets or [])
        cfg["allow_toolset"] = self.allow_toolset

        # ------------------------------------------------------------------
        # allow_skills (skill-asset-level authorization: empty = no skills
        # at all; node.use_skills narrows on top of it; scanning and
        # resolution live in nexus/skills.py — a data asset, no registry)
        # ------------------------------------------------------------------
        declared_skills = (allow_skills if allow_skills is not None
                           else cfg.get("allow_skills"))
        self.allow_skills = list(declared_skills or [])
        cfg["allow_skills"] = self.allow_skills

        # ------------------------------------------------------------------
        # entry_node_code: empty = nodes[0].code
        # ------------------------------------------------------------------
        entry = entry_node_code if entry_node_code is not None else cfg.get("entry_node_code")
        if entry is None:
            entry = self.nodes[0].code
        if entry not in self.node_map:
            raise ValueError(
                f"entry_node_code {entry!r} 不在 nodes 中"
                f"（可用: {sorted(self.node_map)}）"
            )
        self.entry_node_code = entry
        cfg["entry_node_code"] = entry

        # max_steps pinned into config (default DEFAULT_MAX_STEPS; AGENT
        # graph step budget)
        if not isinstance(cfg.get("max_steps"), int):
            cfg["max_steps"] = DEFAULT_MAX_STEPS

        # max_fanout pinned into config (default DEFAULT_MAX_FANOUT;
        # runtime fan-out width budget — the instance-count cap of one
        # sends, the width dimension of the max_steps / max_fanout /
        # executor-internal-rounds three-layer guards)
        if not isinstance(cfg.get("max_fanout"), int) or cfg["max_fanout"] < 1:
            cfg["max_fanout"] = DEFAULT_MAX_FANOUT

        self.config = cfg

    # ------------------------------------------------------------------
    # Convenience reads over the config single source
    # ------------------------------------------------------------------

    @property
    def max_steps(self) -> int:
        """AGENT graph step budget (FSM never consumes it — exactly one node per turn)."""
        return int(self.config.get("max_steps", DEFAULT_MAX_STEPS))

    @property
    def max_fanout(self) -> int:
        """Runtime fan-out width cap (homogeneous instance count of one sends)."""
        return int(self.config.get("max_fanout", DEFAULT_MAX_FANOUT))

    @property
    def agent_hooks(self):
        return self.plugins.get("agent_hooks")

    @agent_hooks.setter
    def agent_hooks(self, value) -> None:
        self.plugins["agent_hooks"] = value
        self.config["plugins"] = self.plugins

    def __repr__(self) -> str:
        return (f"<Pattern code={self.code!r} "
                f"type={self.pattern_type!r} nodes={len(self.nodes)}>")
