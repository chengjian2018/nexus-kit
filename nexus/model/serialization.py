"""Pattern serialization — to_dict/from_dict + to_yaml/from_yaml round-trip.

The plan-② declarative model (all fields are str/bool/list/dict) makes a
pattern fully serializable. The dict/yml shape mirrors the constructor
kwargs; from_dict goes through the same construction path (normalization +
graph fail-fast), then validation (model/validation.py) is the caller's
duty — the host assembly and CLI wiring call validate_pattern after
loading, mirroring the python-declared patterns' registration-time checks.

Non-goals: hot reload / file watching (explicitly out of plan-③ scope).
"""

from typing import Any, Dict, List, Optional

import yaml

from nexus.model.module import BaseModule
from nexus.model.node import BaseNode
from nexus.model.pattern import Pattern

# Module fields serialized (constructor params; kwargs extras ride along).
# messages_builder / agent_hooks ride inside the unified plugins dict (the
# legacy scalar params still load — constructors fold them in).
_MODULE_FIELDS = [
    "module_code", "module_name", "module_description",
    "module_todo_description", "use_tools", "base_prompt",
    "base_nlu_prompt", "base_nlg_prompt", "stages", "sub_modules",
    "executor", "enable_project", "agent_stage", "plugins",
    "is_end", "answer_examples",
]

# Node fields serialized
_NODE_FIELDS = [
    "node_code", "node_name", "node_description", "node_todo_description",
    "sub_nodes", "node_slots", "answer_examples", "stages",
    "base_nlu_prompt", "base_nlg_prompt", "is_end",
]

# Pattern scalar fields serialized (modules/stages handled structurally).
# The executor family + messages_builder + agent_hooks ride inside the
# unified plugins dict (legacy scalar params still load — the constructor
# folds them in).
_PATTERN_FIELDS = [
    "code", "name", "description", "entry_module_code", "plugins",
    "max_hops",
]


def module_to_dict(module: BaseModule) -> Dict[str, Any]:
    """Serialize a module (with its nodes) into a declarative dict."""
    data: Dict[str, Any] = {"type": module.type.value}
    for field in _MODULE_FIELDS:
        value = getattr(module, field, None)
        if value not in (None, [], {}):
            data[field] = value
    if module.module_nodes:
        data["nodes"] = [node_to_dict(n) for n in module.module_nodes]
    return data


def node_to_dict(node: BaseNode) -> Dict[str, Any]:
    """Serialize a node into a declarative dict."""
    data: Dict[str, Any] = {}
    for field in _NODE_FIELDS:
        value = getattr(node, field, None)
        if value not in (None, [], {}):
            data[field] = value
    # jump_module is an optional extra attribute (not a constructor param of
    # BaseNode — it rides kwargs); serialize when present
    jump = getattr(node, "jump_module", None)
    if jump:
        data["jump_module"] = jump
    return data


def pattern_to_dict(pattern: Pattern) -> Dict[str, Any]:
    """Serialize a whole pattern (modules + nodes tree) into a dict."""
    data: Dict[str, Any] = {}
    for field in _PATTERN_FIELDS:
        value = getattr(pattern, field, None)
        if value not in (None, [], {}):
            data[field] = value
    data["stages"] = pattern.stages or []
    data["modules"] = [module_to_dict(m) for m in (pattern.modules or [])]
    return data


def _module_from_dict(data: Dict[str, Any]) -> BaseModule:
    """Build a module (with nodes) from a declarative dict."""
    from nexus.model.module import AgentModule, FSMModule, RouteModule

    type_value = data.pop("type", "agent")
    classes = {"agent": AgentModule, "fsm": FSMModule, "route": RouteModule}
    cls = classes.get(type_value)
    if cls is None:
        raise ValueError(f"未知 module type: {type_value!r}（合法: agent/fsm/route）")

    node_data = data.pop("nodes", None) or []
    nodes = [BaseNode(**{k: v for k, v in nd.items()})
             for nd in node_data]
    data["module_nodes"] = nodes
    return cls(**data)


def pattern_from_dict(data: Dict[str, Any]) -> Pattern:
    """Build a Pattern from a declarative dict (full construction path —
    normalization + graph fail-fast run in the constructor)."""
    data = dict(data)  # never mutate the caller's dict
    module_data = data.pop("modules", None) or []
    data["modules"] = [_module_from_dict(dict(md)) for md in module_data]
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
