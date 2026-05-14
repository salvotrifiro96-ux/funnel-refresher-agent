"""Cross-app: client minimale verso le tabelle orchestrator_* per
permettere agli agenti standalone di "approvare output per un progetto".

Le tabelle (su Supabase project condiviso del team Leone):
  orchestrator_projects        — id, name, status, context jsonb, selected_promise jsonb
  orchestrator_project_agents  — id, project_id, agent_slug, status, output jsonb, user_input jsonb, notes

Pattern d'uso negli agenti standalone:
    from agent.orchestrator_client import OrchestratorClient
    oc = OrchestratorClient.from_env()
    if oc:
        projects = oc.list_projects()         # dropdown sidebar
        project = oc.get_project(selected_id) # carica context + selected_promise
        oc.save_agent_output(
            project_id, "copy", output, status="completed",
        )
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class OrchProject:
    id: str
    name: str
    status: str
    context: dict[str, Any]
    selected_promise: dict[str, Any] | None


class OrchestratorClient:
    def __init__(self, url: str, secret_key: str) -> None:
        if not url or not secret_key:
            raise ValueError("SUPABASE_URL + SUPABASE_SECRET_KEY obbligatori")
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
    def from_env(cls) -> "OrchestratorClient | None":
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
            key = os.getenv("SUPABASE_SECRET_KEY", "") or os.getenv("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            return None
        return cls(url=url, secret_key=key)

    def list_projects(self, limit: int = 100) -> list[OrchProject]:
        r = requests.get(
            f"{self._rest}/orchestrator_projects",
            params={
                "select": "id,name,status,context,selected_promise",
                "order": "updated_at.desc",
                "limit": str(limit),
            },
            headers=self._h_read, timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"List projects: {r.status_code} {r.text[:200]}")
        return [
            OrchProject(
                id=row["id"], name=row.get("name", ""), status=row.get("status", ""),
                context=row.get("context", {}) or {},
                selected_promise=row.get("selected_promise"),
            )
            for row in (r.json() or [])
        ]

    def get_project(self, project_id: str) -> OrchProject | None:
        r = requests.get(
            f"{self._rest}/orchestrator_projects",
            params={
                "select": "id,name,status,context,selected_promise",
                "id": f"eq.{project_id}", "limit": "1",
            },
            headers=self._h_read, timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Get project: {r.status_code} {r.text[:200]}")
        rows = r.json() or []
        if not rows:
            return None
        row = rows[0]
        return OrchProject(
            id=row["id"], name=row.get("name", ""), status=row.get("status", ""),
            context=row.get("context", {}) or {},
            selected_promise=row.get("selected_promise"),
        )

    def save_agent_output(
        self,
        project_id: str,
        agent_slug: str,
        output: dict[str, Any],
        *,
        user_input: dict[str, Any] | None = None,
        status: str = "completed",
        notes: str | None = None,
    ) -> bool:
        """Aggiorna orchestrator_project_agents per il (project, agent_slug).
        Default status='completed' — l'utente sta esplicitamente approvando da qui."""
        body: dict[str, Any] = {
            "status": status,
            "output": output,
            "updated_at": "now()",
        }
        if user_input is not None:
            body["user_input"] = user_input
        if notes is not None:
            body["notes"] = notes
        r = requests.patch(
            f"{self._rest}/orchestrator_project_agents",
            params={
                "project_id": f"eq.{project_id}",
                "agent_slug": f"eq.{agent_slug}",
            },
            data=json.dumps(body),
            headers=self._h_write, timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Save agent output: {r.status_code} {r.text[:200]}")
        return True
