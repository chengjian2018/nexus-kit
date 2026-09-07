"""ClarifyRouteRule — R1 rule-based gating (pure functions, no LLM).

Takes recall results and the clarify slots (topic / keywords) and outputs one of three modes:
- "kb"       : track one — high recall confidence, answer from the business knowledge base
- "fallback" : track two — no recall or low confidence, question-responsive reply + strong pull-back
- "mixed"    : ambiguous zone — partial business knowledge + question-responsive reply

Gating rules (see spec 5.3 for details):
- Empty recall -> fallback (default track when there is no recall)
- Adjusted top score >= t_high -> kb
- t_low <= adjusted top score < t_high -> mixed
- Adjustment: when topic / keywords overlap the chunk's metadata.keywords (biz_keyword),
  add keyword_bonus to the top score
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


class ClarifyRouteRule:
    """R1 gating rule."""

    def __init__(
        self,
        t_high: float = 0.6,
        t_low: float = 0.3,
        keyword_bonus: float = 0.1,
    ):
        self.t_high = t_high
        self.t_low = t_low
        self.keyword_bonus = keyword_bonus

    def route(
        self,
        recall_results: List[Dict[str, Any]],
        topic: str,
        keywords: List[str],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Gating decision; returns (mode, bonus-adjusted copy of the results).

        Args:
            recall_results: fused and reranked recall results (standardized format).
            topic: NLU clarify slot — off-topic question subject.
            keywords: NLU clarify slot — keyword list of the off-topic question.

        Returns:
            (mode, adjusted_results). adjusted_results is a shallow-copied list;
            the top result's score is already boosted when it hits the keyword overlap.
        """
        if not recall_results:
            return "fallback", []

        adjusted = [dict(r) for r in recall_results]
        top = adjusted[0]

        if self._has_overlap(top, topic, keywords):
            top["score"] = round(top.get("score", 0.0) + self.keyword_bonus, 6)

        top_score = top.get("score", 0.0)
        if top_score >= self.t_high:
            mode = "kb"
        elif top_score >= self.t_low:
            mode = "mixed"
        else:
            mode = "fallback"

        return mode, adjusted

    @staticmethod
    def _has_overlap(
        top: Dict[str, Any],
        topic: str,
        keywords: List[str],
    ) -> bool:
        """Whether topic / keywords overlap the top chunk's business keywords.

        Chunk keywords come from ``metadata.keywords`` (where the standardized
        biz_keyword field lives); inclusion is checked word by word in both directions.
        """
        chunk_keywords = [
            str(k) for k in (top.get("metadata", {}).get("keywords") or [])
        ]
        terms = [topic] + [str(k) for k in keywords if k]
        for term in terms:
            if not term:
                continue
            for ck in chunk_keywords:
                if term in ck or ck in term:
                    return True
        return False
