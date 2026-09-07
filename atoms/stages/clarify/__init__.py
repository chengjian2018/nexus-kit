"""clarify — dual-track clarify.

Discrimination and response when a user's answer drifts off the main thread in task-oriented dialogue:
- Track one (kb)      : answer from business knowledge base recall + light pull-back
- Track two (fallback): question-responsive reply + strong pull-back
- Ambiguous (mixed)   : partial business knowledge + question-responsive reply
"""

from atoms.stages.clarify.rule import ClarifyRouteRule
from atoms.stages.clarify.stage import ClarifyStage

__all__ = ["ClarifyRouteRule", "ClarifyStage"]
