"""Stage atoms: nlu / nlg / unified / query / recaller / clarify + default prompts.

Importing this package registers the kernel's builtin fallback stages into
``nexus.pipeline`` — the kernel never imports atom implementations, so this
warm-up is what makes the default four-slot skeleton runnable. The host
bootstrap and tests/conftest.py do it automatically.
"""

from nexus.model.module import ModuleType
from nexus.pipeline import register_default_clarify, register_default_generate

from atoms.stages.nlu import FSMNLU, RouteNLU
from atoms.stages.nlg import FSMNLG, RouteNLG
from atoms.stages.clarify import ClarifyRouteRule, ClarifyStage
from atoms.stages.recaller import (
    KeywordRecallPath,  # noqa: F401 -- re-exported for custom recall paths
    MultiPathRecaller,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)


def _default_clarify_stage():
    """Build the default ClarifyStage: in-memory keyword recall + default gating.

    Production should configure clarify_stage explicitly on the module (the
    ES-backed recall path); the default instance guarantees out-of-the-box
    usability. (Moved in from the old stage_slots.default_clarify_stage.)
    """
    return ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    )


register_default_generate(ModuleType.FSM, lambda: (FSMNLU(), FSMNLG()))
register_default_generate(ModuleType.ROUTE, lambda: (RouteNLU(), RouteNLG()))
register_default_clarify(_default_clarify_stage)
