"""Pattern serialization — to_dict/from_dict + to_yaml/from_yaml round-trip
(two-layer shape).

The declarative model (all fields are str/bool/list/dict) makes a pattern
fully serializable. The dict/yml shape mirrors the constructor kwargs;
``from_dict`` goes through the same construction path (normalization +
compile fail-fast), then validation (model/validation.py) is the caller's
duty — the host assembly and CLI wiring call validate_pattern after loading.

Nodes serialize inline (the complete field list per node dict); the YAML
form is exactly this dict shape. No node registry — cross-file node reuse
waits for a real consumer.
"""

from typing import Any, Dict, List

import yaml

from nexus.model.node import BaseNode
from nexus.model.pattern import DEFAULT_MAX_STEPS, Pattern

# Node fields serialized (constructor params; config/kwargs ride as one dict)
_NODE_FIELDS = [
    "code", "name", "description", "task_description",
    "sub_nodes", "answer_examples", "stages", "slots",
    "use_tools", "use_skills", "is_end", "plugins",
]

# Pattern scalar fields serialized (nodes/stages/plugins handled
# structurally; everything else lives inside config)
_PATTERN_FIELDS = [
    "code", "name", "description", "pattern_type",
    "entry_node_code", "allow_toolset", "allow_skills",
]

# config keys already emitted as top-level structural fields — excluded from
# the config snapshot to avoid double emission
_CFG_DEDUP_KEYS = {
    "pattern_type", "entry_node_code", "stages", "plugins",
    "allow_toolset", "allow_skills", "max_steps",
}


def node_to_dict(node: BaseNode) -> Dict[str, Any]:
    """Serialize a node into a declarative dict."""
    data: Dict[str, Any] = {}
    for field in _NODE_FIELDS:
        value = getattr(node, field, None)
        if value not in (None, [], {}, False):
            data[field] = value
    if node.config:
        data["config"] = node.config
    return data


def node_from_dict(data: Dict[str, Any]) -> BaseNode:
    """Build a node from a declarative dict (full construction path)."""
    return BaseNode(**dict(data))


def pattern_to_dict(pattern: Pattern) -> Dict[str, Any]:
    """Serialize a whole pattern (nodes inline) into a dict."""
    data: Dict[str, Any] = {}
    for field in _PATTERN_FIELDS:
        value = getattr(pattern, field, None)
        if value not in (None, [], {}, False):
            data[field] = value
    if pattern.stages:
        data["stages"] = pattern.stages
    if pattern.plugins:
        data["plugins"] = pattern.plugins
    if pattern.max_steps != DEFAULT_MAX_STEPS:
        data["max_steps"] = pattern.max_steps
    config = {k: v for k, v in (pattern.config or {}).items()
              if k not in _CFG_DEDUP_KEYS}
    if config:
        data["config"] = config
    data["nodes"] = [node_to_dict(n) for n in pattern.nodes]
    return data


def pattern_from_dict(data: Dict[str, Any]) -> Pattern:
    """Build a Pattern from a declarative dict (nodes inline dicts →
    BaseNode objects; full construction path — normalization + compile
    fail-fast run in the constructor)."""
    data = dict(data)  # never mutate the caller's dict
    node_data = data.pop("nodes", None) or []
    data["nodes"] = [n if isinstance(n, BaseNode) else node_from_dict(dict(n))
                     for n in node_data]
    return Pattern(**data)


# ---------------------------------------------------------------------------
# YAML round-trip (dict shape identical; safe_load keeps dict order on 3.7+)
# ---------------------------------------------------------------------------

def pattern_to_yaml(pattern: Pattern) -> str:
    """Serialize a pattern to a YAML string."""
    return yaml.safe_dump(pattern_to_dict(pattern), allow_unicode=True,
                          sort_keys=False, default_flow_style=False)


def pattern_from_yaml(text: str) -> Pattern:
    """Load a pattern from a YAML string (construction + caller-side
    validation, same as from_dict)."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"yaml 内容必须是 pattern 映射: {type(data).__name__}")
    return pattern_from_dict(data)
