"""plugins declaration field — the plugin declaration dict shared by
Pattern and BaseNode.

One ``plugins: Dict[str, str]`` per layer (pattern / node), the same dict
shape as stages: slot name → plugin code, purely declarative, serializable,
fail-fast at construction time. Values are **str/None only** — the
transitional inline-callable window is over (unregistered str codes raise at
validation time, never silently).

Slot table (slot name → resolution target):

=================  ===================  =================================
slot               target               meaning
=================  ===================  =================================
loop               executor            AGENT node executor (ReAct tool
                                        loop / custom / rule executor)
fsm                executor            FSM executor (pattern_type="fsm")
messages_builder   messages_builder    AGENT messages builder
agent_hooks        agent_hooks         agent loop hooks package
=================  ===================  =================================

Resolution chains (node over pattern, the two-layer successor of the old
module > pattern):

- executor (AGENT node): ``node.plugins["loop"]`` >
  ``pattern.plugins["loop"]`` > default_loop
- executor (FSM pattern): ``pattern.plugins["fsm"]`` > default_fsm
- messages_builder / agent_hooks: ``node.plugins[key]`` >
  ``pattern.plugins[key]`` > kernel default / empty passthrough

LLM selection is NOT a plugins slot: it lives in settings
(llm_default ⊕ app config ⊕ metadata override, see nexus.settings.
get_llm_config — the per-app config took over the old plugins["llm"]
declaration slot).
"""

from typing import Any, Dict, Optional

import logging

logger = logging.getLogger(__name__)

# slot name → plugin-registry kind (a new extension point is one line here).
PLUGIN_KINDS: Dict[str, str] = {
    "loop": "executor",
    "fsm": "executor",
    "messages_builder": "messages_builder",
    "agent_hooks": "agent_hooks",
}

# Legacy slots: removals after the app-config takeover. Unlike truly
# unknown slots, these appear in pattern YAMLs persisted before the upgrade
# — raising at construction would turn existing loadable files into broken
# ones. They get warn+dropped (one version cycle of migration grace); truly
# unknown slots stay fail-fast.
_RETIRED_PLUGIN_SLOTS: Dict[str, str] = {
    "llm": "模型选择已迁移到 apps/<name>/config.yaml 的 llm/nodes 段"
           "（nexus.settings.get_llm_config）",
}

# executor-family slots (mirror the pattern_type dispatch: loop drives AGENT
# graph nodes, fsm drives the FSM pipeline)
EXECUTOR_FAMILY_SLOTS = ("loop", "fsm")


def plugins_slot_label(slot: str) -> str:
    """Error label for a slot name: the executor family carries the legacy
    executor_<family> prefix, others use the slot name itself."""
    return f"executor_{slot}" if slot in EXECUTOR_FAMILY_SLOTS else slot


def normalize_plugins(plugins: Optional[Dict[str, Any]],
                      legacy: Optional[Dict[str, Any]] = None,
                      ) -> Dict[str, Any]:
    """Normalize a plugins declaration: merge legacy scalar params +
    structural fail-fast.

    Args:
        plugins: dict-form declaration (slot name → str code / None). On the
          same slot the dict value wins (the dict is the authoritative
          carrier).
        legacy: name-value pairs of the old scalar params (e.g. the Pattern
          constructor's agent_hooks); they only fill slots missing from the
          dict (None values are skipped).

    Raises:
        ValueError: unknown slot name, or a value that is not str/None.
    """
    merged: Dict[str, Any] = dict(plugins or {})
    for slot, value in (legacy or {}).items():
        if value is None:
            continue
        merged.setdefault(slot, value)

    retired = [slot for slot in merged if slot in _RETIRED_PLUGIN_SLOTS]
    for slot in retired:
        logger.warning(
            "plugins 槽位 '%s' 已废弃（%s），已从声明中剔除", slot,
            _RETIRED_PLUGIN_SLOTS[slot])
        del merged[slot]

    for slot, value in merged.items():
        if slot not in PLUGIN_KINDS:
            raise ValueError(
                f"plugins 槽位名非法: {slot!r}"
                f"（合法: {sorted(PLUGIN_KINDS)}）"
            )
        if value is None or isinstance(value, str):
            continue
        raise ValueError(
            f"plugins[{slot!r}] 的值必须是 str/None: {value!r}"
        )
    return merged
