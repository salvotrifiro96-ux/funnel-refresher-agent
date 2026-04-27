"""Unit tests for apply_lead_overrides."""
from __future__ import annotations

from agent.diagnose import (
    AdRow,
    DiagnoseReport,
    ReferralRow,
    apply_lead_overrides,
)


def _make_report() -> DiagnoseReport:
    referrals = [
        ReferralRow("img1", spend=100.0, clicks=200, real_leads=10, real_cpl=10.0),
        ReferralRow("img2", spend=50.0, clicks=100, real_leads=5, real_cpl=10.0),
        ReferralRow("img3", spend=80.0, clicks=150, real_leads=0, real_cpl=None),
    ]
    ads = [
        AdRow(
            ad_id="a1", name="ad1", status="ACTIVE", referral="img1",
            adset_name="Test", spend=100.0, impressions=10000, clicks=200,
            ctr=2.0, real_leads=10, real_cpl=10.0,
            landing_link="https://x.com/?referral=img1", recommendation="keep",
        ),
        AdRow(
            ad_id="a2", name="ad2", status="ACTIVE", referral="img2",
            adset_name="Test", spend=50.0, impressions=5000, clicks=100,
            ctr=2.0, real_leads=5, real_cpl=10.0,
            landing_link="https://x.com/?referral=img2", recommendation="keep",
        ),
        AdRow(
            ad_id="a3", name="ad3", status="ACTIVE", referral="img3",
            adset_name="Test", spend=80.0, impressions=8000, clicks=150,
            ctr=1.875, real_leads=0, real_cpl=None,
            landing_link="https://x.com/?referral=img3", recommendation="pause",
        ),
    ]
    return DiagnoseReport(
        since="2026-04-01", until="2026-04-15", days=14,
        ads=ads, referrals=referrals,
        total_spend=230.0, total_real_leads=15, avg_real_cpl=15.33,
        candidate_ads_to_pause=["a3"],
    )


class TestApplyLeadOverrides:
    def test_no_overrides_returns_same_report(self) -> None:
        r = _make_report()
        out = apply_lead_overrides(r, {})
        assert out is r  # short-circuit returns same object

    def test_override_recomputes_cpl(self) -> None:
        r = _make_report()
        out = apply_lead_overrides(r, {"img1": 20})
        img1 = next(x for x in out.referrals if x.referral == "img1")
        assert img1.real_leads == 20
        assert img1.real_cpl == 5.0  # 100 spend / 20 leads

    def test_override_to_zero_makes_cpl_none(self) -> None:
        r = _make_report()
        out = apply_lead_overrides(r, {"img1": 0})
        img1 = next(x for x in out.referrals if x.referral == "img1")
        assert img1.real_leads == 0
        assert img1.real_cpl is None

    def test_override_propagates_to_ad_rows(self) -> None:
        r = _make_report()
        out = apply_lead_overrides(r, {"img1": 20})
        a1 = next(a for a in out.ads if a.referral == "img1")
        assert a1.real_leads == 20
        assert a1.real_cpl == 5.0

    def test_override_recomputes_totals(self) -> None:
        r = _make_report()
        # Add 10 leads to img3 (which had 0)
        out = apply_lead_overrides(r, {"img3": 10})
        assert out.total_real_leads == 25  # 10 + 5 + 10
        assert out.total_spend == 230.0
        assert out.avg_real_cpl is not None
        assert abs(out.avg_real_cpl - 9.2) < 0.01

    def test_override_changes_pause_candidates(self) -> None:
        r = _make_report()
        # img3 was a pause candidate (no leads on €80 spend); adding leads makes it healthy
        out = apply_lead_overrides(r, {"img3": 10})  # CPL becomes €8
        a3 = next(a for a in out.ads if a.referral == "img3")
        # With CPL €8 and median around €8-10, no longer 1.5x median → not pause
        assert a3.recommendation != "pause"
        assert "a3" not in out.candidate_ads_to_pause

    def test_negative_override_clamped_to_zero(self) -> None:
        r = _make_report()
        out = apply_lead_overrides(r, {"img1": -5})
        img1 = next(x for x in out.referrals if x.referral == "img1")
        assert img1.real_leads == 0

    def test_partial_overrides_leave_others_unchanged(self) -> None:
        r = _make_report()
        out = apply_lead_overrides(r, {"img1": 50})
        img2 = next(x for x in out.referrals if x.referral == "img2")
        assert img2.real_leads == 5  # unchanged
        assert img2.real_cpl == 10.0
