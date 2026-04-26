"""HubSpot API wrapper — pulls Form Submissions to count *real* leads.

The form-integrations endpoint returns one record per submission with `submittedAt`
(epoch ms) and `pageUrl`. We use `pageUrl`'s `?referral=` parameter as the join key
against Meta ad-level spend.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests


@dataclass(frozen=True)
class Submission:
    submitted_at_ms: int
    page_url: str
    referral: str


def _referral_from_url(url: str) -> str:
    if not url or "referral=" not in url:
        return "direct"
    try:
        return parse_qs(urlparse(url).query).get("referral", ["direct"])[0]
    except Exception:
        return "direct"


class HubSpotError(RuntimeError):
    """Raised when the HubSpot API returns a non-200 response."""


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("HubSpot access_token is required")
        self.token = access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def get_form_submissions(
        self,
        form_id: str,
        since_ms: int,
        until_ms: int,
        max_pages: int = 200,
    ) -> list[Submission]:
        """Return submissions whose `submittedAt` falls in [since_ms, until_ms]."""
        results: list[Submission] = []
        after: str | None = None
        pages = 0
        base = f"https://api.hubapi.com/form-integrations/v1/submissions/forms/{form_id}"

        while pages < max_pages:
            url = f"{base}?limit=50"
            if after:
                url += f"&after={after}"
            r = requests.get(url, headers=self._headers(), timeout=30)
            if r.status_code != 200:
                raise HubSpotError(f"GET {url} → {r.status_code}: {r.text[:200]}")
            payload = r.json()
            page = payload.get("results", [])
            if not page:
                break

            stop_paging = False
            for sub in page:
                ts = int(sub.get("submittedAt", 0))
                if ts < since_ms:
                    stop_paging = True
                    break
                if ts > until_ms:
                    continue
                page_url = sub.get("pageUrl", "")
                results.append(
                    Submission(
                        submitted_at_ms=ts,
                        page_url=page_url,
                        referral=_referral_from_url(page_url),
                    )
                )

            if stop_paging:
                break
            after = (payload.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
            pages += 1
            time.sleep(0.05)  # be polite

        return results
