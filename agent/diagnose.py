"""Diagnosis step — pulls Meta ad-level performance and groups by `referral`.

Produces a `DiagnoseReport` that the UI renders as tables and that we hand to the
angle-generation prompt as ground truth ("here is what is fatigued and why").

Lead source: Meta pixel (`offsite_conversion.fb_pixel_lead`, `lead`, `onsite_web_lead`).
HubSpot integration was scoped out of v1 — Meta's reported leads are the data source.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.meta_api import AdInfo, AdInsights, MetaClient


@dataclass(frozen=True)
class AdRow:
    ad_id: str
    name: str
    status: str
    referral: str
    adset_name: str
    spend: float
    impressions: int
    clicks: int
    ctr: float
    real_leads: int
    real_cpl: float | None
    landing_link: str
    recommendation: str  # "keep" | "pause" | "watch"


@dataclass(frozen=True)
class ReferralRow:
    referral: str
    spend: float
    clicks: int
    real_leads: int
    real_cpl: float | None


@dataclass(frozen=True)
class DiagnoseReport:
    since: str
    until: str
    days: int
    ads: list[AdRow]
    referrals: list[ReferralRow]
    total_spend: float
    total_real_leads: int
    avg_real_cpl: float | None
    candidate_ads_to_pause: list[str] = field(default_factory=list)


def _classify(
    spend: float,
    real_cpl: float | None,
    ctr: float,
    median_cpl: float | None,
    median_ctr: float,
) -> str:
    """Heuristic recommendation for a single ad."""
    # Not enough data — leave it alone
    if spend < 10:
        return "watch"
    # No leads yet but spend hasn't crossed the eval threshold → keep watching
    if real_cpl is None and spend < 30:
        return "watch"
    # No leads despite meaningful spend → pause
    if real_cpl is None and spend >= 30:
        return "pause"
    # CPL is meaningfully worse than the median (>1.5x) → pause candidate
    if (
        median_cpl is not None
        and real_cpl is not None
        and real_cpl > median_cpl * 1.5
    ):
        return "pause"
    # CTR collapsed below half the median → pause candidate
    if ctr < median_ctr * 0.5 and median_ctr > 0:
        return "pause"
    return "keep"


def run_diagnosis(
    *,
    meta: MetaClient,
    campaign_id: str,
    days: int = 14,
) -> DiagnoseReport:
    """Full diagnosis: ads + insights aggregated per `referral`. Leads from Meta pixel."""
    until_dt = datetime.now()
    since_dt = until_dt - timedelta(days=days)
    since = since_dt.strftime("%Y-%m-%d")
    until = until_dt.strftime("%Y-%m-%d")

    # 1. Meta — ads + insights (insights includes pixel-reported leads)
    ads: list[AdInfo] = meta.list_ads(campaign_id)
    insights_by_id: dict[str, AdInsights] = {
        ad.ad_id: meta.get_insights(ad.ad_id, since, until) for ad in ads
    }

    # 2. Aggregate per-referral: spend, clicks, and leads (sum across ads sharing the
    #    same referral). Meta pixel leads are the lead-source-of-truth in v1.
    agg_by_referral: dict[str, dict[str, float]] = defaultdict(
        lambda: {"spend": 0.0, "clicks": 0, "leads": 0}
    )
    for ad in ads:
        ins = insights_by_id[ad.ad_id]
        agg_by_referral[ad.referral]["spend"] += ins.spend
        agg_by_referral[ad.referral]["clicks"] += ins.clicks
        agg_by_referral[ad.referral]["leads"] += ins.meta_leads

    referral_rows: list[ReferralRow] = []
    for ref, agg in agg_by_referral.items():
        spend = agg["spend"]
        clicks = int(agg["clicks"])
        leads = int(agg["leads"])
        cpl = (spend / leads) if leads > 0 else None
        referral_rows.append(ReferralRow(ref, spend, clicks, leads, cpl))
    referral_rows.sort(key=lambda r: r.spend, reverse=True)

    # 4. Per-ad rows: real CPL is computed from the *referral group* (not per ad)
    #    because one referral may span more than one ad in rare cases.
    real_cpl_by_referral = {
        r.referral: r.real_cpl for r in referral_rows
    }
    real_leads_by_referral = {
        r.referral: r.real_leads for r in referral_rows
    }

    cpl_values = [r.real_cpl for r in referral_rows if r.real_cpl is not None]
    median_cpl = sorted(cpl_values)[len(cpl_values) // 2] if cpl_values else None
    ctr_values = [insights_by_id[a.ad_id].ctr for a in ads if insights_by_id[a.ad_id].spend > 0]
    median_ctr = sorted(ctr_values)[len(ctr_values) // 2] if ctr_values else 0.0

    ad_rows: list[AdRow] = []
    for ad in ads:
        ins = insights_by_id[ad.ad_id]
        ad_rows.append(
            AdRow(
                ad_id=ad.ad_id,
                name=ad.name,
                status=ad.effective_status or ad.status,
                referral=ad.referral,
                adset_name=ad.adset_name,
                spend=ins.spend,
                impressions=ins.impressions,
                clicks=ins.clicks,
                ctr=ins.ctr,
                real_leads=real_leads_by_referral.get(ad.referral, 0),
                real_cpl=real_cpl_by_referral.get(ad.referral),
                landing_link=ad.landing_link,
                recommendation=_classify(
                    spend=ins.spend,
                    real_cpl=real_cpl_by_referral.get(ad.referral),
                    ctr=ins.ctr,
                    median_cpl=median_cpl,
                    median_ctr=median_ctr,
                ),
            )
        )
    ad_rows.sort(key=lambda r: r.spend, reverse=True)

    # 5. Totals
    total_spend = sum(r.spend for r in referral_rows)
    total_leads = sum(r.real_leads for r in referral_rows)
    avg_cpl = (total_spend / total_leads) if total_leads > 0 else None

    # 6. Pause candidates: only currently-active ads that are flagged "pause"
    candidates = [
        r.ad_id for r in ad_rows if r.recommendation == "pause" and r.status == "ACTIVE"
    ]

    return DiagnoseReport(
        since=since,
        until=until,
        days=days,
        ads=ad_rows,
        referrals=referral_rows,
        total_spend=total_spend,
        total_real_leads=total_leads,
        avg_real_cpl=avg_cpl,
        candidate_ads_to_pause=candidates,
    )
