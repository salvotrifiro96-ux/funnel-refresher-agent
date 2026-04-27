"""Usage logging via Google Apps Script webhook — fire-and-forget event tracking.

Sends each user interaction to a Google Sheet so the operator can see who is
using the app, which campaigns they touch, and how far they go in the flow.
All errors are swallowed: logging must never break the UI.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import requests

_log = logging.getLogger(__name__)

_TIMEOUT_SEC = 3.0


def _get_secret(key: str) -> str:
    """Read from env first, then Streamlit secrets if available."""
    val = os.getenv(key)
    if val:
        return val
    try:
        import streamlit as st

        try:
            return st.secrets.get(key, "")
        except (FileNotFoundError, AttributeError):
            return ""
    except ImportError:
        return ""


def _config() -> tuple[str, str] | None:
    """Return (webhook_url, secret) if configured, else None."""
    url = _get_secret("USAGE_LOG_WEBHOOK_URL")
    secret = _get_secret("USAGE_LOG_WEBHOOK_SECRET")
    if not url or not secret:
        return None
    return url, secret


def ensure_schema() -> None:
    """Kept for API compatibility. Apps Script creates the header row on first write."""
    return None


def get_session_id() -> str:
    """Return a stable per-session UUID, creating it in st.session_state on first call."""
    try:
        import streamlit as st

        if "_usage_session_id" not in st.session_state:
            st.session_state["_usage_session_id"] = str(uuid.uuid4())
        return st.session_state["_usage_session_id"]
    except ImportError:
        return str(uuid.uuid4())


def log_event(
    event: str,
    *,
    meta_account: str | None = None,
    campaign_id: str | None = None,
    landing_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """POST a single usage event to the Google Sheet webhook. Never raises."""
    cfg = _config()
    if cfg is None:
        return
    url, secret = cfg

    body = {
        "secret": secret,
        "session_id": get_session_id(),
        "event": event,
        "meta_account": meta_account or "",
        "campaign_id": campaign_id or "",
        "landing_url": landing_url or "",
        "payload": _scrub(payload or {}),
    }

    try:
        requests.post(url, json=body, timeout=_TIMEOUT_SEC)
    except Exception as exc:
        _log.warning("usage_log: webhook post failed (event=%s): %s", event, exc)


_SECRET_KEYS = {"meta_token", "token", "api_key", "password", "secret"}


def _scrub(data: dict[str, Any]) -> dict[str, Any]:
    """Drop secret-looking keys from payload before sending."""
    clean: dict[str, Any] = {}
    for k, v in data.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            clean[k] = v
        elif isinstance(v, dict):
            clean[k] = _scrub(v)
        elif isinstance(v, (list, tuple)):
            clean[k] = [_scrub(x) if isinstance(x, dict) else x for x in v]
        else:
            try:
                json.dumps(v)
                clean[k] = v
            except (TypeError, ValueError):
                clean[k] = str(v)
    return clean
