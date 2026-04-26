"""Unit tests for the recommendation classifier in agent.diagnose."""
from __future__ import annotations

from agent.diagnose import _classify


class TestClassify:
    def test_low_spend_returns_watch(self) -> None:
        # below the €10 minimum spend threshold
        assert _classify(spend=5, real_cpl=None, ctr=0.5, median_cpl=2.0, median_ctr=1.0) == "watch"

    def test_no_leads_on_meaningful_spend_pauses(self) -> None:
        # spent €40, zero conversions → pause
        assert _classify(spend=40, real_cpl=None, ctr=1.0, median_cpl=2.0, median_ctr=1.0) == "pause"

    def test_no_leads_under_30_spend_does_not_pause(self) -> None:
        assert _classify(spend=20, real_cpl=None, ctr=1.0, median_cpl=2.0, median_ctr=1.0) == "watch"

    def test_cpl_well_above_median_pauses(self) -> None:
        # 4€ vs median 2€ → 2x → pause
        assert _classify(spend=50, real_cpl=4.0, ctr=1.0, median_cpl=2.0, median_ctr=1.0) == "pause"

    def test_cpl_slightly_above_median_keeps(self) -> None:
        # 2.5€ vs median 2€ → 1.25x → keep
        assert _classify(spend=50, real_cpl=2.5, ctr=1.0, median_cpl=2.0, median_ctr=1.0) == "keep"

    def test_ctr_collapsed_pauses(self) -> None:
        # CTR 0.3 vs median 1.0 → 30% < 50% → pause
        assert _classify(spend=50, real_cpl=2.0, ctr=0.3, median_cpl=2.0, median_ctr=1.0) == "pause"

    def test_healthy_ad_keeps(self) -> None:
        assert _classify(spend=50, real_cpl=1.8, ctr=1.5, median_cpl=2.0, median_ctr=1.0) == "keep"

    def test_no_median_cpl_does_not_crash(self) -> None:
        # if no ad has any leads, median is None — must still return a sensible class
        result = _classify(spend=50, real_cpl=None, ctr=1.0, median_cpl=None, median_ctr=1.0)
        assert result == "pause"  # spend ≥ 30 and no leads
