"""Creative generation — Claude writes copy, gpt-image-1 generates the visual.

For one chosen angle we generate `n_variants` creative variants. Each variant has:
  - headline (the "name" field on Meta's link_data)
  - body (the "message" field, multi-line, with line breaks like the existing scripts)
  - image_prompt (Italian, fed to gpt-image-1)
  - image_bytes (PNG bytes returned by gpt-image-1)

`image_bytes` is what we will upload to Meta's /adimages endpoint at launch.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

from anthropic import Anthropic
from openai import OpenAI

from agent.angles import Angle

CLAUDE_MODEL = "claude-sonnet-4-6"
IMAGE_MODEL = "gpt-image-1"


@dataclass
class Creative:
    slug: str
    headline: str
    body: str
    image_prompt: str
    image_bytes: bytes = field(repr=False)
    image_mime: str = "image/png"


COPY_SYSTEM_PROMPT = (
    "You are a senior direct-response copywriter for Italian Meta Ads funnels. "
    "Your job: take ONE creative angle and produce N distinct ad variants in Italian "
    "that *attack the same angle from different concrete entry points* (e.g., different "
    "characters, different micro-pains, different proofs).\n\n"
    "Each variant must have:\n"
    '  - "slug": short snake_case identifier (max 30 chars, ASCII only, no spaces)\n'
    '  - "headline": ad title shown above the image (max 40 chars, Italian, punchy)\n'
    '  - "body": the multi-line ad message (Italian, 4–8 short lines separated by \\n, '
    "      may include emoji at line starts like the existing winning ads, "
    "      and ends with a one-line CTA)\n"
    '  - "image_prompt": a vivid English prompt for an AI image generator that produces '
    "      a photographic, advertising-ready square image embodying the variant. "
    "      Describe subject, setting, mood, lighting. Avoid generic stock-photo language. "
    "      If the variant should include short Italian text on the image, specify the exact "
    "      text in quotes inside the prompt (gpt-image-1 renders text reliably).\n\n"
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
    n_variants: int = 6,
) -> list[dict]:
    """Ask Claude for N copy variants for one angle. Returns raw dicts (no image yet)."""
    client = Anthropic(api_key=api_key)
    user_prompt = (
        f"Target audience: {target_audience}\n"
        f"Brand voice: {brand_voice}\n\n"
        f"Chosen angle:\n"
        f"  Title: {angle.title}\n"
        f"  Rationale: {angle.rationale}\n"
        f"  Target pain: {angle.target_pain}\n"
        f"  Promise: {angle.promise}\n\n"
        f"Produce {n_variants} variants now."
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=COPY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _extract_json_array(text)


def generate_image(
    *,
    api_key: str,
    prompt: str,
    quality: str = "high",
    size: str = "1024x1024",
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
) -> list[Creative]:
    """End-to-end: copies via Claude, then one image per copy via gpt-image-1."""
    raw_copies = generate_copies(
        api_key=anthropic_api_key,
        angle=angle,
        target_audience=target_audience,
        brand_voice=brand_voice,
        n_variants=n_variants,
    )
    creatives: list[Creative] = []
    for c in raw_copies:
        slug = re.sub(r"[^a-z0-9_]+", "_", str(c["slug"]).lower()).strip("_")[:30] or "variant"
        image_bytes = generate_image(
            api_key=openai_api_key,
            prompt=str(c["image_prompt"]),
            quality=image_quality,
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
