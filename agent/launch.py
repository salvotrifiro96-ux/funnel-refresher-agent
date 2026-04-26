"""Launch step — pause selected losers, optionally clone the adset, create new ads.

`LaunchPlan` captures every answer from the pre-launch questionnaire so the call
site can validate once and pass a single object through.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent.generate import Creative
from agent.meta_api import MetaClient


@dataclass(frozen=True)
class LaunchPlan:
    """All the operator's pre-launch decisions in one place."""

    # Pause selections
    ads_to_pause: tuple[str, ...] = ()
    untouchable_ad_ids: tuple[str, ...] = ()

    # Adset placement: False = use existing active adset, True = create new dedicated one
    create_new_adset: bool = False
    new_adset_name: str = ""
    new_adset_daily_budget_eur: float = 0.0
    new_adset_start_time_iso: str = ""  # ISO 8601 with TZ; empty = start immediately
    new_adset_copy_targeting: bool = True  # v1: always True; UI may flag a manual override note
    new_adset_targeting_note: str = ""  # informational only

    # Status of the newly-created ads
    start_status: str = "ACTIVE"  # "ACTIVE" | "PAUSED"

    # Tracking + creative
    referral_prefix: str = "refresh"
    cta_type: str = "LEARN_MORE"

    def validate(self) -> list[str]:
        """Return a list of human-readable validation errors, empty if OK."""
        errors: list[str] = []
        if self.start_status not in ("ACTIVE", "PAUSED"):
            errors.append("start_status must be ACTIVE or PAUSED")
        if self.cta_type not in (
            "LEARN_MORE",
            "SIGN_UP",
            "APPLY_NOW",
            "DOWNLOAD",
            "GET_OFFER",
            "SUBSCRIBE",
            "CONTACT_US",
            "GET_QUOTE",
        ):
            errors.append(f"unsupported cta_type: {self.cta_type}")
        if not self.referral_prefix or " " in self.referral_prefix:
            errors.append("referral_prefix must be non-empty and contain no spaces")
        if self.create_new_adset:
            if not self.new_adset_name.strip():
                errors.append("new_adset_name required when create_new_adset=True")
            if self.new_adset_daily_budget_eur <= 0:
                errors.append("new_adset_daily_budget_eur must be > 0")
        return errors


@dataclass(frozen=True)
class LaunchResult:
    paused: list[str]
    created: list[dict[str, str]]  # each: {"name", "ad_id", "creative_id", "referral"}
    new_adset_id: str | None = None  # set if a new adset was created


def _next_referral_index(existing_referrals: list[str], prefix: str) -> int:
    """Find the next free index for `<prefix>_N` referral tags."""
    used: set[int] = set()
    for ref in existing_referrals:
        if not ref.startswith(prefix + "_"):
            continue
        tail = ref[len(prefix) + 1 :]
        if tail.isdigit():
            used.add(int(tail))
    n = 1
    while n in used:
        n += 1
    return n


def _clone_adset_targeting(
    meta: MetaClient,
    *,
    source_adset_id: str,
    campaign_id: str,
    plan: LaunchPlan,
) -> str:
    """Clone the source adset's full configuration into a new adset.

    The new adset reuses billing_event, optimization_goal, bid_strategy, promoted_object,
    targeting, and destination_type from the source. Only name + daily_budget + start_time
    are overridden by the LaunchPlan.
    """
    src = meta.get_adset_full(source_adset_id)
    targeting = src.get("targeting") or {}
    promoted_object = src.get("promoted_object") or {}
    if isinstance(targeting, str):
        targeting = json.loads(targeting)
    if isinstance(promoted_object, str):
        promoted_object = json.loads(promoted_object)
    return meta.create_adset(
        campaign_id=campaign_id,
        name=plan.new_adset_name,
        daily_budget_cents=int(round(plan.new_adset_daily_budget_eur * 100)),
        billing_event=src.get("billing_event") or "IMPRESSIONS",
        optimization_goal=src.get("optimization_goal") or "OFFSITE_CONVERSIONS",
        bid_strategy=src.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP",
        promoted_object=promoted_object,
        targeting=targeting,
        start_time=plan.new_adset_start_time_iso or None,
        status="ACTIVE",  # the adset itself is always ACTIVE; ad-level status controlled by start_status
        destination_type=src.get("destination_type"),
    )


def launch_refresh(
    *,
    meta: MetaClient,
    campaign_id: str,
    plan: LaunchPlan,
    creatives_to_launch: list[Creative],
    landing_url: str,
    page_id: str,
    instagram_user_id: str,
) -> LaunchResult:
    """Execute the refresh according to `plan`. Idempotent on pause."""
    errors = plan.validate()
    if errors:
        raise ValueError("Invalid LaunchPlan: " + "; ".join(errors))

    untouchables = set(plan.untouchable_ad_ids)
    safe_to_pause = [a for a in plan.ads_to_pause if a not in untouchables]

    # 1. Pause selected ads (skipping untouchables)
    paused: list[str] = []
    for ad_id in safe_to_pause:
        meta.pause_ad(ad_id)
        paused.append(ad_id)

    # 2. Determine the destination adset
    new_adset_id: str | None = None
    if plan.create_new_adset:
        source_adset_id = meta.find_active_adset(campaign_id)
        new_adset_id = _clone_adset_targeting(
            meta,
            source_adset_id=source_adset_id,
            campaign_id=campaign_id,
            plan=plan,
        )
        target_adset_id = new_adset_id
    else:
        target_adset_id = meta.find_active_adset(campaign_id)

    # 3. Pick referral indices that don't collide with existing campaign referrals
    existing = [a.referral for a in meta.list_ads(campaign_id)]
    next_idx = _next_referral_index(existing, plan.referral_prefix)

    # 4. Upload images, create creatives + ads
    created: list[dict[str, str]] = []
    landing_clean = landing_url.split("?")[0].rstrip("/")
    for offset, c in enumerate(creatives_to_launch):
        idx = next_idx + offset
        referral = f"{plan.referral_prefix}_{idx}"
        landing_with_ref = f"{landing_clean}?referral={referral}"
        image_hash = meta.upload_image_bytes(c.image_bytes, filename=f"{referral}.png")
        result = meta.create_ad(
            adset_id=target_adset_id,
            ad_name=referral,
            page_id=page_id,
            instagram_user_id=instagram_user_id,
            landing_url=landing_with_ref,
            image_hash=image_hash,
            headline=c.headline,
            body=c.body,
            cta_type=plan.cta_type,
            creative_label=f"Refresh — {c.slug}",
            status=plan.start_status,
        )
        created.append(
            {
                "name": referral,
                "ad_id": result["ad_id"],
                "creative_id": result["creative_id"],
                "referral": referral,
            }
        )
    return LaunchResult(paused=paused, created=created, new_adset_id=new_adset_id)
