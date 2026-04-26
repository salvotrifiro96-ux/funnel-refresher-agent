"""Unit tests for the pure helpers in agent.meta_api."""
from __future__ import annotations

import pytest

from agent.meta_api import _extract_lead_metrics, _referral_from_url


class TestReferralFromUrl:
    def test_returns_direct_for_empty(self) -> None:
        assert _referral_from_url("") == "direct"

    def test_returns_direct_when_no_param(self) -> None:
        assert _referral_from_url("https://example.com/page") == "direct"

    def test_extracts_referral(self) -> None:
        assert (
            _referral_from_url("https://example.com/page?referral=img5") == "img5"
        )

    def test_extracts_referral_with_other_params(self) -> None:
        url = "https://example.com/page?utm_source=fb&referral=new_mktg_ia_3&fbclid=xyz"
        assert _referral_from_url(url) == "new_mktg_ia_3"

    def test_returns_direct_on_malformed_url(self) -> None:
        # Should not raise, just fall back
        assert _referral_from_url("not a url at all") == "direct"


class TestExtractLeadMetrics:
    def test_no_actions_returns_zero(self) -> None:
        leads, cpl = _extract_lead_metrics({})
        assert leads == 0
        assert cpl == 0.0

    def test_sums_lead_actions(self) -> None:
        insight = {
            "actions": [
                {"action_type": "lead", "value": "5"},
                {"action_type": "offsite_conversion.fb_pixel_lead", "value": "3"},
                {"action_type": "link_click", "value": "100"},  # ignored
            ],
            "cost_per_action_type": [
                {"action_type": "lead", "value": "2.5"},
            ],
        }
        leads, cpl = _extract_lead_metrics(insight)
        assert leads == 8
        assert cpl == 2.5

    def test_takes_first_matching_cpl(self) -> None:
        insight = {
            "actions": [{"action_type": "lead", "value": "1"}],
            "cost_per_action_type": [
                {"action_type": "lead", "value": "1.0"},
                {"action_type": "lead", "value": "9.0"},  # ignored — first wins
            ],
        }
        _, cpl = _extract_lead_metrics(insight)
        assert cpl == 1.0


class TestMetaClientValidation:
    def test_rejects_empty_token(self) -> None:
        from agent.meta_api import MetaClient

        with pytest.raises(ValueError, match="access_token"):
            MetaClient("", "act_123")

    def test_rejects_account_without_act_prefix(self) -> None:
        from agent.meta_api import MetaClient

        with pytest.raises(ValueError, match="act_"):
            MetaClient("token", "123456")
