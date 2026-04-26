"""Unit tests for LaunchPlan validation."""
from __future__ import annotations

from agent.launch import LaunchPlan


class TestLaunchPlanValidate:
    def test_default_plan_validates_clean(self) -> None:
        plan = LaunchPlan()
        assert plan.validate() == []

    def test_invalid_status_flagged(self) -> None:
        plan = LaunchPlan(start_status="WHATEVER")
        errs = plan.validate()
        assert any("start_status" in e for e in errs)

    def test_invalid_cta_flagged(self) -> None:
        plan = LaunchPlan(cta_type="BUY_NOW_PRESTO")
        errs = plan.validate()
        assert any("cta_type" in e for e in errs)

    def test_referral_prefix_with_space_flagged(self) -> None:
        plan = LaunchPlan(referral_prefix="my refresh")
        errs = plan.validate()
        assert any("referral_prefix" in e for e in errs)

    def test_empty_referral_prefix_flagged(self) -> None:
        plan = LaunchPlan(referral_prefix="")
        errs = plan.validate()
        assert any("referral_prefix" in e for e in errs)

    def test_new_adset_without_name_flagged(self) -> None:
        plan = LaunchPlan(create_new_adset=True, new_adset_name="", new_adset_daily_budget_eur=30)
        errs = plan.validate()
        assert any("new_adset_name" in e for e in errs)

    def test_new_adset_without_budget_flagged(self) -> None:
        plan = LaunchPlan(create_new_adset=True, new_adset_name="Refresh", new_adset_daily_budget_eur=0)
        errs = plan.validate()
        assert any("daily_budget" in e for e in errs)

    def test_new_adset_with_negative_budget_flagged(self) -> None:
        plan = LaunchPlan(create_new_adset=True, new_adset_name="Refresh", new_adset_daily_budget_eur=-5)
        errs = plan.validate()
        assert any("daily_budget" in e for e in errs)

    def test_new_adset_complete_validates(self) -> None:
        plan = LaunchPlan(
            create_new_adset=True,
            new_adset_name="Refresh 2026-04-26",
            new_adset_daily_budget_eur=30.0,
            new_adset_start_time_iso="2026-04-27T06:00:00+0200",
            referral_prefix="refresh",
            cta_type="APPLY_NOW",
        )
        assert plan.validate() == []

    def test_all_supported_cta_types_validate(self) -> None:
        for cta in (
            "LEARN_MORE",
            "SIGN_UP",
            "APPLY_NOW",
            "DOWNLOAD",
            "GET_OFFER",
            "SUBSCRIBE",
            "CONTACT_US",
            "GET_QUOTE",
        ):
            plan = LaunchPlan(cta_type=cta)
            assert plan.validate() == [], f"{cta} should validate"
