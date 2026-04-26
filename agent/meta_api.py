"""Meta Graph API wrapper for the funnel refresher.

Reuses the proven request patterns from `meta-ads-analyzer/check_*` and `refresh_*`
scripts, but exposes them as a single class with typed results.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

GRAPH = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True)
class AdInfo:
    ad_id: str
    name: str
    status: str
    effective_status: str
    adset_id: str
    adset_name: str
    referral: str
    landing_link: str


@dataclass(frozen=True)
class AdInsights:
    ad_id: str
    spend: float
    impressions: int
    clicks: int
    ctr: float
    meta_leads: int
    meta_cpl: float


def _referral_from_url(link: str) -> str:
    if not link or "referral=" not in link:
        return "direct"
    try:
        return parse_qs(urlparse(link).query).get("referral", ["direct"])[0]
    except Exception:
        return "direct"


def _extract_lead_metrics(insight: dict[str, Any]) -> tuple[int, float]:
    leads = 0
    cpl = 0.0
    lead_actions = ("lead", "offsite_conversion.fb_pixel_lead", "onsite_web_lead")
    for a in insight.get("actions", []):
        if a["action_type"] in lead_actions:
            leads += int(a["value"])
    for c in insight.get("cost_per_action_type", []):
        if c["action_type"] in lead_actions:
            cpl = float(c["value"])
            break
    return leads, cpl


class MetaError(RuntimeError):
    """Raised when the Graph API returns an error response."""


class MetaClient:
    def __init__(self, access_token: str, ad_account_id: str) -> None:
        if not access_token:
            raise ValueError("Meta access_token is required")
        if not ad_account_id.startswith("act_"):
            raise ValueError("ad_account_id must start with 'act_'")
        self.token = access_token
        self.account = ad_account_id

    # ── low-level helpers ────────────────────────────────────────────
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p: dict[str, Any] = {"access_token": self.token, **(params or {})}
        r = requests.get(f"{GRAPH}/{endpoint}", params=p, timeout=30)
        body = r.json()
        if "error" in body:
            raise MetaError(f"GET {endpoint}: {body['error']}")
        return body

    def _post(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        body = {"access_token": self.token, **data}
        r = requests.post(f"{GRAPH}/{endpoint}", data=body, timeout=30)
        resp = r.json()
        if "error" in resp:
            raise MetaError(f"POST {endpoint}: {resp['error']}")
        return resp

    # ── reads ────────────────────────────────────────────────────────
    def list_adsets(self, campaign_id: str) -> list[dict[str, Any]]:
        return self._get(
            f"{campaign_id}/adsets",
            {"fields": "id,name,status,effective_status", "limit": 50},
        ).get("data", [])

    def list_ads(self, campaign_id: str) -> list[AdInfo]:
        ads: list[AdInfo] = []
        for adset in self.list_adsets(campaign_id):
            raw = self._get(
                f"{adset['id']}/ads",
                {
                    "fields": "id,name,status,effective_status,creative{object_story_spec}",
                    "limit": 100,
                },
            ).get("data", [])
            for ad in raw:
                spec = (ad.get("creative") or {}).get("object_story_spec") or {}
                link_data = spec.get("link_data") or spec.get("video_data") or {}
                link = link_data.get("link", "")
                ads.append(
                    AdInfo(
                        ad_id=ad["id"],
                        name=ad.get("name", ""),
                        status=ad.get("status", ""),
                        effective_status=ad.get("effective_status", ""),
                        adset_id=adset["id"],
                        adset_name=adset.get("name", ""),
                        referral=_referral_from_url(link),
                        landing_link=link,
                    )
                )
        return ads

    def get_insights(self, ad_id: str, since: str, until: str) -> AdInsights:
        data = self._get(
            f"{ad_id}/insights",
            {
                "fields": "spend,impressions,clicks,ctr,actions,cost_per_action_type",
                "time_range": json.dumps({"since": since, "until": until}),
            },
        ).get("data", [])
        if not data:
            return AdInsights(ad_id, 0.0, 0, 0, 0.0, 0, 0.0)
        ins = data[0]
        leads, cpl = _extract_lead_metrics(ins)
        return AdInsights(
            ad_id=ad_id,
            spend=float(ins.get("spend", 0)),
            impressions=int(ins.get("impressions", 0)),
            clicks=int(ins.get("clicks", 0)),
            ctr=float(ins.get("ctr", 0)),
            meta_leads=leads,
            meta_cpl=cpl,
        )

    def find_active_adset(self, campaign_id: str) -> str:
        adsets = self.list_adsets(campaign_id)
        actives = [a for a in adsets if a.get("effective_status") == "ACTIVE"]
        if not actives:
            raise MetaError(
                f"No active adset found in campaign {campaign_id}. "
                f"All adsets: {[(a['name'], a['effective_status']) for a in adsets]}"
            )
        return actives[0]["id"]

    def get_adset_full(self, adset_id: str) -> dict[str, Any]:
        """Fetch all fields needed to clone an adset (targeting, optimization, etc.)."""
        fields = [
            "name",
            "campaign_id",
            "daily_budget",
            "billing_event",
            "optimization_goal",
            "bid_strategy",
            "promoted_object",
            "targeting",
            "destination_type",
            "status",
            "effective_status",
        ]
        return self._get(adset_id, {"fields": ",".join(fields)})

    def create_adset(
        self,
        *,
        campaign_id: str,
        name: str,
        daily_budget_cents: int,
        billing_event: str,
        optimization_goal: str,
        bid_strategy: str,
        promoted_object: dict[str, Any],
        targeting: dict[str, Any],
        start_time: str | None = None,
        status: str = "ACTIVE",
        destination_type: str | None = None,
    ) -> str:
        """Create a new adset. Returns the new adset_id."""
        data: dict[str, Any] = {
            "campaign_id": campaign_id,
            "name": name,
            "daily_budget": str(daily_budget_cents),
            "billing_event": billing_event,
            "optimization_goal": optimization_goal,
            "bid_strategy": bid_strategy,
            "promoted_object": json.dumps(promoted_object),
            "targeting": json.dumps(targeting),
            "status": status,
        }
        if start_time:
            data["start_time"] = start_time
        if destination_type:
            data["destination_type"] = destination_type
        r = self._post(f"{self.account}/adsets", data)
        return r["id"]

    # ── writes ───────────────────────────────────────────────────────
    def pause_ad(self, ad_id: str) -> None:
        self._post(ad_id, {"status": "PAUSED"})

    def upload_image_bytes(self, image_bytes: bytes, filename: str = "image.png") -> str:
        """Upload raw bytes to /adimages and return the Meta image_hash."""
        r = requests.post(
            f"{GRAPH}/{self.account}/adimages",
            files={"file": (filename, image_bytes, "image/png")},
            data={"access_token": self.token},
            timeout=60,
        )
        body = r.json()
        if "images" not in body:
            raise MetaError(f"Upload failed: {body}")
        return list(body["images"].values())[0]["hash"]

    def create_ad(
        self,
        *,
        adset_id: str,
        ad_name: str,
        page_id: str,
        instagram_user_id: str,
        landing_url: str,
        image_hash: str,
        headline: str,
        body: str,
        cta_type: str = "LEARN_MORE",
        creative_label: str | None = None,
        status: str = "ACTIVE",
    ) -> dict[str, str]:
        """Create a creative + ad. Returns {'ad_id', 'creative_id'}."""
        if status not in ("ACTIVE", "PAUSED"):
            raise ValueError(f"status must be ACTIVE or PAUSED, got {status!r}")
        object_story_spec = {
            "page_id": page_id,
            "instagram_user_id": instagram_user_id,
            "link_data": {
                "link": landing_url,
                "image_hash": image_hash,
                "name": headline,
                "message": body,
                "description": "",
                "call_to_action": {"type": cta_type, "value": {"link": landing_url}},
            },
        }
        creative = self._post(
            f"{self.account}/adcreatives",
            {
                "name": creative_label or f"Refresh — {ad_name}",
                "object_story_spec": json.dumps(object_story_spec),
            },
        )
        ad = self._post(
            f"{self.account}/ads",
            {
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative["id"]}),
                "name": ad_name,
                "status": status,
            },
        )
        return {"ad_id": ad["id"], "creative_id": creative["id"]}
