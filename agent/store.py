"""Archivio Supabase dei refresh prodotti dal funnel-refresher-agent.

Riusa la tabella condivisa `agent_outputs`:
    agent_type = 'refresher'
    subtype    = 'refresh_run'
    title      = "<campaign_id> · <YYYY-MM-DD HH:MM>"
    payload    = { config (no secrets), briefing, diagnosis, angles,
                   chosen_angle_idx, creatives (no image bytes), approvals,
                   launch_result, observations }

Le immagini dei creative NON sono persistite (bytes troppo pesanti per JSON).
Sono comunque su Meta dopo il launch; l'archivio è per ricordare cosa è stato
fatto, non per riprodurre i visual.

Env vars: SUPABASE_URL + SUPABASE_SECRET_KEY (o SUPABASE_SERVICE_KEY).
`RefreshStore.from_env()` ritorna None se mancano.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import requests

_TABLE = "agent_outputs"
_AGENT_TYPE = "refresher"
_SUBTYPE = "refresh_run"

_SECRET_KEYS = {"meta_token", "meta_access_token", "access_token", "anthropic_api_key", "openai_api_key"}


def _strip_secrets(d: dict[str, Any] | None) -> dict[str, Any]:
    if not d:
        return {}
    return {k: v for k, v in d.items() if k not in _SECRET_KEYS}


@dataclass(frozen=True)
class RefreshRow:
    id: str
    title: str
    payload: dict[str, Any]
    created_at: str


class RefreshStore:
    def __init__(self, url: str, secret_key: str) -> None:
        if not url or not secret_key:
            raise ValueError("SUPABASE_URL e SUPABASE_SECRET_KEY obbligatori")
        self.url = url.rstrip("/")
        self.secret_key = secret_key
        self._rest = f"{self.url}/rest/v1"
        self._h_read = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }
        self._h_write = {
            **self._h_read,
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    @classmethod
    def from_env(cls) -> "RefreshStore | None":
        try:
            import streamlit as st
            url = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
            key = (
                os.getenv("SUPABASE_SECRET_KEY")
                or os.getenv("SUPABASE_SERVICE_KEY")
                or st.secrets.get("SUPABASE_SECRET_KEY", "")
                or st.secrets.get("SUPABASE_SERVICE_KEY", "")
            )
        except Exception:
            url = os.getenv("SUPABASE_URL", "")
            key = (
                os.getenv("SUPABASE_SECRET_KEY", "")
                or os.getenv("SUPABASE_SERVICE_KEY", "")
            )
        if not url or not key:
            return None
        return cls(url=url, secret_key=key)

    @staticmethod
    def _row_to_refresh(row: dict[str, Any]) -> RefreshRow:
        return RefreshRow(
            id=str(row["id"]),
            title=row.get("title", "") or "(senza titolo)",
            payload=row.get("payload") or {},
            created_at=row.get("created_at", ""),
        )

    @staticmethod
    def _serialize_creatives(creatives: list[Any] | None) -> list[dict[str, Any]]:
        """Serializza Creative senza image_bytes (sostituisce con size_kb)."""
        out: list[dict[str, Any]] = []
        for c in creatives or []:
            if isinstance(c, dict):
                d = {k: v for k, v in c.items() if k != "image_bytes"}
                if (b := c.get("image_bytes")):
                    d["image_size_kb"] = len(b) // 1024
                out.append(d)
            else:
                d = {
                    "slug": getattr(c, "slug", ""),
                    "headline": getattr(c, "headline", ""),
                    "body": getattr(c, "body", ""),
                    "image_prompt": getattr(c, "image_prompt", ""),
                    "image_mime": getattr(c, "image_mime", ""),
                }
                if (b := getattr(c, "image_bytes", b"")):
                    d["image_size_kb"] = len(b) // 1024
                out.append(d)
        return out

    @staticmethod
    def _serialize_dataclass(obj: Any) -> Any:
        if obj is None:
            return None
        if isinstance(obj, (dict, list, str, int, float, bool)):
            return obj
        try:
            from dataclasses import asdict, is_dataclass
            if is_dataclass(obj):
                return asdict(obj)
        except Exception:
            pass
        return str(obj)

    def save_refresh(
        self,
        *,
        config: dict[str, Any] | None,
        briefing: dict[str, Any] | None,
        diagnosis: Any,
        observations: str,
        angles: list[Any] | None,
        chosen_angle_idx: int | None,
        creatives: list[Any] | None,
        approvals: list[bool] | None,
        launch_result: Any,
    ) -> RefreshRow:
        from datetime import datetime as _dt
        cfg = _strip_secrets(config)
        camp_id = cfg.get("campaign_id") or "?"
        title = f"{camp_id} · {_dt.utcnow().strftime('%Y-%m-%d %H:%M')}"[:200]
        lr = self._serialize_dataclass(launch_result) or {}
        body = {
            "agent_type": _AGENT_TYPE,
            "subtype": _SUBTYPE,
            "title": title,
            "payload": {
                "config": cfg,
                "briefing": briefing or {},
                "diagnosis": self._serialize_dataclass(diagnosis),
                "observations": observations or "",
                "angles": [self._serialize_dataclass(a) for a in (angles or [])],
                "chosen_angle_idx": chosen_angle_idx,
                "creatives": self._serialize_creatives(creatives),
                "approvals": list(approvals or []),
                "launch_result": lr,
            },
            "preview": (observations or briefing.get("project_context", "") if briefing else "")[:500],
            "metadata": {
                "campaign_id": camp_id,
                "meta_account": cfg.get("meta_account"),
                "created_count": len((lr or {}).get("created", []) or []),
                "paused_count": len((lr or {}).get("paused", []) or []),
            },
        }
        r = requests.post(
            f"{self._rest}/{_TABLE}",
            data=json.dumps(body, default=str),
            headers=self._h_write,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Insert refresh fallito {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"Risposta inattesa: {data!r}")
        return self._row_to_refresh(data[0])

    def list_recent(self, limit: int = 30) -> list[RefreshRow]:
        r = requests.get(
            f"{self._rest}/{_TABLE}",
            params={
                "select": "*",
                "agent_type": f"eq.{_AGENT_TYPE}",
                "subtype": f"eq.{_SUBTYPE}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            headers=self._h_read,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"List refresh fallito {r.status_code}: {r.text[:300]}")
        rows = r.json() or []
        return [self._row_to_refresh(row) for row in rows]

    def get(self, refresh_id: str) -> RefreshRow | None:
        r = requests.get(
            f"{self._rest}/{_TABLE}",
            params={"select": "*", "id": f"eq.{refresh_id}", "limit": "1"},
            headers=self._h_read,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Get refresh fallito {r.status_code}: {r.text[:300]}")
        rows = r.json() or []
        if not rows:
            return None
        return self._row_to_refresh(rows[0])

    def delete(self, refresh_id: str) -> None:
        r = requests.delete(
            f"{self._rest}/{_TABLE}",
            params={"id": f"eq.{refresh_id}"},
            headers=self._h_read,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Delete refresh fallito {r.status_code}: {r.text[:300]}")
