"""UI helpers per integrare un agente standalone con l'orchestratore.

Pattern d'uso:
    # in app.py
    from agent.orch_link import sidebar_project_picker, save_to_project_button

    sidebar_project_picker()  # mette il selettore in sidebar; popola session_state
                              # "_orch_linked_project_id" + "_orch_linked_project_context"

    # dopo aver generato un output:
    save_to_project_button(
        agent_slug="copy",
        output={"ads": [...], "channel": "meta"},
        label="🎯 Approva per progetto",
    )
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from agent.orchestrator_client import OrchestratorClient, OrchProject


SESSION_KEYS = {
    "client": "_orch_client",
    "project_id": "_orch_linked_project_id",
    "project_name": "_orch_linked_project_name",
    "project_context": "_orch_linked_project_context",
    "project_selected_promise": "_orch_linked_project_selected_promise",
}


def _client() -> OrchestratorClient | None:
    if SESSION_KEYS["client"] not in st.session_state:
        try:
            st.session_state[SESSION_KEYS["client"]] = OrchestratorClient.from_env()
        except Exception:
            st.session_state[SESSION_KEYS["client"]] = None
    return st.session_state[SESSION_KEYS["client"]]


def sidebar_project_picker(*, location: str = "sidebar") -> str | None:
    """Mostra il selettore progetto. `location='sidebar'` per metterlo in
    sidebar, qualunque altro valore lo mette nel main flow."""
    oc = _client()
    container = st.sidebar if location == "sidebar" else st
    container.divider()
    container.markdown("**🎯 Progetto Orchestrator**")
    if not oc:
        container.caption(
            "_Disabilitato: mancano SUPABASE_URL / SUPABASE_SECRET_KEY._"
        )
        return None
    try:
        projects = oc.list_projects()
    except Exception as e:
        container.caption(f"Errore lista progetti: {e}")
        return None
    active = [p for p in projects if p.status in ("active", "discovery")]
    if not active:
        container.caption("_Nessun progetto attivo. Crealo dall'orchestratore._")
        return None

    options = ["(nessuno — lavora standalone)"] + [
        f"{p.name[:50]} · {p.id[:8]}" for p in active
    ]
    # default: progetto gia` collegato in sessione, altrimenti nessuno
    current_id = st.session_state.get(SESSION_KEYS["project_id"])
    default_idx = 0
    if current_id:
        for i, p in enumerate(active):
            if p.id == current_id:
                default_idx = i + 1
                break

    selected_label = container.selectbox(
        "Lavora per",
        options=options,
        index=default_idx,
        key="_orch_picker_select",
    )
    if selected_label == options[0]:
        # nessun progetto
        for k in (
            SESSION_KEYS["project_id"],
            SESSION_KEYS["project_name"],
            SESSION_KEYS["project_context"],
            SESSION_KEYS["project_selected_promise"],
        ):
            st.session_state.pop(k, None)
        return None

    idx = options.index(selected_label) - 1
    chosen = active[idx]
    # Aggiorno sessione con dati del progetto
    st.session_state[SESSION_KEYS["project_id"]] = chosen.id
    st.session_state[SESSION_KEYS["project_name"]] = chosen.name
    st.session_state[SESSION_KEYS["project_context"]] = chosen.context
    st.session_state[SESSION_KEYS["project_selected_promise"]] = chosen.selected_promise

    container.success(f"📌 Collegato: **{chosen.name}**")
    if chosen.selected_promise:
        with container.expander("🪄 Promessa ufficiale del progetto", expanded=False):
            p = chosen.selected_promise
            if p.get("pre_headline"):
                container.caption(p["pre_headline"])
            if p.get("usp_name"):
                container.markdown(f"**{p['usp_name']}**")
            container.markdown(p.get("headline", ""))
            if p.get("sub_headline"):
                container.caption(p["sub_headline"])
    return chosen.id


def linked_project_id() -> str | None:
    return st.session_state.get(SESSION_KEYS["project_id"])


def linked_project_context() -> dict[str, Any]:
    return st.session_state.get(SESSION_KEYS["project_context"]) or {}


def linked_project_selected_promise() -> dict[str, Any] | None:
    return st.session_state.get(SESSION_KEYS["project_selected_promise"])


def save_to_project_button(
    *,
    agent_slug: str,
    output: dict[str, Any],
    user_input: dict[str, Any] | None = None,
    label: str = "🎯 Approva per progetto",
    key_suffix: str = "",
) -> bool:
    """Render il bottone solo se un progetto e` collegato. Ritorna True dopo save ok."""
    pid = linked_project_id()
    if not pid:
        return False
    btn_key = f"_orch_save_{agent_slug}_{key_suffix}"
    if not st.button(label, key=btn_key, type="primary"):
        return False
    oc = _client()
    if not oc:
        st.error("OrchestratorClient non disponibile")
        return False
    try:
        oc.save_agent_output(pid, agent_slug, output, user_input=user_input)
        st.success(
            f"✅ Output salvato per il progetto **{st.session_state.get(SESSION_KEYS['project_name'], pid[:8])}** "
            f"(agente `{agent_slug}` → status=completed)."
        )
        return True
    except Exception as e:
        st.error(f"Save fallito: {e}")
        return False
