"""Stage atoms: nlu / nlg / unified / query / recaller / clarify + default prompts.

Importing this package does two registrations:

1. the kernel's builtin fallback stage factories into ``nexus.pipeline``
   (register_default_generate/_clarify — the kernel never imports atom
   implementations, so this warm-up is what makes the default skeleton
   runnable; host bootstrap and tests/conftest.py do it automatically);
2. the named stages into the plugin registry (kind="stage") — the string
   codes that pattern/node ``stages`` declarations reference (FSM patterns
   only — plan-⑧: AGENT nodes run via their loop executors):

   - ``fsm_nlu`` / ``fsm_nlg``       : FSM two-stage defaults
   - ``fsm_unified``                 : single-call unified stage
   - ``nlg_pass_through``            : no-op NLG companion for unified
   - ``time_aug_query``              : time-augmentation query rewrite
   - ``clarify_default``             : default ClarifyStage assembly
"""

from nexus.pipeline import register_default_clarify, register_default_generate
from nexus.registry.plugins import registry

from atoms.stages.nlu import FSMNLU
from atoms.stages.nlg import FSMNLG
from atoms.stages.unified import (
    FSMUnifiedNLU,
    PassThroughNLG,
)
from atoms.stages.clarify import ClarifyRouteRule, ClarifyStage
from atoms.stages.recaller import (
    KeywordRecallPath,  # noqa: F401 -- re-exported for custom recall paths
    MultiPathRecaller,
    ScoreThresholdFilter,
    WeightedScoreFusion,
)
from atoms.stages.query.time_aug import TimeAugQueryRewriter


def _default_clarify_stage():
    """Build the default ClarifyStage: in-memory keyword recall + default gating.

    Production should configure the clarify slot explicitly on the node (the
    ES-backed recall path); the default instance guarantees out-of-the-box
    usability.
    """
    return ClarifyStage(
        recaller=MultiPathRecaller(
            recall_paths=[],
            filters=[ScoreThresholdFilter(threshold=0.1)],
            fusion=WeightedScoreFusion(),
        ),
        rule=ClarifyRouteRule(),
    )


register_default_generate("fsm", lambda: (FSMNLU(), FSMNLG()))
register_default_clarify(_default_clarify_stage)

# ---------------------------------------------------------------------------
# Named stages (kind="stage") — string codes referenced by stages declarations
# ---------------------------------------------------------------------------

registry.register("stage", "fsm_nlu", FSMNLU)
registry.register("stage", "fsm_nlg", FSMNLG)
registry.register("stage", "fsm_unified", FSMUnifiedNLU)
registry.register("stage", "nlg_pass_through", PassThroughNLG)
registry.register("stage", "time_aug_query", TimeAugQueryRewriter)
registry.register("stage", "clarify_default", _default_clarify_stage)
