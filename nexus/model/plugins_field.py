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
loop               executor            AGENT 节点执行器（ReAct 工具循环 /
                                        自定义 / 规则 executor）
fsm                executor            FSM 执行器（pattern_type="fsm"）
messages_builder   messages_builder    AGENT 消息构建器
agent_hooks        agent_hooks         agent 循环 hooks 包
llm                llm_providers code  LLM provider code（settings 解析，
                                        声明式只存 code，不内联密钥）
=================  ===================  =================================

Resolution chains (node over pattern, the two-layer successor of the old
module > pattern):

- executor（AGENT 节点）: ``node.plugins["loop"]`` >
  ``pattern.plugins["loop"]`` > default_loop
- executor（FSM pattern）: ``pattern.plugins["fsm"]`` > default_fsm
- messages_builder / agent_hooks: ``node.plugins[key]`` >
  ``pattern.plugins[key]`` > kernel default / empty passthrough
- llm: ``node.plugins["llm"]`` > ``pattern.plugins["llm"]`` > settings 的
  pattern_llm 分层解析（见 nexus.settings.get_llm_config）
"""

from typing import Any, Dict, Optional

# slot name → plugin-registry kind (a new extension point is one line here).
# "llm" has no registry kind — its value is an llm_providers code resolved
# by nexus.settings at refresh time (R1/R3/R4), not a plugin instance.
PLUGIN_KINDS: Dict[str, str] = {
    "loop": "executor",
    "fsm": "executor",
    "messages_builder": "messages_builder",
    "agent_hooks": "agent_hooks",
    "llm": "",  # settings-resolved (llm_providers code)
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
