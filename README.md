# Funnel Refresher Agent

Streamlit web app that runs a creative refresh on a Meta Ads funnel.

Workflow (one campaign at a time, on-demand):

1. **Onboarding** — client fills form with Meta + HubSpot tokens and campaign details.
2. **Diagnosis** — pulls last 14 days of ad-level performance from Meta and cross-references
   with HubSpot Form Submissions to compute *real* CPL per `?referral=` parameter.
3. **Angle proposal** — Claude proposes 3–5 new creative angles based on what is fatigued
   and the brand voice / target audience.
4. **Creative generation** — Claude writes copy/headlines, `gpt-image-1` produces the visuals.
5. **Approval** — operator approves each creative individually (or "approve all").
6. **Launch** — pauses the fatigued ads and creates the new ones in the same active adset
   with referral tracking (`?referral=newN`).

## Setup (local)

```bash
cd /Users/salvotrifiro/funnel-refresher-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then edit .env with your real keys
streamlit run app.py
```

App opens at `http://localhost:8501`.

## Deploy on Streamlit Cloud

1. Push repo to GitHub (private is fine).
2. Sign in at [streamlit.io/cloud](https://streamlit.io/cloud) with the same GitHub account.
3. New app → pick this repo → main branch → entry point `app.py`.
4. Settings → Secrets → paste the content of `.streamlit/secrets.toml.example`
   replacing the placeholders with your real keys.

## What the client provides

The client (the funnel owner) provides their own credentials in the onboarding form:

- **Meta Ad Account ID** — `act_XXXXXXXXX`
- **Meta System User Access Token** — generated from Business Settings → System Users
- **HubSpot Private App Token**
- **Meta Campaign ID** — the campaign to refresh
- **Landing URL** — destination for new ads (referral param appended automatically)
- **HubSpot Form ID** — the lead-capture form on that landing
- **Page ID + Instagram User ID** — required to create ads on the right Page/IG account
- **Target audience** (1 sentence) — drives angle generation
- **Brand voice** (1 sentence) — drives copy generation

These never leave the user's session — they are kept only in `st.session_state` (memory),
not persisted to disk in v1.

## Cost per refresh (rough)

| Item | Cost |
|------|------|
| Claude Sonnet 4.6 (diagnosis + 3 angles + 6–8 copy variants) | ~€0.30 |
| `gpt-image-1` high quality, 8 images | ~€2.00 |
| Meta + HubSpot API | free |
| Hosting (Streamlit Cloud free tier) | free |
| **Total** | **~€2.30** |
