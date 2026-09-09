"""Pipeline slots — the four-slot axis pre_recall / query / post_recall / generate,
with three-layer lazy resolution at execution time (node > module > pattern)
plus validated fallback.

pattern.stages (or the default skeleton) declares the pipeline **shape**:
- Concrete stages: executed verbatim (an explicit choice by the author; they
  can compose arbitrary shapes such as no NLU, unified single-call, etc.; no
  validation, no substitution)
- Slots: when execution reaches that position, the runner calls resolve_stage

The two forms of generate and its lazy sub-parts (core design):
- Config forms: a single stage (e.g. unified, one call that writes both
  nlu_result/nlg_result itself) or a dict {"nlu": s1, "nlg": s2} (exactly the
  two keys with valid values; missing either invalidates the whole layer)
- GenerateSlot does not bind a concrete stage at resolution time; instead it
  expands into structure:
    ROUTE              → [nlu part,                  nlg part]
    FSM+enable_clarify → [nlu part, ClarifyStage,    nlg part]
    FSM default        → [nlu part,                  nlg part]
  The two sub-parts each do three-layer resolution **at their own execution
  moment** — under ROUTE, the chat layer's jump detection
  (chat._detect_jump_after_stage) advances the menu node and refreshes
  node-level LLM config after the nlu part, and the nlg part resolves after
  the node switch, so menu-node-level nlg takes effect the same turn (timing
  fix); under FSM, ClarifyStage sets metadata["clarify"] before NLG runs, so
  clarify semantics are unchanged.
  In single form only the nlu part executes it, once; the nlg part is always
  a no-op for single:
  - root single + menu dict: the nlg part re-resolves to the menu dict, and
    the root's single already ran in the nlu part — the nlg part normally
    executes the menu nlg (menu dict takes effect the same turn);
  - root dict + menu single: the menu single does not execute this turn (the
    nlg part is a no-op); it only takes effect on the next turn when the nlu
    part resolves at the menu node.

Fallback rules (uniform across all slots): an invalid layer config → warning
+ fall back to the next layer; all three layers empty/invalid → recall/rewrite
slots become no-ops (empty list), generate falls back to builtin (by
module.type: FSMNLU/FSMNLG or RouteNLU/RouteNLG).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from nexus.context import DialogueContext, PipelineStage
from nexus.model.module import ModuleType

logger = logging.getLogger(__name__)


# ============================================================================
# Builtin default-stage hooks (registered by atoms, never imported)
# ============================================================================

# The kernel must not depend on atom implementations: the builtin generate /
# clarify fallbacks are registered at import time by ``atoms.stages`` (the
# host and tests/conftest warm it up), keeping the layering one-directional.
# Storage goes through the plugin registry (kind="stage_factory", keyed by
# module type / "clarify") — the public register_default_* API is unchanged.
from nexus.registry.plugins import registry as _plugin_registry


def register_default_generate(module_type: Any,
                              factory: Callable[[], Tuple[Any, Any]]) -> None:
    """Register the builtin ``(nlu, nlg)`` fallback pair for a module type."""
    _plugin_registry.register(
        "stage_factory", f"generate:{module_type}", factory)


def register_default_clarify(factory: Callable[[], Any]) -> None:
    """Register the builtin clarify-stage factory (FSM ``enable_clarify`` fallback)."""
    _plugin_registry.register("stage_factory", "clarify", factory)


def _default_generate_factories_get(module_type: Any) -> Optional[Callable]:
    code = f"generate:{module_type}"
    if _plugin_registry.has("stage_factory", code):
        return lambda: _plugin_registry.resolve("stage_factory", code)
    return None


def _builtin_clarify():
    if not _plugin_registry.has("stage_factory", "clarify"):
        raise RuntimeError(
            "未注册内置 clarify 兜底 stage：请 import atoms.stages（宿主/测试的"
            "装配阶段会做）或在 module 上显式配置 clarify_stage"
        )
    return _plugin_registry.resolve("stage_factory", "clarify")


# ============================================================================
# Slot sentinels
# ============================================================================

class StageSlot(PipelineStage):
    """Slot base class: placeholder marker, implements no execution logic.

    Calling execute directly (without going through the runner's resolution)
    raises immediately — fail fast against a skeleton being mistakenly run
    bare as a concrete pipeline (e.g. visualize / custom runners).
    """

    stage_name = "stage_slot"

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        raise NotImplementedError(
            f"{type(self).__name__} 是管线骨架槽位，必须经 "
            "resolve_stage() 解析为具体 stage 后执行"
        )


class PreRecallSlot(StageSlot):
    """Pre-recall slot: three-layer attribute name ``pre_recall``."""

    stage_name = "pre_recall_slot"


class QuerySlot(StageSlot):
    """Query rewrite slot: three-layer attribute name ``query``."""

    stage_name = "query_slot"


class PostRecallSlot(StageSlot):
    """Post-recall slot: three-layer attribute name ``post_recall``."""

    stage_name = "post_recall_slot"


class GenerateSlot(StageSlot):
    """Generate slot: three-layer attribute name ``generate``, single/dict dual form (see the module docstring)."""

    stage_name = "generate_slot"


# ============================================================================
# Validation and normalization
# ============================================================================

def is_valid_stage(obj: Any) -> bool:
    """Duck-typed stage validation: has a callable ``execute``."""
    return (
        obj is not None
        and hasattr(obj, "execute")
        and callable(obj.execute)
    )


def normalize_generate(value: Any) -> Optional[Tuple[str, Any, Any]]:
    """Normalize a ``generate`` config value into a classified tuple; None if invalid.

    Returns:
        ("dict", nlu, nlg) — dict with exactly the nlu/nlg keys, both values valid
        ("single", stage, None) — single-stage form (unified etc.)
        None — missing/extra keys, invalid values, or wrong types
    """
    if isinstance(value, dict):
        if set(value.keys()) != {"nlu", "nlg"}:
            return None
        nlu, nlg = value["nlu"], value["nlg"]
        if is_valid_stage(nlu) and is_valid_stage(nlg):
            return ("dict", nlu, nlg)
        return None
    if is_valid_stage(value):
        return ("single", value, None)
    return None


# ============================================================================
# Three-layer config lookup (node > module > pattern)
# ============================================================================

def _layered_values(attr: str, ctx: DialogueContext, module: Any,
                    pattern: Any) -> List[Tuple[str, Any]]:
    """Collect configured layers as (layer name, raw value) in node > module > pattern order."""
    layers: List[Tuple[str, Any]] = []
    node = ctx.get_current_node()
    if node is not None:
        layers.append(("node", getattr(node, attr, None)))
    layers.append(("module", getattr(module, attr, None)))
    if pattern is not None:
        layers.append(("pattern", getattr(pattern, attr, None)))
    return [(name, v) for name, v in layers if v is not None]


def _resolve_generate(ctx: DialogueContext, module: Any,
                      pattern: Any) -> Tuple[str, Any, Any]:
    """Three-layer generate resolution (with whole-layer fallback); all empty/invalid → the builtin classified tuple."""
    for layer_name, value in _layered_values("generate", ctx, module, pattern):
        normalized = normalize_generate(value)
        if normalized is not None:
            return normalized
        logger.warning(
            "[stage_slots] %s 层 generate 配置非法（dict 须恰含 nlu/nlg 两键"
            "且值合法，或为带 execute 的单 stage），整层降级: %r",
            layer_name, value,
        )
    # Builtin fallback (by module.type) — factory registered by atoms.stages
    factory = _default_generate_factories_get(getattr(module, "type", None))
    if factory is None:
        raise RuntimeError(
            "未注册该 module type 的内置 generate 兜底 stage：请 import "
            "atoms.stages（宿主/测试的装配阶段会做），或在 node/module/pattern "
            "任一层显式配置 generate"
        )
    nlu, nlg = factory()
    return ("dict", nlu, nlg)


# ============================================================================
# generate lazy sub-parts
# ============================================================================

class _GenerateNLUPart(PipelineStage):
    """The nlu part of generate: three-layer resolution at execution time; dict takes nlu / single executes the whole stage."""

    stage_name = "generate_nlu_part"

    def __init__(self, module: Any, pattern: Any):
        self.module = module
        self.pattern = pattern

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        _kind, nlu, _nlg = _resolve_generate(ctx, self.module, self.pattern)
        # In single form the nlu slot holds the entire stage (the nlg slot is None)
        return nlu.execute(ctx)


class _GenerateNLGPart(PipelineStage):
    """The nlg part of generate: re-resolves the three layers (the node may
    have switched to a menu node by then).

    dict form → executes nlg; single form → no-op (single already ran whole in
    the nlu part; unified writes nlg_result itself, and a menu-node-level
    single takes effect the next turn).
    """

    stage_name = "generate_nlg_part"

    def __init__(self, module: Any, pattern: Any):
        self.module = module
        self.pattern = pattern

    def execute(self, ctx: DialogueContext) -> DialogueContext:
        kind, _nlu, nlg = _resolve_generate(ctx, self.module, self.pattern)
        if kind == "single":
            return ctx
        return nlg.execute(ctx)



# ============================================================================
# Slot resolution entry point
# ============================================================================

def resolve_stage(stage: Any, ctx: DialogueContext, module: Any,
                  pattern: Any = None) -> List[Any]:
    """Return the list of stages to execute: slots resolved lazily, non-slots
    passed through as-is as ``[stage]``.

    - GenerateSlot → structural list (the nlg part resolves independently after
      the node switch; see the module docstring)
    - Recall/rewrite slots → three-layer resolution takes the first valid layer
      as ``[stage]``; all empty/invalid → ``[]``
    """
    if not isinstance(stage, StageSlot):
        return [stage]

    if isinstance(stage, GenerateSlot):
        parts: List[Any] = [_GenerateNLUPart(module, pattern)]
        if getattr(module, "type", None) == ModuleType.FSM and getattr(module, "enable_clarify", False):
            parts.append(getattr(module, "clarify_stage", None)
                         or _builtin_clarify())
        parts.append(_GenerateNLGPart(module, pattern))
        return parts

    attr = stage.stage_name.removesuffix("_slot")
    for layer_name, value in _layered_values(attr, ctx, module, pattern):
        if is_valid_stage(value):
            return [value]
        logger.warning(
            "[stage_slots] %s 层 %s 配置非法（stage 需有 callable execute），"
            "降级下一层: %r",
            layer_name, attr, value,
        )
    return []
