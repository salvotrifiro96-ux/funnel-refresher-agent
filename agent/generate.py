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


def _copy_system_prompt(image_text_mode: ImageTextMode) -> str:
    text_clause = {
        "none": (
            "The image must be visual-only — DO NOT include any rendered text on the image."
        ),
        "headline": (
            "Render the Italian headline (or a 3-5 word punchy version of it) as text "
            "on the image. Use a clean, advertising-style font. gpt-image-1 renders text "
            "reliably — instruct it explicitly with the exact text in quotes."
        ),
        "auto": (
            "You decide whether to include text on the image. If a short Italian phrase "
            "(<6 words) reinforces the message, include it in the image prompt with exact "
            "quoted text. Otherwise keep the image visual-only."
        ),
    }[image_text_mode]
    return (
        "You are a senior direct-response copywriter for Italian Meta Ads funnels. "
        "Your job: take ONE creative angle and produce N distinct ad variants in Italian "
        "that *attack the same angle from different concrete entry points* (e.g., different "
        "characters, different micro-pains, different proofs).\n\n"
        f"Image text policy: {text_clause}\n\n"
        "Each variant must have:\n"
        '  - "slug": short snake_case identifier (max 30 chars, ASCII only, no spaces)\n'
        '  - "headline": ad title shown above the image (max 40 chars, Italian, punchy)\n'
        '  - "body": the multi-line ad message (Italian, 4–8 short lines separated by \\n, '
        "      may include emoji at line starts, ends with a one-line CTA)\n"
        '  - "image_prompt": vivid English prompt for an AI image generator that produces '
        "      a PHOTOGRAPHIC, advertising-ready square 1024x1024 image embodying the variant. "
        "      Describe subject, setting, mood, lighting concretely. Avoid generic stock-photo "
        "      language. Square aspect ratio is mandatory — the prompt must be composable "
        "      as a square frame.\n\n"
        "Reply ONLY with a strict JSON array of N variant objects. No prose, no markdown fences."
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
        max_tokens=4500,
        system=_copy_system_prompt(image_text_mode),
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _extract_json_array(text)


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
