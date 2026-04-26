"""Unit tests for the referral indexing helper in agent.launch."""
from __future__ import annotations

from agent.launch import _next_referral_index


class TestNextReferralIndex:
    def test_no_existing_starts_at_one(self) -> None:
        assert _next_referral_index([], "refresh") == 1

    def test_existing_unrelated_returns_one(self) -> None:
        assert _next_referral_index(["direct", "img5", "old_ref_2"], "refresh") == 1

    def test_picks_smallest_free_index(self) -> None:
        existing = ["refresh_1", "refresh_3"]
        # 2 is free
        assert _next_referral_index(existing, "refresh") == 2

    def test_skips_taken_sequence(self) -> None:
        existing = ["refresh_1", "refresh_2", "refresh_3"]
        assert _next_referral_index(existing, "refresh") == 4

    def test_ignores_non_numeric_tails(self) -> None:
        existing = ["refresh_a", "refresh_xx", "refresh_2"]
        # only "refresh_2" counts as taken → next free is 1
        assert _next_referral_index(existing, "refresh") == 1

    def test_does_not_match_other_prefix(self) -> None:
        # "refresh_v2_1" does NOT match prefix "refresh"
        existing = ["refresh_v2_1", "refresh_v2_2"]
        assert _next_referral_index(existing, "refresh") == 1

    def test_matches_exact_prefix_with_underscore(self) -> None:
        existing = ["new_mktg_ia_1", "new_mktg_ia_5"]
        # 2,3,4 are free → smallest is 2
        assert _next_referral_index(existing, "new_mktg_ia") == 2
