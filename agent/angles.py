"""Angle generation — Claude proposes new creative angles based on diagnosis + briefing.

The prompt receives the diagnosis snapshot (winners + losers + spent), the brand
voice + target audience, plus any briefing context (creative constraints, upcoming
deadlines, post-diagnosis observations, an optional operator-suggested angle).
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
    lines: list[str] = [
        f"Window: {report.since} → {report.until} ({report.days} days)",
        f"Total spend: €{report.total_spend:.2f}",
        f"Total leads (Meta pixel): {report.total_real_leads}",
    ]
    if report.avg_real_cpl is not None:
        lines.append(f"Average CPL: €{report.avg_real_cpl:.2f}")
    lines.append("")
    lines.append("Per-referral breakdown (sorted by spend):")
    for r in report.referrals[:15]:
        cpl = f"€{r.real_cpl:.2f}" if r.real_cpl is not None else "no leads"
        lines.append(
            f"  - {r.referral}: spend €{r.spend:.2f} | clicks {r.clicks} "
            f"| leads {r.real_leads} | CPL {cpl}"
        )
    lines.append("")
    lines.append("Pause-candidate ads (CPL > 1.5x median, or no leads on €30+ spend):")
    pause_rows = [a for a in report.ads if a.recommendation == "pause" and a.status == "ACTIVE"]
    if not pause_rows:
        lines.append("  (none flagged)")
    for a in pause_rows[:10]:
        cpl = f"€{a.real_cpl:.2f}" if a.real_cpl is not None else "no leads"
        lines.append(
            f"  - {a.name} (referral={a.referral}): spend €{a.spend:.2f} "
            f"| CTR {a.ctr:.2f}% | CPL {cpl}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are a senior performance marketer specialized in creative refresh for Meta Ads "
    "funnels. You analyze fatigue signals (CPL drift, CTR collapse, no-conversion ads) "
    "and propose new creative angles that explore *different psychological levers* than "
    "the ones currently fatigued.\n\n"
    "Always reply with a strict JSON array of angle objects, no prose, no markdown "
    "code fences. Each angle object has these fields:\n"
    '  - "title": short label (max 40 chars), in Italian\n'
    '  - "rationale": why this angle should work given the diagnosis (max 200 chars), in Italian\n'
    '  - "target_pain": the specific pain point the angle attacks (max 120 chars), in Italian\n'
    '  - "promise": the concrete promise the ad makes to the reader (max 120 chars), in Italian\n'
)


def _extract_json_array(raw: str) -> list[dict]:
    raw = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    return json.loads(raw)


def _section(label: str, body: str) -> str:
    """Render an optional briefing section, or empty string if body is blank."""
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n{label}:\n{body}\n"


def propose_angles(
    *,
    api_key: str,
    diagnosis: DiagnoseReport,
    target_audience: str,
    brand_voice: str,
    n_angles: int = 3,
    constraints: str = "",
    deadlines: str = "",
    extra_notes: str = "",
    observations: str = "",
    suggested_angle: str = "",
    extra_instructions: str = "",
) -> list[Angle]:
    """Ask Claude for `n_angles` new angles given diagnosis + briefing context."""
    if n_angles < 1 or n_angles > 10:
        raise ValueError(f"n_angles must be in [1, 10], got {n_angles}")

    client = Anthropic(api_key=api_key)
    user_prompt_parts = [
        f"Target audience: {target_audience}",
        f"Brand voice: {brand_voice}",
        f"Number of angles to propose: {n_angles}",
    ]
    user_prompt_parts.append(_section("Creative constraints (things to AVOID)", constraints))
    user_prompt_parts.append(_section("Upcoming deadlines / events", deadlines))
    user_prompt_parts.append(_section("Operator's free notes", extra_notes))
    user_prompt_parts.append(_section("Operator's observations on the diagnosis numbers", observations))
    user_prompt_parts.append(
        _section(
            "Operator-suggested angle to ALSO include",
            f"{suggested_angle}\n(Include this as one of the angles, refined and on-brand.)"
            if suggested_angle.strip()
            else "",
        )
    )
    user_prompt_parts.append(_section("Extra instructions", extra_instructions))
    user_prompt_parts.append("\nDiagnosis snapshot:\n" + _build_diagnosis_summary(diagnosis))
    user_prompt_parts.append(f"\n\nNow return the JSON array of exactly {n_angles} new creative angles.")
    user_prompt = "".join(user_prompt_parts)

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2500,
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
