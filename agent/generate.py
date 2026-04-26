"""Creative generation — Claude writes copy, gpt-image-1 generates the visual.

For one chosen angle we generate `n_variants` creative variants. Each variant has:
  - headline (the "name" field on Meta's link_data)
  - body (the "message" field, multi-line, with line breaks like the existing scripts)
  - image_prompt (Italian-grounded but English-described, fed to gpt-image-1)
  - image_bytes (PNG bytes returned by gpt-image-1)

`image_bytes` is what we will upload to Meta's /adimages endpoint at launch.

Briefing context (creative_constraints, deadlines, image_constraints, image_text_mode)
is woven into both the copy prompt and the image prompts.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from anthropic import Anthropic
from openai import OpenAI

from agent.angles import Angle

CLAUDE_MODEL = "claude-sonnet-4-6"
IMAGE_MODEL = "gpt-image-1"
IMAGE_SIZE_SQUARE = "1024x1024"

ImageTextMode = Literal["none", "headline", "auto"]


@dataclass
class Creative:
    slug: str
    headline: str
    body: str
    image_prompt: str
    image_bytes: bytes = field(repr=False)
    image_mime: str = "image/png"


def _section(label: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n{label}:\n{body}\n"


_RICH_AD_GUIDANCE = (
    "Each `image_prompt` must instruct gpt-image-1 to produce a SQUARE 1024x1024 "
    "ADVERTISING-DESIGN composition — NOT a generic stock photo with a headline "
    "overlay. Think Meta/Instagram ad, billboard, or magazine spread quality.\n\n"
    "MANDATORY composition: pick ONE of these structures for each variant and "
    "describe it in detail:\n"
    "  A. Split-screen PRIMA / DOPO: vertical divider down the middle, dark+stressed "
    "     scene LEFT (e.g. before automation), bright+relaxed scene RIGHT (after). "
    "     Centered glowing icon connects the two halves.\n"
    "  B. Hero subject + icon-callouts: a real-looking Italian person centered, with "
    "     3-4 floating callout boxes around them (each with a small icon + 1-3 word "
    "     Italian label like '+€3.500/MESE' or 'ZERO CHIAMATE FREDDE').\n"
    "  C. Big-text-dominant: 60% of the frame is bold Italian text (headline + sub), "
    "     40% is a contextual photo or graphic. Newspaper/billboard style.\n"
    "  D. Numbers/proof spotlight: a huge number or stat (e.g. '+247 LEAD/MESE') as "
    "     the visual hero, with supporting photo or icons.\n\n"
    "MANDATORY visual language for every prompt:\n"
    "  - HIGH-CONTRAST color palette (e.g. black + electric yellow, navy + orange, "
    "    deep red + ivory). NO pastels, NO washed-out muted tones.\n"
    "  - Bold sans-serif uppercase for main headlines (advertising font feel — Inter "
    "    Black, Anton, Bebas Neue, Helvetica Black).\n"
    "  - When characters appear: realistic Italian/European 30-50 year olds, NOT "
    "    plastic stock-photo smile. Authentic micro-expressions.\n"
    "  - Color-graded: dark/cool for 'before' scenes, warm/golden for 'after'.\n"
    "  - Iconography: small modern flat-style icons (lightning, checkmark, clock, "
    "    coin, target) on callouts when relevant.\n"
    "  - One bold CTA bar at the bottom edge in a contrasting color (e.g. yellow "
    "    bar with black text, OR red bar with white text) with 2-4 word action "
    "    verb in Italian.\n\n"
    "TEXT RENDERING — gpt-image-1 reads quoted Italian text reliably. ALWAYS quote "
    "exact text inline:\n"
    '  - Top-bar headline: 3-5 words uppercase bold (e.g. "MENO CAOS. PIÙ TEMPO.")\n'
    '  - Optional subhead: one short line below (e.g. "AUTOMATIZZA CON L\'AI.")\n'
    '  - Callouts: 1-3 words each, uppercase\n'
    '  - Bottom CTA bar: action phrase (e.g. "AUTOMATIZZA. DELEGA. LIBERATI.")\n\n'
    "Aim for EVERY image_prompt to be 200-400 words long with concrete spatial, "
    "color, typography, and text-element detail. Vague prompts = bland output."
)


def _copy_system_prompt(image_text_mode: ImageTextMode) -> str:
    if image_text_mode == "none":
        text_policy = (
            "TEXT POLICY: NO rendered text on the image. Image must be purely visual. "
            "Skip headlines, callouts, CTA bars. Use composition + color + iconography "
            "only. The Meta ad headline + body will live OUTSIDE the image."
        )
    elif image_text_mode == "headline":
        text_policy = (
            "TEXT POLICY: render ONLY one dominant Italian headline in the image "
            "(3-5 words, uppercase bold, top of frame). No subhead, no callouts, "
            "no CTA bar. Photo-driven with one strong text element overlay."
        )
    else:  # "auto" — the default, full ad-design
        text_policy = (
            "TEXT POLICY: full advertising design with multiple Italian text elements "
            "as described in the rich-ad guidance below. Headline + (optional) subhead "
            "+ 2-4 callout labels + CTA bar. Each text element quoted explicitly."
        )

    return (
        "You are a senior art director and direct-response copywriter for Italian "
        "Meta Ads funnels. Take ONE creative angle and produce N distinct variants "
        "that *attack the angle from different concrete entry points* (different "
        "characters, micro-pains, proofs, formats).\n\n"
        f"{text_policy}\n\n"
        f"{_RICH_AD_GUIDANCE}\n\n"
        "Each variant object MUST have these fields exactly:\n"
        '  - "slug": short snake_case ASCII identifier (max 30 chars, no spaces)\n'
        '  - "headline": Meta ad TITLE field (Italian, max 40 chars, punchy). This '
        "      is the text that appears in Meta\'s ad metadata, NOT necessarily the "
        "      same as the headline rendered on the image.\n"
        '  - "body": Meta ad PRIMARY TEXT field (Italian, 4–8 short lines separated '
        "      by \\n, may use emoji at line starts, ends with a one-line CTA).\n"
        '  - "image_prompt": detailed advertising-design prompt (200-400 words, '
        "      English, follows ALL the rich-ad guidance above).\n\n"
        "Reply ONLY with a strict JSON array of N variant objects. No prose, no "
        "markdown code fences."
    )


def _extract_json_array(raw: str) -> list[dict]:
    raw = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    return json.loads(raw)


def generate_copies(
    *,
    api_key: str,
    angle: Angle,
    target_audience: str,
    brand_voice: str,
    n_variants: int,
    creative_constraints: str = "",
    deadlines: str = "",
    image_constraints: str = "",
    image_text_mode: ImageTextMode = "auto",
) -> list[dict]:
    """Ask Claude for N copy variants for one angle. Returns raw dicts (no image yet)."""
    client = Anthropic(api_key=api_key)
    user_prompt_parts = [
        f"Target audience: {target_audience}",
        f"Brand voice: {brand_voice}",
        "",
        "Chosen angle:",
        f"  Title: {angle.title}",
        f"  Rationale: {angle.rationale}",
        f"  Target pain: {angle.target_pain}",
        f"  Promise: {angle.promise}",
    ]
    user_prompt_parts.append(_section("Copy constraints (things to AVOID in the body/headline)", creative_constraints))
    user_prompt_parts.append(_section("Deadlines / urgency to convey", deadlines))
    user_prompt_parts.append(_section("Image constraints (things to AVOID or ALWAYS include in image_prompt)", image_constraints))
    user_prompt_parts.append(f"\nProduce {n_variants} variants now.")
    user_prompt = "\n".join(user_prompt_parts)

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,  # rich image_prompts run 200-400 words × N variants
        system=_copy_system_prompt(image_text_mode),
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _extract_json_array(text)


REGEN_SYSTEM_PROMPT = (
    "You are revising ONE existing Italian Meta ad creative variant based on operator "
    "feedback. Read the original variant + the feedback, then produce a SINGLE revised "
    "variant that addresses the feedback while staying on-brand, on-angle, and on-target. "
    "If the feedback is purely about the image (e.g. 'più colore', 'metti un uomo'), keep "
    "headline+body roughly the same and only revise image_prompt. If feedback is about "
    "the copy (e.g. 'headline più aggressiva'), revise headline/body and keep image_prompt "
    "close to original. If both, revise both. Always keep the same slug to make tracking easy.\n\n"
    "Reply with ONE JSON object (NOT an array), no prose, no markdown fences. Fields: "
    '"slug", "headline", "body", "image_prompt". Same content rules as a fresh generation '
    "(rich ad-design composition for image_prompt, etc.)."
)


def regenerate_one_variant(
    *,
    anthropic_api_key: str,
    openai_api_key: str,
    angle: Angle,
    original: Creative,
    feedback: str,
    target_audience: str,
    brand_voice: str,
    creative_constraints: str = "",
    deadlines: str = "",
    image_constraints: str = "",
    image_text_mode: ImageTextMode = "auto",
    image_quality: str = "high",
) -> Creative:
    """Regenerate a SINGLE variant given operator feedback. Returns the revised Creative."""
    if not feedback.strip():
        raise ValueError("Feedback cannot be empty for regeneration")

    client = Anthropic(api_key=anthropic_api_key)
    user_prompt_parts = [
        f"Target audience: {target_audience}",
        f"Brand voice: {brand_voice}",
        "",
        "Chosen angle:",
        f"  Title: {angle.title}",
        f"  Rationale: {angle.rationale}",
        f"  Target pain: {angle.target_pain}",
        f"  Promise: {angle.promise}",
        "",
        "ORIGINAL variant to revise:",
        f"  slug: {original.slug}",
        f"  headline: {original.headline}",
        f"  body: |",
    ]
    for line in original.body.splitlines():
        user_prompt_parts.append(f"    {line}")
    user_prompt_parts.append(f"  image_prompt: {original.image_prompt}")
    user_prompt_parts.append("")
    user_prompt_parts.append("OPERATOR FEEDBACK (the modification to apply):")
    user_prompt_parts.append(f"  {feedback}")
    user_prompt_parts.append(_section("Copy constraints (still apply)", creative_constraints))
    user_prompt_parts.append(_section("Deadlines / urgency (still apply)", deadlines))
    user_prompt_parts.append(_section("Image constraints (still apply)", image_constraints))
    user_prompt_parts.append("\nReturn the revised variant as ONE JSON object now.")
    user_prompt = "\n".join(user_prompt_parts)

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2500,
        system=REGEN_SYSTEM_PROMPT + "\n\n" + _RICH_AD_GUIDANCE,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    raw = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    revised = json.loads(raw)
    if isinstance(revised, list):
        # Be lenient if Claude returned an array of one
        revised = revised[0]

    slug = re.sub(r"[^a-z0-9_]+", "_", str(revised["slug"]).lower()).strip("_")[:30] or original.slug
    image_bytes = generate_image(
        api_key=openai_api_key,
        prompt=str(revised["image_prompt"]),
        quality=image_quality,
        size=IMAGE_SIZE_SQUARE,
    )
    return Creative(
        slug=slug,
        headline=str(revised["headline"]).strip(),
        body=str(revised["body"]).strip(),
        image_prompt=str(revised["image_prompt"]).strip(),
        image_bytes=image_bytes,
    )


def generate_image(
    *,
    api_key: str,
    prompt: str,
    quality: str = "high",
    size: str = IMAGE_SIZE_SQUARE,
) -> bytes:
    """Call gpt-image-1 and return raw PNG bytes."""
    client = OpenAI(api_key=api_key)
    result = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size=size,
        quality=quality,
        n=1,
    )
    b64 = result.data[0].b64_json
    if not b64:
        raise RuntimeError("gpt-image-1 returned empty b64_json")
    return base64.b64decode(b64)


def generate_creatives(
    *,
    anthropic_api_key: str,
    openai_api_key: str,
    angle: Angle,
    target_audience: str,
    brand_voice: str,
    n_variants: int = 6,
    image_quality: str = "high",
    creative_constraints: str = "",
    deadlines: str = "",
    image_constraints: str = "",
    image_text_mode: ImageTextMode = "auto",
) -> list[Creative]:
    """End-to-end: copies via Claude, then one square image per copy via gpt-image-1."""
    raw_copies = generate_copies(
        api_key=anthropic_api_key,
        angle=angle,
        target_audience=target_audience,
        brand_voice=brand_voice,
        n_variants=n_variants,
        creative_constraints=creative_constraints,
        deadlines=deadlines,
        image_constraints=image_constraints,
        image_text_mode=image_text_mode,
    )
    creatives: list[Creative] = []
    for c in raw_copies:
        slug = re.sub(r"[^a-z0-9_]+", "_", str(c["slug"]).lower()).strip("_")[:30] or "variant"
        image_bytes = generate_image(
            api_key=openai_api_key,
            prompt=str(c["image_prompt"]),
            quality=image_quality,
            size=IMAGE_SIZE_SQUARE,
        )
        creatives.append(
            Creative(
                slug=slug,
                headline=str(c["headline"]).strip(),
                body=str(c["body"]).strip(),
                image_prompt=str(c["image_prompt"]).strip(),
                image_bytes=image_bytes,
            )
        )
    return creatives
