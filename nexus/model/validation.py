"""Pattern validation — base-info completeness + plugin-declaration
resolvability, collecting ALL errors before raising one numbered
ValueError (fail-fast timing stays at assembly/registration time; the report
upgrades from one-error-per-run to a full list).

Two entry points:

- validate_base_info(pattern): code/name/entry non-empty & resolvable,
  module_code uniqueness (today's module_map assignment silently
  overwrites), FSM/ROUTE modules have ≥1 node, node_code unique within its
  module. Missing display names are warnings (soft), everything else raises.
- validate_plugin_declarations(pattern): executor / stages / skeleton /
  messages_builder / agent_hooks string codes resolve in the plugin
  registry (checked via has() — no instantiation).

Callers: host/cli assembly (after discover_builtin_plugins warms the
registry) validate every registered pattern; yml loading (plan-③) validates
after construction. The graph checks (dangling edges / self-loops /
lend-tools authorization) already run in Pattern.__init__ and are not
duplicated here.
"""

import logging
from typing import List

from nexus.model.pattern import Pattern
from nexus.pipeline import normalize_skeleton
from nexus.registry.plugins import DEFAULT_EXECUTOR_CODES
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

# Slots that carry builtin defaults (nlu/nlg per module type) — a None value
# there is fine even without any declaration; other slots with None are
# optional by the universal skip rule, so None is never an error either.
# Resolution errors are only about *declared* codes that cannot resolve.


def _slot_codes_declared(stages) -> List[str]:
    """All non-None codes declared in a stages dict or skeleton."""
    codes = []
    if not stages:
        return codes
    if isinstance(stages, dict):
        return [v for v in stages.values() if v]
    for entry in stages:
        for code in entry.values():
            if code:
                codes.append(code)
    return codes


def validate_base_info(pattern: Pattern) -> List[str]:
    """Validate structural completeness; returns the collected error list
    (empty = valid). Missing display names log warnings but never raise."""
    errors: List[str] = []
    warnings: List[str] = []

    if not getattr(pattern, "code", None):
        errors.append("pattern.code 为空")
    if not getattr(pattern, "name", None):
        warnings.append(f"pattern {pattern.code!r} 缺少 name")
    if not getattr(pattern, "entry_module_code", None):
        errors.append(f"pattern {pattern.code!r} 缺少 entry_module_code")
    elif pattern.entry_module_code not in pattern.module_map:
        errors.append(
            f"pattern {pattern.code!r} 的 entry_module_code "
            f"{pattern.entry_module_code!r} 不在 modules 中"
        )

    seen_module_codes = set()
    for module in pattern.modules or []:
        code = module.module_code
        if not code:
            errors.append(f"存在 module_code 为空的模块（name="
                          f"{module.module_name!r}）")
            continue
        if code in seen_module_codes:
            errors.append(f"module_code 重复: {code!r}（后者覆盖前者）")
        seen_module_codes.add(code)

        if not getattr(module, "module_name", None):
            warnings.append(f"module {code!r} 缺少 module_name")

        # FSM/ROUTE need at least one node
        from nexus.model.module import ModuleType
        if module.type in (ModuleType.FSM, ModuleType.ROUTE):
            if not module.module_nodes:
                errors.append(
                    f"module {code!r}（{module.type.value}）没有任何节点")

        # node_code unique within the module
        seen_node_codes = set()
        for node in module.module_nodes:
            node_code = node.node_code
            if not node_code:
                errors.append(f"module {code!r} 存在 node_code 为空的节点")
                continue
            if node_code in seen_node_codes:
                errors.append(
                    f"module {code!r} 内 node_code 重复: {node_code!r}")
            seen_node_codes.add(node_code)

    for w in warnings:
        logger.warning("[validation] %s（软警告）", w)
    return errors


def validate_plugin_declarations(pattern: Pattern) -> List[str]:
    """Validate that all declared plugin codes resolve; returns the error
    list (empty = valid)."""
    errors: List[str] = []
    pcode = getattr(pattern, "code", "?")

    # Executors
    for field, default_code in (
        ("executor_loop", DEFAULT_EXECUTOR_CODES["agent"]),
        ("executor_fsm", DEFAULT_EXECUTOR_CODES["fsm"]),
        ("executor_route", DEFAULT_EXECUTOR_CODES["route"]),
    ):
        declared = getattr(pattern, field, None)
        if declared and not plugin_registry.has("executor", declared):
            errors.append(
                f"pattern {pcode!r} 的 {field}={declared!r} 未注册"
                f"（kind=executor）")
        del default_code

    skeleton_slot_names: List[str] = []
    try:
        skeleton = normalize_skeleton(getattr(pattern, "stages", None))
        skeleton_slot_names = [slot for entry in skeleton
                               for slot in entry.keys()]
    except ValueError as e:
        errors.append(f"pattern {pcode!r} 骨架声明非法: {e}")
        skeleton = []

    # Skeleton + per-module/node stages codes resolve & slots belong to the
    # skeleton; the unified pair (nlu/nlg sharing a code) is the only legal
    # duplicate
    for entry in skeleton:
        for slot, code in entry.items():
            if code and not plugin_registry.has("stage", code):
                errors.append(
                    f"pattern {pcode!r} 骨架槽位 {slot} 声明的 {code!r} "
                    f"未注册（kind=stage）")

    for module in pattern.modules or []:
        mcode = module.module_code
        declared = getattr(module, "executor", None)
        if declared and not plugin_registry.has("executor", declared):
            errors.append(
                f"module {mcode!r} 的 executor={declared!r} 未注册"
                f"（kind=executor）")

        stages = getattr(module, "stages", None) or {}
        if not isinstance(stages, dict):
            errors.append(f"module {mcode!r} 的 stages 必须是 dict: {stages!r}")
        else:
            for slot, code in stages.items():
                if skeleton_slot_names and slot not in skeleton_slot_names:
                    errors.append(
                        f"module {mcode!r} 的 stages 声明了骨架不存在的槽位 "
                        f"{slot!r}（骨架: {skeleton_slot_names}）")
                if code and not plugin_registry.has("stage", code):
                    errors.append(
                        f"module {mcode!r} 的 stages[{slot}]={code!r} 未注册"
                        f"（kind=stage）")

        builder = getattr(module, "messages_builder", None)
        if isinstance(builder, str) and not plugin_registry.has(
                "messages_builder", builder):
            errors.append(
                f"module {mcode!r} 的 messages_builder={builder!r} 未注册"
                f"（kind=messages_builder）")
        hooks = getattr(module, "agent_hooks", None)
        if isinstance(hooks, str) and not plugin_registry.has(
                "agent_hooks", hooks):
            errors.append(
                f"module {mcode!r} 的 agent_hooks={hooks!r} 未注册"
                f"（kind=agent_hooks）")

        for node in module.module_nodes:
            ncode = node.node_code
            nstages = getattr(node, "stages", None) or {}
            if not isinstance(nstages, dict):
                errors.append(
                    f"node {ncode!r} 的 stages 必须是 dict: {nstages!r}")
            else:
                for slot, code in nstages.items():
                    if skeleton_slot_names and slot not in skeleton_slot_names:
                        errors.append(
                            f"node {ncode!r} 的 stages 声明了骨架不存在的"
                            f"槽位 {slot!r}")
                    if code and not plugin_registry.has("stage", code):
                        errors.append(
                            f"node {ncode!r} 的 stages[{slot}]={code!r} "
                            f"未注册（kind=stage）")

    # Plugin-code uniqueness across the resolved slots (the nlu/nlg pair
    # sharing one code — the unified form — is the only legal duplicate)
    slots_by_code: dict = {}
    stages_root = getattr(pattern, "stages", None)
    if isinstance(stages_root, list):
        for entry in stages_root:
            for slot, code in entry.items():
                if code:
                    slots_by_code.setdefault(code, []).append(slot)
    for module in pattern.modules or []:
        for slot, code in (getattr(module, "stages", None) or {}).items():
            if code:
                slots_by_code.setdefault(code, []).append(slot)
        for node in module.module_nodes:
            for slot, code in (getattr(node, "stages", None) or {}).items():
                if code:
                    slots_by_code.setdefault(code, []).append(slot)
    for code, slots in slots_by_code.items():
        if len(slots) > 1 and set(slots) - {"nlu", "nlg"}:
            errors.append(
                f"stage code {code!r} 在多个槽位声明（{sorted(set(slots))}；"
                f"仅 nlu/nlg 同 code 的 unified 形态允许，其余为声明错误）")

    return errors


def validate_pattern(pattern: Pattern) -> None:
    """Full validation: base info + plugin declarations; collects ALL errors
    then raises one numbered ValueError (empty list = valid, silent return).
    """
    errors = validate_base_info(pattern) + validate_plugin_declarations(pattern)
    if errors:
        numbered = "\n".join(f"  [{i + 1}] {e}" for i, e in enumerate(errors))
        raise ValueError(
            f"pattern {getattr(pattern, 'code', '?')!r} 校验失败"
            f"（共 {len(errors)} 项）:\n{numbered}"
        )
