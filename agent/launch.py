"""Launch step — pause selected losers and create the approved new ads.

Naming convention for the new ads matches what the existing meta-ads-analyzer
scripts use: `new_<campaign_slug>_<N>` so referral tracking stays consistent.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.generate import Creative
from agent.meta_api import MetaClient


@dataclass(frozen=True)
class LaunchResult:
    paused: list[str]
    created: list[dict[str, str]]  # each: {"name", "ad_id", "creative_id", "referral"}


def _next_referral_index(existing_referrals: list[str], prefix: str) -> int:
    """Find the next free index for `<prefix>_N` referral tags."""
    used = set()
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


def launch_refresh(
    *,
    meta: MetaClient,
    campaign_id: str,
    ads_to_pause: list[str],
    creatives_to_launch: list[Creative],
    landing_url: str,
    page_id: str,
    instagram_user_id: str,
    referral_prefix: str = "refresh",
    cta_type: str = "LEARN_MORE",
) -> LaunchResult:
    """Pause + create. Idempotent on pause (already-paused ads are no-op-ish)."""
    # 1. Pause selected ads
    paused: list[str] = []
    for ad_id in ads_to_pause:
        meta.pause_ad(ad_id)
        paused.append(ad_id)

    # 2. Find the active adset to attach new ads to
    adset_id = meta.find_active_adset(campaign_id)

    # 3. Pick referral indices that don't collide with existing ones
    existing = [a.referral for a in meta.list_ads(campaign_id)]
    next_idx = _next_referral_index(existing, referral_prefix)

    # 4. Upload images + create creatives + create ads
    created: list[dict[str, str]] = []
    landing_clean = landing_url.split("?")[0].rstrip("/")
    for offset, c in enumerate(creatives_to_launch):
        idx = next_idx + offset
        referral = f"{referral_prefix}_{idx}"
        landing_with_ref = f"{landing_clean}?referral={referral}"
        image_hash = meta.upload_image_bytes(c.image_bytes, filename=f"{referral}.png")
        ad_name = referral
        result = meta.create_ad(
            adset_id=adset_id,
            ad_name=ad_name,
            page_id=page_id,
            instagram_user_id=instagram_user_id,
            landing_url=landing_with_ref,
            image_hash=image_hash,
            headline=c.headline,
            body=c.body,
            cta_type=cta_type,
            creative_label=f"Refresh — {c.slug}",
        )
        created.append(
            {
                "name": ad_name,
                "ad_id": result["ad_id"],
                "creative_id": result["creative_id"],
                "referral": referral,
            }
        )
    return LaunchResult(paused=paused, created=created)
