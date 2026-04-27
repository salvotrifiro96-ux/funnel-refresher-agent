"""Funnel Refresher Agent — Streamlit multi-step UI.

Flow:
  0. Onboarding (sidebar)            → Meta creds + campaign details + briefing
  1. Diagnosis                        → CPL per referral (Meta pixel)
  2. Angles questionnaire + propose   → Claude proposes new angles
  3. Creatives questionnaire + gen    → Claude writes copy + gpt-image-1
  4. Approval (per-variant checkbox)  → operator picks variants
  5. Pre-launch questionnaire         → status, adset, scheduling, CTA, referral
  6. Done                             → summary + new ad IDs
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta

import streamlit as st
from dotenv import load_dotenv

from agent.angles import Angle, propose_angles
from agent.diagnose import DiagnoseReport, apply_lead_overrides, run_diagnosis
from agent.generate import Creative, generate_creatives, regenerate_one_variant
from agent.launch import LaunchPlan, launch_refresh
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


# ── Password gate ──────────────────────────────────────────────────
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


# ── State init ─────────────────────────────────────────────────────
DEFAULT_STATE: dict[str, object] = {
    "step": "onboarding",
    "config": None,             # credentials + campaign
    "briefing": None,           # advanced briefing answers
    "observations": "",         # post-diagnosis observations
    "diagnosis": None,          # raw, from Meta API
    "lead_overrides": {},       # {referral: int} — manual lead corrections
    "angles": None,
    "chosen_angle_idx": None,
    "creatives": None,
    "approvals": [],
    "creative_settings": None,  # n_variants, quality, text_mode, constraints (for regen)
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


def _parse_evergreen_list(raw: str) -> list[str]:
    """Parse the evergreen-referrals textarea (one per line) into a clean list."""
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _corrected_diagnosis() -> DiagnoseReport | None:
    """Return the diagnosis report with manual lead overrides applied (if any)."""
    raw = st.session_state.get("diagnosis")
    if raw is None:
        return None
    overrides = st.session_state.get("lead_overrides") or {}
    return apply_lead_overrides(raw, overrides)


# ── Sidebar: onboarding form ──────────────────────────────────────
def _onboarding_sidebar() -> None:
    st.sidebar.header("⚙️ Setup")
    if not ANTHROPIC_API_KEY or not OPENAI_API_KEY:
        st.sidebar.error(
            "Backend keys missing. Set `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` "
            "in `.env` (local) or Streamlit Cloud Secrets."
        )

    cfg = st.session_state.config or {}
    brief = st.session_state.briefing or {}

    with st.sidebar.form("onboarding"):
        meta_account = st.text_input(
            "Meta Ad Account ID",
            value=cfg.get("meta_account", ""),
            placeholder="act_191279579779492",
            help=(
                "📍 Lo trovi in **Meta Business Manager** → Business Settings → "
                "Accounts → Ad Accounts. Clicca sull'account: vedi un numero tipo "
                "`191279579779492`. Anteponi `act_` davanti → `act_191279579779492`."
            ),
        )
        meta_token = st.text_input(
            "Meta System User Token",
            value=cfg.get("meta_token", ""),
            type="password",
            help=(
                "🔑 Generalo da **Business Settings** → Users → System Users → "
                "seleziona o crea un system user → bottone **Generate New Token** → "
                "scegli l'app, durata **60 giorni** (max), e abilita questi permessi:\n\n"
                "• `ads_management`\n• `ads_read`\n• `business_management`\n• `pages_read_engagement`\n\n"
                "⚠️ Scade dopo 60 giorni: rigeneralo periodicamente."
            ),
        )
        campaign_id = st.text_input(
            "Meta Campaign ID",
            value=cfg.get("campaign_id", ""),
            placeholder="120238282072610614",
            help=(
                "🎯 In **Meta Ads Manager**, clicca sulla campagna. L'URL del browser "
                "contiene `selected_campaign_ids=XXXXXXXXX` — quel numero è l'ID. "
                "In alternativa: abilita la colonna **Campaign ID** in Ads Manager "
                "(Customize Columns)."
            ),
        )
        page_id = st.text_input(
            "Meta Page ID",
            value=cfg.get("page_id", ""),
            placeholder="1615189335210525",
            help=(
                "📘 Pagina Facebook con cui le ads vengono pubblicate. "
                "Lo trovi su **facebook.com/yourpage/about** → 'Page transparency' "
                "→ 'Page ID'. In alternativa: è già usato dalle ads esistenti "
                "della campagna che stai rinfrescando — riusa lo stesso."
            ),
        )
        ig_user_id = st.text_input(
            "Instagram User ID",
            value=cfg.get("ig_user_id", ""),
            placeholder="17841401741132064",
            help=(
                "📸 ID dell'account Instagram collegato alla pagina. "
                "Lo trovi in **Business Settings** → Accounts → Instagram Accounts → "
                "seleziona l'account → l'ID è nei dettagli/URL. Riusa lo stesso ID "
                "delle ads esistenti per coerenza di brand."
            ),
        )
        landing_url = st.text_input(
            "Landing URL",
            value=cfg.get("landing_url", ""),
            placeholder="https://yourbrand.com/your-offer/",
            help=(
                "🌐 URL completo della landing page dove le ads mandano traffico. "
                "L'agente appende automaticamente `?referral=refresh_N` per il "
                "tracking — tu metti l'URL pulito, senza parametri."
            ),
        )
        target_audience = st.text_area(
            "Target audience (1 frase)",
            value=cfg.get("target_audience", ""),
            placeholder="Imprenditori italiani 35-55 senza background tecnico",
            height=70,
            help=(
                "👥 Descrizione breve del target. Serve a Claude per proporre angoli "
                "rilevanti. Più sei specifico (età, ruolo, frustrazione tipica), "
                "più gli angoli proposti saranno mirati."
            ),
        )
        brand_voice = st.text_area(
            "Brand voice (1 frase)",
            value=cfg.get("brand_voice", ""),
            placeholder="Diretto, pragmatico, italiano semplice, no anglicismi",
            height=70,
            help="🗣 Tono di voce del brand: entra nei prompt di copywriting e angoli.",
        )
        days = st.slider(
            "Lookback days",
            7, 30,
            value=cfg.get("days", 14),
            help=(
                "📅 Quanti giorni indietro la diagnosi guarda per calcolare CTR, CPL "
                "e identificare la fatigue.\n\n"
                "• **7** → reattivo ma rumoroso\n"
                "• **14** (default) → bilanciato\n"
                "• **30** → trend più solido ma include creative pre-fatigue"
            ),
        )

        with st.expander("📝 Briefing avanzato (opzionale)", expanded=bool(brief)):
            st.caption("Opzionale ma fortemente consigliato — riduce drasticamente le 'cazzate'.")
            constraints = st.text_area(
                "Vincoli creativi — cosa NON fare",
                value=brief.get("constraints", ""),
                placeholder="Es. niente facce di donne; no mention competitor; no CTA aggressive; non parlare di prezzi",
                height=80,
                help="Viene incluso nei prompt di angoli + copy + immagini.",
            )
            deadlines = st.text_area(
                "Eventi / scadenze imminenti",
                value=brief.get("deadlines", ""),
                placeholder="Es. 'iscrizioni chiudono 15 maggio', 'sconto -50% solo questa settimana'",
                height=70,
                help="Consente all'agente di iniettare urgenza nelle copy.",
            )
            evergreen = st.text_area(
                "Ads protette — referral o nomi che NON vanno mai pausati (uno per riga)",
                value=brief.get("evergreen", ""),
                placeholder="img5\nnew_mktg_ia_2\nimg11_winner",
                height=70,
                help=(
                    "Lista referral o nomi ad da escludere automaticamente dai candidati pause. "
                    "Match esatto, case-sensitive."
                ),
            )
            free_notes = st.text_area(
                "Note libere",
                value=brief.get("free_notes", ""),
                placeholder="Qualsiasi altro contesto utile (es. 'la landing è stata cambiata 5 giorni fa')",
                height=70,
            )

        submitted = st.form_submit_button("💾 Save & continue", use_container_width=True)

    if submitted:
        required = {
            "Meta Account": meta_account,
            "Meta Token": meta_token,
            "Campaign ID": campaign_id,
            "Page ID": page_id,
            "IG User ID": ig_user_id,
            "Landing URL": landing_url,
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
                "campaign_id": campaign_id.strip(),
                "page_id": page_id.strip(),
                "ig_user_id": ig_user_id.strip(),
                "landing_url": landing_url.strip(),
                "target_audience": target_audience.strip(),
                "brand_voice": brand_voice.strip(),
                "days": days,
            }
            st.session_state.briefing = {
                "constraints": constraints.strip(),
                "deadlines": deadlines.strip(),
                "evergreen": evergreen.strip(),
                "evergreen_list": _parse_evergreen_list(evergreen),
                "free_notes": free_notes.strip(),
            }
            if st.session_state.step == "onboarding":
                _set_step("diagnosis")
            st.rerun()

    if st.sidebar.button("🔄 Reset session", use_container_width=True):
        for k in DEFAULT_STATE:
            st.session_state[k] = DEFAULT_STATE[k]
        st.rerun()


# ── Step 0 (welcome) ──────────────────────────────────────────────
def _step_onboarding() -> None:
    st.title("🎯 Funnel Refresher Agent")
    st.markdown(
        "Compila il **form a sinistra** con le credenziali Meta del cliente "
        "e i dettagli della campagna da rinfrescare. Espandi **Briefing avanzato** "
        "se hai vincoli, scadenze o ads evergreen — l'agente le rispetterà a ogni step. "
        "Poi premi **Save & continue**."
    )
    st.info(
        "💡 Le credenziali restano solo nella memoria della sessione (`st.session_state`), "
        "non vengono mai persistite su disco né su DB in questa versione."
    )


# ── Step 1: Diagnosis ─────────────────────────────────────────────
def _step_diagnosis() -> None:
    cfg = st.session_state.config
    st.title("Step 1 · Diagnosi")
    st.caption(
        f"Campagna `{cfg['campaign_id']}` • account `{cfg['meta_account']}` • "
        f"finestra ultimi {cfg['days']} giorni"
    )

    if st.session_state.diagnosis is None:
        if st.button("🔍 Run diagnosis", type="primary"):
            with st.spinner("Pulling Meta ads + insights…"):
                try:
                    meta = MetaClient(cfg["meta_token"], cfg["meta_account"])
                    report = run_diagnosis(
                        meta=meta,
                        campaign_id=cfg["campaign_id"],
                        days=cfg["days"],
                    )
                    st.session_state.diagnosis = report
                    st.rerun()
                except (MetaError, ValueError) as e:
                    st.session_state.error = f"Diagnosis failed: {e}"
                except Exception as e:
                    st.session_state.error = f"Unexpected error: {e}\n\n{traceback.format_exc()}"
        return

    raw_report: DiagnoseReport = st.session_state.diagnosis
    # Build a quick lookup of Meta-reported leads (the "ground truth" before overrides)
    meta_leads_by_referral = {r.referral: r.real_leads for r in raw_report.referrals}

    # Apply any existing overrides to get the corrected report for display + downstream
    report = _corrected_diagnosis() or raw_report

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spesa", f"€{report.total_spend:,.2f}")
    c2.metric("Lead totali", report.total_real_leads)
    c3.metric("CPL medio", f"€{report.avg_real_cpl:,.2f}" if report.avg_real_cpl else "—")
    c4.metric("Da spegnere (suggerito)", len(report.candidate_ads_to_pause))

    st.subheader("📈 Per referral")
    st.caption(
        "💡 La colonna **lead corretto** è editabile: se i lead Meta non corrispondono a "
        "quelli reali (es. li conti su HubSpot/CRM), correggili qui — CPL e raccomandazione "
        '"da spegnere" si ricalcolano automaticamente.'
    )

    edit_data = [
        {
            "referral": r.referral,
            "spend €": round(r.spend, 2),
            "clicks": r.clicks,
            "lead Meta": meta_leads_by_referral.get(r.referral, 0),
            "lead corretto": r.real_leads,
            "CPL €": round(r.real_cpl, 2) if r.real_cpl is not None else None,
        }
        for r in report.referrals
    ]
    edited = st.data_editor(
        edit_data,
        disabled=["referral", "spend €", "clicks", "lead Meta", "CPL €"],
        column_config={
            "lead corretto": st.column_config.NumberColumn(
                "lead corretto",
                min_value=0,
                step=1,
                format="%d",
                help="Edita questo numero se i lead reali sono diversi da quelli del pixel Meta.",
            ),
            "CPL €": st.column_config.NumberColumn(format="%.2f"),
        },
        hide_index=True,
        use_container_width=True,
        key="leads_editor",
    )

    # Sync edits back to lead_overrides state
    new_overrides: dict[str, int] = {}
    for row in edited:
        ref = row["referral"]
        corrected = int(row["lead corretto"]) if row["lead corretto"] is not None else 0
        meta_val = meta_leads_by_referral.get(ref, 0)
        if corrected != meta_val:
            new_overrides[ref] = corrected
    if new_overrides != (st.session_state.lead_overrides or {}):
        st.session_state.lead_overrides = new_overrides
        st.rerun()

    if st.session_state.lead_overrides:
        st.success(
            f"✏️ {len(st.session_state.lead_overrides)} referral con lead corretti manualmente. "
            "CPL, raccomandazioni e step successivi useranno i valori corretti."
        )

    st.subheader("📋 Per ad")
    st.caption("Read-only — i lead riflettono i correzioni della tabella sopra.")
    st.dataframe(
        [
            {
                "name": a.name,
                "status": a.status,
                "referral": a.referral,
                "spend €": round(a.spend, 2),
                "ctr %": round(a.ctr, 2),
                "lead": a.real_leads,
                "CPL €": round(a.real_cpl, 2) if a.real_cpl is not None else None,
                "reco": a.recommendation,
            }
            for a in report.ads
        ],
        use_container_width=True,
    )

    st.divider()
    st.subheader("📝 Hai osservazioni sui numeri?")
    st.caption(
        "Aiuta l'agente a interpretare correttamente la diagnosi. "
        "Es. 'la spesa qui è bassa perché era Pasqua e abbiamo bloccato il budget'."
    )
    st.session_state.observations = st.text_area(
        "Osservazioni (opzionale)",
        value=st.session_state.observations,
        height=80,
        label_visibility="collapsed",
        key="observations_area",
    )

    if st.button("➡️ Propose new angles", type="primary"):
        _set_step("angles")
        st.rerun()


# ── Step 2: Angles ────────────────────────────────────────────────
def _step_angles() -> None:
    st.title("Step 2 · Nuovi angoli")
    cfg = st.session_state.config
    brief = st.session_state.briefing or {}
    report: DiagnoseReport = _corrected_diagnosis()  # uses lead overrides if any

    if st.session_state.angles is None:
        with st.form("angles_form"):
            st.markdown("**Domande prima di proporre gli angoli:**")
            n_angles = st.slider(
                "Quanti angoli vuoi che proponga?",
                min_value=3, max_value=5, value=3,
                help="3 = focalizzato, scelta veloce. 5 = più variazione, decisione più lunga.",
            )
            suggested_angle = st.text_area(
                "Hai già in mente un angolo specifico da esplorare? (opzionale)",
                placeholder="Es. 'un angolo basato sulla storia di un caso studio reale di un nostro studente'",
                height=70,
                help="Se compilato, l'agente lo include negli angoli proposti, raffinato e on-brand.",
            )
            extra_instructions = st.text_area(
                "Altre indicazioni per l'agente? (opzionale)",
                placeholder="Es. 'evita angoli puramente tecnici, parla a chi non sa cos'è un funnel'",
                height=70,
            )
            submitted = st.form_submit_button("✨ Generate angles", type="primary")

        if submitted:
            with st.spinner(f"Claude sta proponendo {n_angles} angoli…"):
                try:
                    angles = propose_angles(
                        api_key=ANTHROPIC_API_KEY,
                        diagnosis=report,
                        target_audience=cfg["target_audience"],
                        brand_voice=cfg["brand_voice"],
                        n_angles=n_angles,
                        constraints=brief.get("constraints", ""),
                        deadlines=brief.get("deadlines", ""),
                        extra_notes=brief.get("free_notes", ""),
                        observations=st.session_state.observations,
                        suggested_angle=suggested_angle,
                        extra_instructions=extra_instructions,
                    )
                    st.session_state.angles = angles
                    st.rerun()
                except Exception as e:
                    st.session_state.error = f"Angle generation failed: {e}"
        return

    angles: list[Angle] = st.session_state.angles
    st.markdown(f"**Scegli un angolo** — l'agente genererà N varianti creative su quello.")
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
    if cols[2].button("➡️ Configure creatives", type="primary"):
        _set_step("creatives")
        st.rerun()


# ── Step 3: Creatives ─────────────────────────────────────────────
def _step_creatives() -> None:
    st.title("Step 3 · Generazione creative")
    cfg = st.session_state.config
    brief = st.session_state.briefing or {}
    angles: list[Angle] = st.session_state.angles
    angle = angles[st.session_state.chosen_angle_idx]

    if st.session_state.creatives is None:
        with st.form("creatives_form"):
            st.markdown(f"**Angolo scelto**: *{angle.title}* — {angle.promise}")
            st.markdown("**Domande prima di generare le creative:**")

            cols = st.columns(2)
            n_variants = cols[0].slider(
                "Quante varianti?",
                min_value=3, max_value=10, value=6,
                help="Più varianti = più scelta in approvazione, ma più costo.",
            )
            quality = cols[1].selectbox(
                "Qualità immagine",
                ["high", "medium", "low"],
                index=0,
                help=(
                    "**high** ≈ €0.25/img (uso per prod) • "
                    "**medium** ≈ €0.07/img • "
                    "**low** ≈ €0.02/img (test rapido)"
                ),
            )
            text_mode = st.radio(
                "Stile immagine",
                ["auto", "headline", "none"],
                index=0,
                horizontal=True,
                help=(
                    "**auto** (consigliato — ad design ricco): composizione pubblicitaria "
                    "completa con headline + sub + 2-4 callout (es. '+€3.500/mese', 'ZERO "
                    "CHIAMATE FREDDE') + CTA bar. Stile split-screen prima/dopo, hero+callouts, "
                    "o big-text dominante. Multi-elemento, color contrast, icone.\n\n"
                    "**headline** (foto + scritta): foto pubblicitaria con UN solo titolo "
                    "in overlay, niente callout o CTA bar. Più sobrio.\n\n"
                    "**none** (solo foto): immagine puramente visiva, nessun testo "
                    "renderizzato. Da usare quando il copy nelle ads Meta basta da solo."
                ),
            )
            image_constraints = st.text_area(
                "Caratteristiche specifiche delle immagini (opzionale)",
                placeholder=(
                    "Es. 'sempre persone reali italiane', 'sfondo ufficio moderno',  "
                    "'no immagini stock', 'no faccia primo piano'"
                ),
                height=70,
                help="Si aggiunge ai prompt di gpt-image-1 per ogni variante.",
            )
            submitted = st.form_submit_button("🎨 Generate creatives", type="primary")

        if submitted:
            with st.spinner(
                f"Generando {n_variants} copy + {n_variants} immagini ({quality})… (1-2 min)"
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
                        creative_constraints=brief.get("constraints", ""),
                        deadlines=brief.get("deadlines", ""),
                        image_constraints=image_constraints,
                        image_text_mode=text_mode,
                    )
                    st.session_state.creatives = creatives
                    st.session_state.approvals = [True] * len(creatives)
                    st.session_state.creative_settings = {
                        "image_quality": quality,
                        "image_text_mode": text_mode,
                        "image_constraints": image_constraints,
                    }
                    st.rerun()
                except Exception as e:
                    st.session_state.error = f"Creative generation failed: {e}"
        return

    creatives: list[Creative] = st.session_state.creatives
    st.markdown(
        f"Generati **{len(creatives)} variants** sull'angolo *{angle.title}*. "
        f"Approva quelle che vuoi lanciare."
    )

    if "approvals" not in st.session_state or len(st.session_state.approvals) != len(creatives):
        st.session_state.approvals = [True] * len(creatives)

    settings = st.session_state.creative_settings or {}
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

            approved = st.checkbox(
                f"✅ Approva variante {i + 1}",
                value=st.session_state.approvals[i],
                key=f"approve_{i}",
            )
            st.session_state.approvals[i] = approved

            if not approved:
                feedback = st.text_area(
                    "💬 Cosa vuoi modificare? (poi clicca Rigenera, oppure scartala)",
                    placeholder=(
                        "Es. 'metti più colore vivo', 'sostituisci la donna con un uomo', "
                        "'elimina la CTA bar in basso', 'rendi la headline più aggressiva', "
                        "'riduci il testo nell'immagine'"
                    ),
                    key=f"feedback_{i}",
                    height=80,
                )
                action_cols = st.columns([1, 1, 3])
                if action_cols[0].button(
                    "🔄 Rigenera con feedback",
                    key=f"regen_{i}",
                    type="primary",
                    disabled=not feedback.strip(),
                ):
                    with st.spinner("Rigenerazione in corso (~30 sec)…"):
                        try:
                            new_creative = regenerate_one_variant(
                                anthropic_api_key=ANTHROPIC_API_KEY,
                                openai_api_key=OPENAI_API_KEY,
                                angle=angle,
                                original=c,
                                feedback=feedback,
                                target_audience=cfg["target_audience"],
                                brand_voice=cfg["brand_voice"],
                                creative_constraints=brief.get("constraints", ""),
                                deadlines=brief.get("deadlines", ""),
                                image_constraints=settings.get("image_constraints", ""),
                                image_text_mode=settings.get("image_text_mode", "auto"),
                                image_quality=settings.get("image_quality", "high"),
                            )
                            st.session_state.creatives[i] = new_creative
                            st.session_state.approvals[i] = True
                            st.session_state[f"feedback_{i}"] = ""
                            st.rerun()
                        except Exception as e:
                            st.error(f"Regen failed: {e}")
                if action_cols[1].button("🗑 Scarta variante", key=f"discard_{i}"):
                    del st.session_state.creatives[i]
                    del st.session_state.approvals[i]
                    st.session_state.pop(f"feedback_{i}", None)
                    st.session_state.pop(f"approve_{i}", None)
                    st.rerun()

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
        f"➡️ Pre-launch ({n_approved})",
        type="primary",
        disabled=n_approved == 0,
    ):
        _set_step("launch")
        st.rerun()


# ── Step 4: Pre-launch questionnaire ──────────────────────────────
def _step_launch() -> None:
    st.title("Step 4 · Pre-launch")
    cfg = st.session_state.config
    brief = st.session_state.briefing or {}
    report: DiagnoseReport = _corrected_diagnosis()  # uses lead overrides if any
    creatives: list[Creative] = st.session_state.creatives
    approvals: list[bool] = st.session_state.approvals
    approved_creatives = [c for c, ok in zip(creatives, approvals) if ok]

    # Pre-compute defaults from briefing
    evergreen_list: list[str] = brief.get("evergreen_list", [])
    active_ads = [a for a in report.ads if a.status == "ACTIVE"]
    pause_default = [
        a.ad_id
        for a in active_ads
        if a.recommendation == "pause"
        and a.referral not in evergreen_list
        and a.name not in evergreen_list
    ]
    untouchable_default = [
        a.ad_id
        for a in active_ads
        if a.referral in evergreen_list or a.name in evergreen_list
    ]

    pause_options = {
        a.ad_id: f"{a.name} ({a.referral}) · spend €{a.spend:.2f} · "
        f"CPL {('€%.2f' % a.real_cpl) if a.real_cpl else '—'}"
        for a in active_ads
    }

    with st.form("launch_form"):
        st.subheader("🛑 Ads da pausare")
        selected_pause = st.multiselect(
            "Seleziona quali pausare (le 'protette' del briefing sono pre-escluse)",
            options=list(pause_options.keys()),
            default=pause_default,
            format_func=lambda ad_id: pause_options[ad_id],
        )

        st.subheader("🛡 Ads protette (non vengono mai pausate)")
        selected_untouchable = st.multiselect(
            "Conferma quali NON pausare in nessun caso",
            options=list(pause_options.keys()),
            default=untouchable_default,
            format_func=lambda ad_id: pause_options[ad_id],
            help=(
                "Pre-popolato dalla lista 'evergreen' dell'onboarding (match per referral o nome). "
                "Aggiungi qui altre ads che vuoi blindare."
            ),
        )

        st.divider()
        st.subheader("📦 Dove creare le nuove ads")
        adset_choice = st.radio(
            "Scegli l'opzione",
            options=[
                "🟢 Stesso adset attivo della campagna · ads ACTIVE subito",
                "🟡 Stesso adset attivo · ads PAUSED (le attivo io da Ads Manager)",
                "🔵 Crea nuovo adset dedicato",
            ],
            index=0,
            help=(
                "**Stesso adset · ACTIVE**: parto subito, nessuna scheduling.\n\n"
                "**Stesso adset · PAUSED**: ads create ma non attive — utile per QA "
                "manuale prima di farle partire.\n\n"
                "**Nuovo adset dedicato**: crea un adset separato (clonando targeting + "
                "promoted_object), serve se vuoi schedulare la partenza in un orario "
                "specifico o tenere queste creative fuori dall'algoritmo dell'adset esistente."
            ),
        )
        create_new_adset = adset_choice.startswith("🔵")
        if adset_choice.startswith("🟢"):
            start_status = "ACTIVE"
        elif adset_choice.startswith("🟡"):
            start_status = "PAUSED"
        else:
            start_status = "ACTIVE"  # in new adset, the schedule controls timing

        new_adset_name = ""
        new_adset_budget = 0.0
        new_adset_start_iso = ""
        new_adset_targeting_note = ""
        if create_new_adset:
            with st.container(border=True):
                st.markdown("**Configurazione nuovo adset**")
                today = datetime.now()
                default_name = f"Refresh {today.strftime('%Y-%m-%d')}"
                new_adset_name = st.text_input(
                    "Nome adset", value=default_name,
                    help="Visibile in Ads Manager. Default include la data di oggi.",
                )
                new_adset_budget = st.number_input(
                    "Budget giornaliero (€)",
                    min_value=5.0,
                    max_value=10000.0,
                    value=30.0,
                    step=5.0,
                    help="Budget dedicato a questo nuovo adset. Se vuoto, copia dall'adset esistente non è supportato in v1 — devi indicarlo.",
                )
                schedule_cols = st.columns(2)
                schedule_date = schedule_cols[0].date_input(
                    "Data partenza",
                    value=today.date(),
                    min_value=today.date(),
                    help="Quando il nuovo adset deve iniziare a spendere.",
                )
                schedule_time = schedule_cols[1].time_input(
                    "Ora partenza (Europe/Rome)",
                    value=(today + timedelta(hours=1)).time().replace(second=0, microsecond=0),
                    help="Es. 06:00 per partire all'alba.",
                )
                # Build ISO 8601 with Italy timezone offset
                naive = datetime.combine(schedule_date, schedule_time)
                # Italy is +0100 in winter / +0200 DST. Use +0200 as default for spring/summer.
                # User will see exact behavior in Ads Manager once created.
                new_adset_start_iso = naive.strftime("%Y-%m-%dT%H:%M:%S+0200")
                st.caption(
                    f"Partenza programmata: `{new_adset_start_iso}` (Europe/Rome). "
                    f"Se sei in fuso orario diverso, controlla l'orario in Ads Manager dopo la creazione."
                )

                st.markdown("**Targeting del nuovo adset**")
                targeting_choice = st.radio(
                    "Vuoi un targeting specifico?",
                    options=[
                        "Copia dall'adset attivo della campagna (consigliato)",
                        "Voglio un targeting diverso — lo configuro io da Ads Manager dopo la creazione",
                    ],
                    index=0,
                )
                if targeting_choice.startswith("Voglio un targeting diverso"):
                    new_adset_targeting_note = st.text_area(
                        "Memo del targeting che vorrai impostare (solo nota interna)",
                        placeholder="Es. 'lookalike 1% Italia su ultimi 30gg fb_pixel_lead'",
                        height=70,
                    )
                    st.warning(
                        "⚠️ Il nuovo adset viene **creato comunque con il targeting clonato**. "
                        "Vai su Meta Ads Manager subito dopo la creazione per modificarlo. "
                        "L'agente non lascia il nuovo adset senza targeting per evitare errori API."
                    )

        st.divider()
        st.subheader("🏷 Tracking referral")
        referral_prefix = st.text_input(
            "Prefisso referral (verrà concatenato con _N)",
            value="refresh",
            help=(
                "Le nuove ads avranno landing URL con `?referral=<prefix>_1`, "
                "`<prefix>_2`, ecc. — coerente con il tuo schema di tracking esistente."
            ),
        )
        n_to_create = len(approved_creatives)
        if n_to_create > 0 and referral_prefix.strip():
            preview = ", ".join(f"{referral_prefix}_{i + 1}" for i in range(min(n_to_create, 4)))
            if n_to_create > 4:
                preview += f", … (+{n_to_create - 4})"
            st.caption(f"Preview: `{preview}`")

        st.subheader("🎯 Call to Action button")
        cta_type = st.selectbox(
            "Bottone delle nuove ads",
            options=[
                "LEARN_MORE", "SIGN_UP", "APPLY_NOW", "DOWNLOAD",
                "GET_OFFER", "SUBSCRIBE", "CONTACT_US", "GET_QUOTE",
            ],
            index=0,
            help=(
                "**LEARN_MORE** → 'Scopri di più' (default lead gen)\n\n"
                "**SIGN_UP** → 'Iscriviti'\n\n"
                "**APPLY_NOW** → 'Candidati ora' (ottimo per recruiting/job ads)\n\n"
                "**DOWNLOAD** → 'Scarica' (lead magnet)\n\n"
                "**GET_OFFER** → 'Richiedi offerta'\n\n"
                "**SUBSCRIBE** → 'Iscriviti' (newsletter / corsi)\n\n"
                "**CONTACT_US** → 'Contattaci'\n\n"
                "**GET_QUOTE** → 'Richiedi preventivo'"
            ),
        )

        submitted = st.form_submit_button("📋 Show launch summary", type="primary", use_container_width=True)

    if submitted:
        st.session_state["_launch_form_data"] = {
            "ads_to_pause": tuple(selected_pause),
            "untouchable_ad_ids": tuple(selected_untouchable),
            "create_new_adset": create_new_adset,
            "new_adset_name": new_adset_name,
            "new_adset_daily_budget_eur": float(new_adset_budget),
            "new_adset_start_time_iso": new_adset_start_iso if create_new_adset else "",
            "new_adset_targeting_note": new_adset_targeting_note,
            "start_status": start_status,
            "referral_prefix": referral_prefix.strip(),
            "cta_type": cta_type,
        }

    form_data = st.session_state.get("_launch_form_data")
    if form_data:
        st.divider()
        st.subheader("📝 Riassunto pre-launch")
        st.markdown(
            f"- **Pausa**: {len(form_data['ads_to_pause'])} ads "
            f"(escluse {len(form_data['untouchable_ad_ids'])} protette)\n"
            f"- **Crea**: {len(approved_creatives)} nuove ads con prefisso "
            f"`{form_data['referral_prefix']}` e CTA `{form_data['cta_type']}`\n"
            f"- **Stato iniziale ads**: `{form_data['start_status']}`\n"
            f"- **Adset**: "
            + (
                f"NUOVO `{form_data['new_adset_name']}` · €{form_data['new_adset_daily_budget_eur']:.0f}/d · "
                f"start `{form_data['new_adset_start_time_iso']}`"
                if form_data["create_new_adset"]
                else "stesso adset attivo della campagna"
            )
            + (
                f"\n- **Memo targeting**: {form_data['new_adset_targeting_note']}"
                if form_data["create_new_adset"] and form_data["new_adset_targeting_note"]
                else ""
            )
        )
        st.warning(
            "⚠️ Una volta cliccato **LAUNCH** le ads vengono create su Meta e i loser pausati. "
            "Operazione non reversibile da questa UI (puoi sempre rimettere ACTIVE da Ads Manager)."
        )

        cols = st.columns([1, 1, 3])
        if cols[0].button("⬅️ Back"):
            _set_step("creatives")
            st.session_state["_launch_form_data"] = None
            st.rerun()
        if cols[1].button("🚀 LAUNCH", type="primary"):
            with st.spinner("Lancio in corso… (uplodo immagini, creo creatives, creo ads)"):
                try:
                    plan = LaunchPlan(
                        ads_to_pause=form_data["ads_to_pause"],
                        untouchable_ad_ids=form_data["untouchable_ad_ids"],
                        create_new_adset=form_data["create_new_adset"],
                        new_adset_name=form_data["new_adset_name"],
                        new_adset_daily_budget_eur=form_data["new_adset_daily_budget_eur"],
                        new_adset_start_time_iso=form_data["new_adset_start_time_iso"],
                        new_adset_copy_targeting=True,
                        new_adset_targeting_note=form_data["new_adset_targeting_note"],
                        start_status=form_data["start_status"],
                        referral_prefix=form_data["referral_prefix"],
                        cta_type=form_data["cta_type"],
                    )
                    meta = MetaClient(cfg["meta_token"], cfg["meta_account"])
                    result = launch_refresh(
                        meta=meta,
                        campaign_id=cfg["campaign_id"],
                        plan=plan,
                        creatives_to_launch=approved_creatives,
                        landing_url=cfg["landing_url"],
                        page_id=cfg["page_id"],
                        instagram_user_id=cfg["ig_user_id"],
                    )
                    st.session_state.launch_result = result
                    st.session_state["_launch_form_data"] = None
                    _set_step("done")
                    st.rerun()
                except Exception as e:
                    st.session_state.error = f"Launch failed: {e}\n\n{traceback.format_exc()}"


# ── Step 5: Done ──────────────────────────────────────────────────
def _step_done() -> None:
    st.title("✅ Refresh completato")
    result = st.session_state.launch_result
    st.success(
        f"Pausate: {len(result.paused)} · Create: {len(result.created)}"
        + (f" · Nuovo adset: {result.new_adset_id}" if result.new_adset_id else "")
    )

    if result.new_adset_id:
        st.info(
            f"📦 Nuovo adset creato: `{result.new_adset_id}`. "
            "Vai su Meta Ads Manager se hai indicato di voler personalizzare il targeting."
        )

    st.subheader("Pausate")
    st.code("\n".join(result.paused) or "(nessuna)")

    st.subheader("Nuove ads create")
    st.dataframe(result.created, use_container_width=True)

    st.info(
        "Le ads sono state create. Meta tipicamente impiega 5–30 minuti per l'approvazione. "
        "Verifica lo status su Ads Manager o rilancia la diagnosi tra qualche ora."
    )
    if st.button("🔄 New refresh (same client)"):
        for k in (
            "diagnosis", "lead_overrides", "observations", "angles", "chosen_angle_idx",
            "creatives", "approvals", "launch_result",
        ):
            st.session_state[k] = DEFAULT_STATE[k]
        st.session_state["_launch_form_data"] = None
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
