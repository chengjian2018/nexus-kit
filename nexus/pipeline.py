"""Pipeline stages — the ordered slot skeleton + two-layer lazy resolution
(node > pattern) at execution time. FSM patterns only — AGENT nodes
run via their loop executors and have no stages pipeline.

Declarative shape:

- ``pattern.stages: List[Dict[str, Optional[str]]]`` — the ordered skeleton,
  each entry a single-key dict {slot_name: code-or-None}. None means "fill
  at runtime from the layers"; a string is the pattern-level default for
  that slot. Default skeleton (kernel builtin):
      [{"pre_recall": None}, {"query": None}, {"post_recall": None},
       {"nlu": None}, {"clarify": None}, {"nlg": None}]
- ``node.stages: Dict[str, str]`` — the node-layer slot config (slot_name ->
  plugin code).
- Resolution per slot: node.stages > pattern skeleton value > builtin
  default code. A slot that resolves to None is **skipped** (the universal
  rule — pre_recall / post_recall / clarify are optional by nature; nlu/nlg
  default to the unified builtin).
- Stage codes are strings resolved from the plugin registry (kind="stage");
  registration lives in atoms (module-level ``registry.register(...)`` +
  AST discovery).
- Unified dedup: nlu/nlg may share one code (the unified stage writes both
  nlu_result and nlg_result); the resolved execution sequence executes any
  code at most once. Any other repetition is a declaration error caught by
  validation.
- enable_clarify is gone: declaring ``clarify`` in stages IS the switch.

The module layer of the pre-merge three-layer resolution is gone with the
module layer itself; the ROUTE-era deferred-NLG wrapper is gone too (no
same-turn node switch outside NLU advancement anymore).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from nexus.context import DialogueContext
from nexus.registry.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)


# ============================================================================
# Default skeleton (kernel builtin)
# ============================================================================

DEFAULT_SKELETON_SLOTS: List[str] = [
    "pre_recall", "query", "post_recall", "nlu", "clarify", "nlg",
]

# Slot pair allowed to share one code (unified single-call stage writes both
# nlu_result and nlg_result)
_UNIFIED_PAIR = ("nlu", "nlg")


def default_skeleton() -> List[Dict[str, Optional[str]]]:
    """The kernel default skeleton: six ordered slots, all None values.

    A fresh list per call (callers may mutate); values are filled at
    resolution time by the two-layer lookup / builtin defaults.
    """
    return [{slot: None} for slot in DEFAULT_SKELETON_SLOTS]


def normalize_skeleton(stages: Optional[List[Any]]) -> List[Dict[str, Optional[str]]]:
    """Normalize a pattern.stages declaration into the canonical ordered form.

    Accepts the single-key-dict list (each {slot: code-or-None}); None/empty
    falls back to the default skeleton. Raises ValueError on malformed
    entries (not a list / not single-key dicts / slot name not a string) —
    the pattern author's explicit choice, fail fast at construction.
    """
    if not stages:
        return default_skeleton()
    if not isinstance(stages, list):
        raise ValueError(
            f"pattern.stages 必须是有序的 list[单键dict]（槽位名→code 或 None）: {stages!r}")
    normalized: List[Dict[str, Optional[str]]] = []
    for idx, entry in enumerate(stages):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(
                f"pattern.stages[{idx}] 必须是单键 dict（槽位名→code 或 None）: {entry!r}")
        (slot, code), = entry.items()
        if not isinstance(slot, str) or not slot:
            raise ValueError(
                f"pattern.stages[{idx}] 槽位名必须是字符串: {slot!r}")
        if code is not None and not isinstance(code, str):
            raise ValueError(
                f"pattern.stages[{idx}] 的 {slot!r} 槽位 code 必须是 str 或 None: {code!r}")
        normalized.append({slot: code})
    return normalized


# ============================================================================
# Builtin default-stage hooks (registered by atoms, never imported)
# ============================================================================

# The kernel must not depend on atom implementations: the builtin generate /
# clarify fallbacks are registered at import time by ``atoms.stages`` (the
# host and tests/conftest warm it up), keeping the layering one-directional.
# Storage goes through the plugin registry (kind="stage_factory", keyed by
# pattern type / "clarify") — the public register_default_* API is unchanged.


def register_default_generate(pattern_type: Any,
                              factory: Callable[[], Tuple[Any, Any]]) -> None:
    """Register the builtin ``(nlu, nlg)`` fallback pair for a pattern type."""
    plugin_registry.register(
        "stage_factory", f"generate:{pattern_type}", factory)


def register_default_clarify(factory: Callable[[], Any]) -> None:
    """Register the builtin clarify-stage factory (``clarify`` slot fallback)."""
    plugin_registry.register("stage_factory", "clarify", factory)


def builtin_generate_default(pattern_type: Any) -> Optional[Dict[str, str]]:
    """The builtin nlu/nlg default codes for a pattern type, if registered.

    Returns e.g. {"nlu": "builtin:generate:X#0", "nlg": "builtin:generate:X#1"}
    where X is the stage_factory code and #0/#1 select the pair element (the
    two-stage default registers two different stages; a unified default
    registers one stage reached via both element markers). None when
    atoms.stages has not registered the pair.
    """
    code = f"generate:{pattern_type}"
    if not plugin_registry.has("stage_factory", code):
        return None
    return {"nlu": f"builtin:{code}#0", "nlg": f"builtin:{code}#1"}


def _resolve_builtin_stage(builtin_code: str, element: int = 0):
    """Resolve a ``builtin:<stage_factory code>`` marker to a stage instance.

    The generate factories return the (nlu, nlg) pair — element selects which
    (nlu → 0, nlg → 1; the pair may be the same class for unified stages,
    different classes for the two-stage default).
    """
    factory_code = builtin_code.removeprefix("builtin:")
    resolved = plugin_registry.resolve("stage_factory", factory_code)
    if isinstance(resolved, tuple):
        return resolved[element]
    return resolved


def builtin_clarify_default() -> Optional[str]:
    """The builtin clarify default code, if registered."""
    if plugin_registry.has("stage_factory", "clarify"):
        return "builtin:clarify"
    return None


# ============================================================================
# Slot resolution (two layers + builtin defaults)
# ============================================================================

def resolve_stage_code(slot: str, cxt: DialogueContext, node: Any,
                       pattern: Any, skeleton_value: Optional[str] = None,
                       ) -> Optional[str]:
    """Resolve a slot's code: node.stages > skeleton value.

    The builtin-default tail (nlu/nlg/clarify) is applied by
    resolve_execution_sequence, which knows the whole skeleton; this
    function covers only the declarative layers.
    """
    node_stages = getattr(node, "stages", None) or {}
    if slot in node_stages:
        return node_stages[slot]
    return skeleton_value


def _resolve_code_to_stage(code: Optional[str]) -> Optional[Any]:
    """Resolve a stage code to a stage instance (None stays None = skip).

    ``builtin:<factory>#<element>`` markers go to the stage_factory storage;
    other codes to the plugin registry kind="stage".
    """
    if code is None:
        return None
    if code.startswith("builtin:"):
        element = 0
        factory_code = code.removeprefix("builtin:")
        if "#" in factory_code:
            factory_code, elem = factory_code.split("#", 1)
            element = int(elem)
        stage = _resolve_builtin_stage(f"builtin:{factory_code}", element)
        if stage is None:
            return None
        return stage
    if not plugin_registry.has("stage", code):
        logger.warning(
            "[stages] stage code 未注册（跳过该槽位）: %r（请 import 对应 stage 原子模块）",
            code,
        )
        return None
    return plugin_registry.resolve("stage", code)


def resolve_execution_sequence(cxt: DialogueContext, node: Any,
                               pattern: Any) -> List[Tuple[str, Any]]:
    """Resolve the pattern skeleton into the concrete (slot, stage) sequence.

    Rules:
    - Per slot: node.stages > skeleton value > builtin default
    - None after all layers → slot skipped (not in the sequence)
    - Unified dedup: when nlu and nlg resolve to the same code, the nlg entry
      is dropped (the unified stage already wrote nlg_result); any other
      duplicate code across slots logs a warning and keeps only the first
      occurrence — a declaration error surfaced by validation
    """
    raw_stages = getattr(pattern, "stages", None) if pattern is not None else None
    skeleton = normalize_skeleton(raw_stages)
    skeleton_values = {slot: code for entry in skeleton
                       for slot, code in entry.items()}

    sequence: List[Tuple[str, Any]] = []
    seen_codes: Dict[str, str] = {}
    resolved_codes: Dict[str, Optional[str]] = {}

    for entry in skeleton:
        (slot, _skeleton_code), = entry.items()
        code = resolve_stage_code(slot, cxt, node, pattern,
                                  skeleton_value=skeleton_values.get(slot))
        resolved_codes[slot] = code

    # Builtin defaults tail (nlu/nlg only), and only for slots the skeleton
    # actually carries. The clarify slot is opt-in by declaration — no
    # builtin tail (a pattern that does not declare clarify never gets one;
    # the builtin factory backs the *declared* clarify slot's code only via
    # "builtin:clarify", which resolve_stage_code never produces).
    if resolved_codes.get("nlu") is None or resolved_codes.get("nlg") is None:
        builtin_pair = builtin_generate_default("fsm")
        if builtin_pair:
            for slot in ("nlu", "nlg"):
                if resolved_codes.get(slot) is None:
                    resolved_codes[slot] = builtin_pair[slot]

    for entry in skeleton:
        (slot, _), = entry.items()
        code = resolved_codes.get(slot)
        if code is None:
            continue  # universal skip rule

        # Unified pair: nlg sharing nlu's code is dropped (single execution)
        if (slot == "nlg"
                and resolved_codes.get("nlu") is not None
                and code == resolved_codes["nlu"]):
            continue

        if code in seen_codes:
            logger.warning(
                "[stages] code %r 在多个槽位声明（%s 与 %s），仅首个生效",
                code, seen_codes[code], slot,
            )
            continue
        seen_codes[code] = slot

        stage = _resolve_code_to_stage(code)
        if stage is None:
            continue
        sequence.append((slot, stage))

    return sequence


# ============================================================================
# Compat: stage duck-typing validation
# ============================================================================

def is_valid_stage(obj: Any) -> bool:
    """Duck-typed stage validation: has a callable ``execute``."""
    return (
        obj is not None
        and hasattr(obj, "execute")
        and callable(obj.execute)
    )


__all__ = [
    "DEFAULT_SKELETON_SLOTS",
    "builtin_clarify_default",
    "builtin_generate_default",
    "default_skeleton",
    "is_valid_stage",
    "normalize_skeleton",
    "register_default_clarify",
    "register_default_generate",
    "resolve_execution_sequence",
    "resolve_stage_code",
]
