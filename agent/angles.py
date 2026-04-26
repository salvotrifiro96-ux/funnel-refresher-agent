"""Angle generation — Claude proposes 3–5 new creative angles based on diagnosis.

The prompt is structured: it gets the diagnosis snapshot (winners + losers + spent),
the brand voice and target audience, and is asked to return a strict JSON list of
angle objects.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from anthropic import Anthropic

from agent.diagnose import DiagnoseReport

CLAUDE_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class Angle:
    title: str
    rationale: str
    target_pain: str
    promise: str


def _build_diagnosis_summary(report: DiagnoseReport) -> str:
    """Compact, human-readable snapshot the model will reason over."""
    lines: list[str] = [
        f"Window: {report.since} → {report.until} ({report.days} days)",
        f"Total spend: €{report.total_spend:.2f}",
        f"Total real leads (HubSpot): {report.total_real_leads}",
    ]
    if report.avg_real_cpl is not None:
        lines.append(f"Average real CPL: €{report.avg_real_cpl:.2f}")
    lines.append("")
    lines.append("Per-referral breakdown (sorted by spend):")
    for r in report.referrals[:15]:
        cpl = f"€{r.real_cpl:.2f}" if r.real_cpl is not None else "no leads"
        lines.append(
            f"  - {r.referral}: spend €{r.spend:.2f} | clicks {r.clicks} "
            f"| HS leads {r.real_leads} | real CPL {cpl}"
        )
    lines.append("")
    lines.append("Pause-candidate ads (real CPL > 1.5x median, or no leads on €30+ spend):")
    pause_rows = [a for a in report.ads if a.recommendation == "pause" and a.status == "ACTIVE"]
    if not pause_rows:
        lines.append("  (none flagged)")
    for a in pause_rows[:10]:
        cpl = f"€{a.real_cpl:.2f}" if a.real_cpl is not None else "no leads"
        lines.append(
            f"  - {a.name} (referral={a.referral}): spend €{a.spend:.2f} "
            f"| CTR {a.ctr:.2f}% | real CPL {cpl}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are a senior performance marketer specialized in creative refresh for Meta Ads "
    "funnels. You analyze fatigue signals (CPL drift, CTR collapse, no-conversion ads) "
    "and propose new creative angles that explore *different psychological levers* than "
    "the ones currently fatigued.\n\n"
    "Always reply with a strict JSON array of 3 to 5 angle objects, no prose, no markdown "
    "code fences. Each angle object has these fields:\n"
    '  - "title": short label (max 40 chars), in Italian\n'
    '  - "rationale": why this angle should work given the diagnosis (max 200 chars), in Italian\n'
    '  - "target_pain": the specific pain point the angle attacks (max 120 chars), in Italian\n'
    '  - "promise": the concrete promise the ad makes to the reader (max 120 chars), in Italian\n'
)


def _extract_json_array(raw: str) -> list[dict]:
    """Be lenient: strip ```json fences if the model added them anyway."""
    raw = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    return json.loads(raw)


def propose_angles(
    *,
    api_key: str,
    diagnosis: DiagnoseReport,
    target_audience: str,
    brand_voice: str,
    extra_instructions: str = "",
) -> list[Angle]:
    client = Anthropic(api_key=api_key)
    user_prompt = (
        f"Target audience: {target_audience}\n"
        f"Brand voice: {brand_voice}\n"
    )
    if extra_instructions.strip():
        user_prompt += f"Extra notes from operator: {extra_instructions}\n"
    user_prompt += "\nDiagnosis snapshot:\n" + _build_diagnosis_summary(diagnosis)
    user_prompt += "\n\nNow return the JSON array of 3 to 5 new creative angles."

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    raw_angles = _extract_json_array(text)
    return [
        Angle(
            title=str(a["title"]).strip(),
            rationale=str(a["rationale"]).strip(),
            target_pain=str(a["target_pain"]).strip(),
            promise=str(a["promise"]).strip(),
        )
        for a in raw_angles
    ]
