"""ClarifyRouteRule — unit tests for the R1 gating pure functions."""

import pytest

from atoms.stages.clarify.rule import ClarifyRouteRule


def _item(score, content="chunk", keywords=None):
    """Build one standardized recall result."""
    return {
        "id": f"id-{score}",
        "content": content,
        "score": score,
        "source": "kb_es",
        "metadata": {"keywords": keywords or []},
    }


class TestRouteThreeBranches:
    """The three gating branches: kb / fallback / mixed."""

    def test_empty_recall_falls_back(self):
        rule = ClarifyRouteRule()
        mode, results = rule.route([], topic="费用", keywords=["收费"])
        assert mode == "fallback"
        assert results == []

    def test_high_score_routes_kb(self):
        rule = ClarifyRouteRule()
        mode, _ = rule.route([_item(0.8)], topic="费用", keywords=[])
        assert mode == "kb"

    def test_low_score_routes_fallback(self):
        rule = ClarifyRouteRule()
        mode, _ = rule.route([_item(0.1)], topic="费用", keywords=[])
        assert mode == "fallback"

    def test_middle_score_routes_mixed(self):
        rule = ClarifyRouteRule()
        mode, _ = rule.route([_item(0.45)], topic="费用", keywords=[])
        assert mode == "mixed"


class TestKeywordBonus:
    """topic/keywords overlap with chunk keywords -> bonus added to the top score."""

    def test_keyword_overlap_raises_score_into_kb(self):
        rule = ClarifyRouteRule()
        # 0.55 would be mixed on its own; a keywords hit adds +0.1
        # -> 0.65 >= t_high -> kb
        mode, results = rule.route(
            [_item(0.55, keywords=["收费", "服务费"])],
            topic="费用",
            keywords=["额外收费"],
        )
        assert mode == "kb"
        assert results[0]["score"] == pytest.approx(0.65)

    def test_no_overlap_no_bonus(self):
        rule = ClarifyRouteRule()
        mode, results = rule.route(
            [_item(0.55, keywords=["保险"])],
            topic="费用",
            keywords=["上牌"],
        )
        assert mode == "mixed"
        assert results[0]["score"] == pytest.approx(0.55)


class TestThresholdBoundary:
    """Threshold boundaries: >= T_high is kb; T_low <= s < T_high is mixed."""

    def test_score_equals_t_high_is_kb(self):
        rule = ClarifyRouteRule()
        mode, _ = rule.route([_item(0.6)], topic="", keywords=[])
        assert mode == "kb"

    def test_score_equals_t_low_is_mixed(self):
        rule = ClarifyRouteRule()
        mode, _ = rule.route([_item(0.3)], topic="", keywords=[])
        assert mode == "mixed"

    def test_score_just_below_t_low_is_fallback(self):
        rule = ClarifyRouteRule()
        mode, _ = rule.route([_item(0.29)], topic="", keywords=[])
        assert mode == "fallback"
