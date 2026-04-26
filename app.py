"""Funnel Refresher Agent — Streamlit multi-step UI.

Flow:
  1. Onboarding (sidebar form) → captures Meta + HubSpot creds + campaign details
  2. Diagnosis  → real CPL per referral, recommendations
  3. Angles     → Claude proposes 3–5 new angles, operator picks one
  4. Creatives  → Claude writes copy + gpt-image-1 generates images
  5. Approval   → operator approves which to pause + which to launch
  6. Launch     → executes pause + create on Meta
"""
from __future__ import annotations

import os
import traceback
from dataclasses import asdict

import streamlit as st
from dotenv import load_dotenv

from agent.angles import Angle, propose_angles
from agent.diagnose import DiagnoseReport, run_diagnosis
from agent.generate import Creative, generate_creatives
from agent.hubspot_api import HubSpotClient, HubSpotError
from agent.launch import launch_refresh
from agent.meta_api import MetaClient, MetaError


# ── Config ─────────────────────────────────────────────────────────
load_dotenv()


def _secret(key: str, default: str = "") -> str:
    """Read from env first, then st.secrets (Streamlit Cloud)."""
    val = os.getenv(key)
    if val:
        return val
    try:
        return st.secrets.get(key, default)
    except (FileNotFoundError, AttributeError):
        return default


ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY")
OPENAI_API_KEY = _secret("OPENAI_API_KEY")
APP_PASSWORD = _secret("APP_PASSWORD")

st.set_page_config(page_title="Funnel Refresher Agent", layout="wide", page_icon="🎯")


# ── Password gate (optional) ───────────────────────────────────────
def _password_gate() -> None:
    if not APP_PASSWORD:
        return
    if st.session_state.get("authed"):
        return
    st.title("Funnel Refresher Agent")
    pw = st.text_input("Password", type="password", key="pw_input")
    if st.button("Enter"):
        if pw == APP_PASSWORD:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()


_password_gate()


# ── State helpers ──────────────────────────────────────────────────
DEFAULT_STATE = {
    "step": "onboarding",
    "config": None,
    "diagnosis": None,
    "angles": None,
    "chosen_angle_idx": None,
    "creatives": None,
    "launch_result": None,
    "error": None,
}
for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v


def _set_step(new_step: str) -> None:
    st.session_state.step = new_step
    st.session_state.error = None


def _show_error_if_any() -> None:
    err = st.session_state.get("error")
    if err:
        st.error(err)


# ── Sidebar: onboarding form ──────────────────────────────────────
def _onboarding_sidebar() -> None:
    st.sidebar.header("⚙️ Setup")
    if not ANTHROPIC_API_KEY or not OPENAI_API_KEY:
        st.sidebar.error(
            "Backend keys missing. Set `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` "
            "in `.env` (local) or Streamlit Cloud Secrets."
        )

    cfg = st.session_state.config or {}
    with st.sidebar.form("onboarding"):
        meta_account = st.text_input(
            "Meta Ad Account ID", value=cfg.get("meta_account", ""),
            placeholder="act_191279579779492",
        )
        meta_token = st.text_input(
            "Meta System User Token", value=cfg.get("meta_token", ""),
            type="password",
        )
        hs_token = st.text_input(
            "HubSpot Private App Token", value=cfg.get("hs_token", ""),
            type="password",
        )
        campaign_id = st.text_input(
            "Meta Campaign ID", value=cfg.get("campaign_id", ""),
            placeholder="120238282072610614",
        )
        page_id = st.text_input(
            "Meta Page ID", value=cfg.get("page_id", ""),
            placeholder="1615189335210525",
        )
        ig_user_id = st.text_input(
            "Instagram User ID", value=cfg.get("ig_user_id", ""),
            placeholder="17841401741132064",
        )
        landing_url = st.text_input(
            "Landing URL", value=cfg.get("landing_url", ""),
            placeholder="https://yourbrand.com/your-offer/",
        )
        form_id = st.text_input(
            "HubSpot Form ID", value=cfg.get("form_id", ""),
            placeholder="2a842b8e-9836-4ab1-84fd-db7e691d0c09",
        )
        target_audience = st.text_area(
            "Target audience (1 sentence)", value=cfg.get("target_audience", ""),
            placeholder="Imprenditori italiani 35-55 senza background tecnico",
            height=70,
        )
        brand_voice = st.text_area(
            "Brand voice (1 sentence)", value=cfg.get("brand_voice", ""),
            placeholder="Diretto, pragmatico, parla italiano semplice, no anglicismi",
            height=70,
        )
        days = st.slider("Lookback days", 7, 30, value=cfg.get("days", 14))
        submitted = st.form_submit_button("Save & continue", use_container_width=True)

    if submitted:
        required = {
            "Meta Account": meta_account,
            "Meta Token": meta_token,
            "HubSpot Token": hs_token,
            "Campaign ID": campaign_id,
            "Page ID": page_id,
            "IG User ID": ig_user_id,
            "Landing URL": landing_url,
            "Form ID": form_id,
            "Target audience": target_audience,
            "Brand voice": brand_voice,
        }
        missing = [k for k, v in required.items() if not str(v).strip()]
        if missing:
            st.sidebar.error(f"Missing: {', '.join(missing)}")
        else:
            st.session_state.config = {
                "meta_account": meta_account.strip(),
                "meta_token": meta_token.strip(),
                "hs_token": hs_token.strip(),
                "campaign_id": campaign_id.strip(),
                "page_id": page_id.strip(),
                "ig_user_id": ig_user_id.strip(),
                "landing_url": landing_url.strip(),
                "form_id": form_id.strip(),
                "target_audience": target_audience.strip(),
                "brand_voice": brand_voice.strip(),
                "days": days,
            }
            if st.session_state.step == "onboarding":
                _set_step("diagnosis")
            st.rerun()

    if st.sidebar.button("🔄 Reset session", use_container_width=True):
        for k in DEFAULT_STATE:
            st.session_state[k] = DEFAULT_STATE[k]
        st.rerun()


# ── Step UIs ───────────────────────────────────────────────────────
def _step_onboarding() -> None:
    st.title("🎯 Funnel Refresher Agent")
    st.markdown(
        "Compila il **form a sinistra** con le credenziali Meta + HubSpot del cliente "
        "e i dettagli della campagna da rinfrescare. Poi premi **Save & continue**."
    )
    st.info(
        "💡 Le credenziali restano solo nella memoria della sessione (`st.session_state`), "
        "non vengono mai persistite su disco né su DB in questa versione."
    )


def _step_diagnosis() -> None:
    cfg = st.session_state.config
    st.title("Step 1 · Diagnosi")
    st.caption(
        f"Campagna `{cfg['campaign_id']}` • account `{cfg['meta_account']}` • "
        f"finestra ultimi {cfg['days']} giorni"
    )

    if st.session_state.diagnosis is None:
        if st.button("🔍 Run diagnosis", type="primary"):
            with st.spinner("Pulling Meta ads, insights e HubSpot submissions…"):
                try:
                    meta = MetaClient(cfg["meta_token"], cfg["meta_account"])
                    hs = HubSpotClient(cfg["hs_token"])
                    report = run_diagnosis(
                        meta=meta,
                        hubspot=hs,
                        campaign_id=cfg["campaign_id"],
                        form_id=cfg["form_id"],
                        days=cfg["days"],
                    )
                    st.session_state.diagnosis = report
                    st.rerun()
                except (MetaError, HubSpotError, ValueError) as e:
                    st.session_state.error = f"Diagnosis failed: {e}"
                except Exception as e:
                    st.session_state.error = f"Unexpected error: {e}\n\n{traceback.format_exc()}"
        return

    report: DiagnoseReport = st.session_state.diagnosis
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spesa", f"€{report.total_spend:,.2f}")
    c2.metric("Lead reali (HubSpot)", report.total_real_leads)
    c3.metric("CPL medio", f"€{report.avg_real_cpl:,.2f}" if report.avg_real_cpl else "—")
    c4.metric("Da spegnere (suggerito)", len(report.candidate_ads_to_pause))

    st.subheader("📈 Per referral")
    st.dataframe(
        [
            {
                "referral": r.referral,
                "spend €": round(r.spend, 2),
                "clicks": r.clicks,
                "lead HS": r.real_leads,
                "CPL reale €": round(r.real_cpl, 2) if r.real_cpl is not None else None,
            }
            for r in report.referrals
        ],
        use_container_width=True,
    )

    st.subheader("📋 Per ad")
    st.dataframe(
        [
            {
                "name": a.name,
                "status": a.status,
                "referral": a.referral,
                "spend €": round(a.spend, 2),
                "ctr %": round(a.ctr, 2),
                "lead HS": a.real_leads,
                "CPL €": round(a.real_cpl, 2) if a.real_cpl is not None else None,
                "reco": a.recommendation,
            }
            for a in report.ads
        ],
        use_container_width=True,
    )

    if st.button("➡️ Propose new angles", type="primary"):
        _set_step("angles")
        st.rerun()


def _step_angles() -> None:
    st.title("Step 2 · Nuovi angoli")
    cfg = st.session_state.config
    report: DiagnoseReport = st.session_state.diagnosis

    if st.session_state.angles is None:
        extra = st.text_area(
            "Note aggiuntive (opzionali) — es. 'evita angoli tecnici, parla a chi non sa cos'è un funnel'",
            value="",
            height=70,
        )
        if st.button("✨ Generate angles", type="primary"):
            with st.spinner("Claude sta proponendo nuovi angoli…"):
                try:
                    angles = propose_angles(
                        api_key=ANTHROPIC_API_KEY,
                        diagnosis=report,
                        target_audience=cfg["target_audience"],
                        brand_voice=cfg["brand_voice"],
                        extra_instructions=extra,
                    )
                    st.session_state.angles = angles
                    st.rerun()
                except Exception as e:
                    st.session_state.error = f"Angle generation failed: {e}"
        return

    angles: list[Angle] = st.session_state.angles
    st.markdown("Scegli **un angolo**: l'agente genererà 6 varianti creative su quello.")
    options = [f"{i + 1}. {a.title}" for i, a in enumerate(angles)]
    chosen_label = st.radio("Angoli proposti", options, index=0)
    chosen_idx = options.index(chosen_label)
    st.session_state.chosen_angle_idx = chosen_idx
    angle = angles[chosen_idx]

    with st.expander("Dettaglio angolo selezionato", expanded=True):
        st.markdown(f"**Rationale**: {angle.rationale}")
        st.markdown(f"**Pain attaccato**: {angle.target_pain}")
        st.markdown(f"**Promessa**: {angle.promise}")

    cols = st.columns([1, 1, 4])
    if cols[0].button("⬅️ Diagnosis"):
        _set_step("diagnosis")
        st.rerun()
    if cols[1].button("🔁 Re-generate"):
        st.session_state.angles = None
        st.rerun()
    if cols[2].button("➡️ Generate creatives", type="primary"):
        _set_step("creatives")
        st.rerun()


def _step_creatives() -> None:
    st.title("Step 3 · Generazione creative")
    cfg = st.session_state.config
    angles: list[Angle] = st.session_state.angles
    angle = angles[st.session_state.chosen_angle_idx]

    if st.session_state.creatives is None:
        n_variants = st.slider("Quante varianti?", 3, 10, 6)
        quality = st.selectbox(
            "Qualità immagine (gpt-image-1)",
            ["high", "medium", "low"],
            index=0,
            help="high ≈ €0.25/img, medium ≈ €0.07/img, low ≈ €0.02/img",
        )
        if st.button("🎨 Generate creatives", type="primary"):
            with st.spinner(
                f"Generando {n_variants} copy con Claude e {n_variants} immagini con gpt-image-1… "
                f"(può richiedere 1-2 min)"
            ):
                try:
                    creatives = generate_creatives(
                        anthropic_api_key=ANTHROPIC_API_KEY,
                        openai_api_key=OPENAI_API_KEY,
                        angle=angle,
                        target_audience=cfg["target_audience"],
                        brand_voice=cfg["brand_voice"],
                        n_variants=n_variants,
                        image_quality=quality,
                    )
                    st.session_state.creatives = creatives
                    # init approvals to all True
                    st.session_state.approvals = [True] * len(creatives)
                    st.rerun()
                except Exception as e:
                    st.session_state.error = f"Creative generation failed: {e}"
        return

    creatives: list[Creative] = st.session_state.creatives
    st.markdown(f"Generati **{len(creatives)} variants** sull'angolo *{angle.title}*. Approva quelle che vuoi lanciare.")

    if "approvals" not in st.session_state or len(st.session_state.approvals) != len(creatives):
        st.session_state.approvals = [True] * len(creatives)

    for i, c in enumerate(creatives):
        with st.container(border=True):
            cols = st.columns([1, 2])
            with cols[0]:
                st.image(c.image_bytes, use_container_width=True)
            with cols[1]:
                st.markdown(f"**Headline**: {c.headline}")
                st.markdown("**Body**:")
                st.code(c.body, language=None)
                st.caption(f"slug: `{c.slug}`")
                with st.expander("Image prompt"):
                    st.text(c.image_prompt)
            st.session_state.approvals[i] = st.checkbox(
                f"✅ Approva variante {i + 1}",
                value=st.session_state.approvals[i],
                key=f"approve_{i}",
            )

    cols = st.columns([1, 1, 1, 3])
    if cols[0].button("⬅️ Angles"):
        _set_step("angles")
        st.rerun()
    if cols[1].button("🔁 Re-generate"):
        st.session_state.creatives = None
        st.session_state.approvals = []
        st.rerun()
    n_approved = sum(st.session_state.approvals)
    if cols[2].button(
        f"➡️ Review launch ({n_approved})",
        type="primary",
        disabled=n_approved == 0,
    ):
        _set_step("launch")
        st.rerun()


def _step_launch() -> None:
    st.title("Step 4 · Approvazione finale e lancio")
    cfg = st.session_state.config
    report: DiagnoseReport = st.session_state.diagnosis
    creatives: list[Creative] = st.session_state.creatives
    approvals: list[bool] = st.session_state.approvals

    approved_creatives = [c for c, ok in zip(creatives, approvals) if ok]

    st.subheader("🛑 Ads da spegnere")
    pause_options = {
        a.ad_id: f"{a.name} ({a.referral}) · spend €{a.spend:.2f} · CPL "
        f"{('€%.2f' % a.real_cpl) if a.real_cpl else '—'}"
        for a in report.ads
        if a.status == "ACTIVE"
    }
    pause_default = [
        a.ad_id
        for a in report.ads
        if a.status == "ACTIVE" and a.recommendation == "pause"
    ]
    selected_pause = st.multiselect(
        "Seleziona quali pausare",
        options=list(pause_options.keys()),
        default=pause_default,
        format_func=lambda ad_id: pause_options[ad_id],
    )

    st.subheader("🚀 Ads da lanciare")
    st.markdown(
        f"**{len(approved_creatives)}** variants approvate. Saranno create nello stesso "
        f"adset attivo della campagna con tracking `?referral=refresh_N`."
    )
    for i, c in enumerate(approved_creatives):
        st.markdown(f"  {i + 1}. **{c.headline}** · slug `{c.slug}`")

    st.divider()
    st.warning(
        "⚠️ Una volta cliccato **Launch** le Ads vengono create su Meta e i loser pausati. "
        "Operazione non reversibile da questa UI (puoi sempre rimettere ACTIVE da Meta Ads Manager)."
    )

    cols = st.columns([1, 1, 3])
    if cols[0].button("⬅️ Back"):
        _set_step("creatives")
        st.rerun()
    if cols[1].button("🚀 Launch", type="primary", disabled=len(approved_creatives) == 0):
        with st.spinner("Lancio in corso… (uplodo immagini, creo creatives, creo ads)"):
            try:
                meta = MetaClient(cfg["meta_token"], cfg["meta_account"])
                result = launch_refresh(
                    meta=meta,
                    campaign_id=cfg["campaign_id"],
                    ads_to_pause=selected_pause,
                    creatives_to_launch=approved_creatives,
                    landing_url=cfg["landing_url"],
                    page_id=cfg["page_id"],
                    instagram_user_id=cfg["ig_user_id"],
                )
                st.session_state.launch_result = result
                _set_step("done")
                st.rerun()
            except Exception as e:
                st.session_state.error = f"Launch failed: {e}\n\n{traceback.format_exc()}"


def _step_done() -> None:
    st.title("✅ Refresh completato")
    result = st.session_state.launch_result
    st.success(f"Pausate: {len(result.paused)} · Create: {len(result.created)}")

    st.subheader("Pausate")
    st.code("\n".join(result.paused) or "(nessuna)")

    st.subheader("Nuove ads create")
    st.dataframe(result.created, use_container_width=True)

    st.info(
        "Le ads sono state create in stato ACTIVE. Meta tipicamente impiega 5–30 minuti "
        "per l'approvazione. Verifica lo status su Ads Manager o rilancia la diagnosi tra qualche ora."
    )
    if st.button("🔄 New refresh (same client)"):
        for k in ("diagnosis", "angles", "chosen_angle_idx", "creatives", "approvals", "launch_result"):
            st.session_state[k] = DEFAULT_STATE.get(k)
        _set_step("diagnosis")
        st.rerun()


# ── Render ────────────────────────────────────────────────────────
_onboarding_sidebar()
_show_error_if_any()

step = st.session_state.step
if st.session_state.config is None and step != "onboarding":
    _set_step("onboarding")
    step = "onboarding"

if step == "onboarding":
    _step_onboarding()
elif step == "diagnosis":
    _step_diagnosis()
elif step == "angles":
    _step_angles()
elif step == "creatives":
    _step_creatives()
elif step == "launch":
    _step_launch()
elif step == "done":
    _step_done()
