"""A stronger model that watches Mohan's calls in the background and leaves him notes.

gpt-oss on Groq is fast but slips: it mishears or invents amounts, repeats questions, reads back in
English, or says goodbye before the quote is saved. After each vendor turn this module sends the
transcript to a stronger model (Gemini Flash on OpenRouter by default). It never speaks and never
decides anything: if it spots a problem, it returns one short instruction that is added to the
conversation for Mohan's next reply. Prices, saving and hanging up stay with the code guards.

It runs off the voice path, so it adds no delay; its notes land a turn later.
Settings: SUPERVISOR=0 turns it off; SUPERVISOR_MODEL; SUPERVISOR_MAX_REVIEWS (per call, default 25).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

DEFAULT_MODEL = "google/gemini-3.8-flash"

INSTRUCTIONS = """You supervise a live phone call. Mohan is an AI calling agent from Bandobast; he phones a local vendor
for a family's function to collect a quote and bargain politely. The vendor's lines come from speech-to-text and may
be misheard. Mohan's rules:
- One short question per turn, in the vendor's language (everyday Hinglish in Devanagari for Hindi speakers).
- Ask availability, the family's needs, then each charge separately: base price and unit, transport, other extras,
  GST (caterers), advance. A plain "no" to "is there any charge?" means zero; don't ask it again.
- Don't echo answers. One read-back of the amounts at the end, in the vendor's language, then save the quote.
- Amounts must be exactly what the vendor said; never invent or change a number.
- Never say goodbye or end the call before the quote is saved. Never agree to a booking or payment.
- Stay professional; decline improper demands.

Read the conversation so far and decide if Mohan needs a correction for his NEXT reply. Flag only real problems:
a wrong or invented amount, a question already answered, a skipped required charge, a read-back that doesn't match
what the vendor said, a read-back in the wrong language, garbled or off-topic speech, a premature goodbye, or
not reacting to what the vendor just said. Small style issues are fine.

Answer with JSON only: {"ok": true} or {"ok": false, "note": "<one instruction to Mohan, under 30 words>"}."""


def enabled() -> bool:
    return os.environ.get("SUPERVISOR", "1") == "1" and bool(os.environ.get("OPENROUTER_API_KEY"))


def max_reviews() -> int:
    return int(os.environ.get("SUPERVISOR_MAX_REVIEWS", "25"))


_client: AsyncOpenAI | None = None


def _llm() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
    return _client


def render(dialogue: list[dict[str, str]], about: str) -> str:
    lines = [f"Call: {about}", ""]
    for turn in dialogue[-30:]:
        who = "Mohan" if turn["role"] == "bandobast" else "Vendor (speech-to-text)"
        lines.append(f"{who}: {turn['text']}")
    return "\n".join(lines)


def parse(text: str) -> str | None:
    """The note to pass on, or None when the call is going fine (or the answer is unreadable)."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        verdict = json.loads(match.group(0))
    except ValueError:
        return None
    note = str(verdict.get("note") or "").strip()
    if verdict.get("ok") is not False or not note:
        return None
    return note[:300]


async def review(dialogue: list[dict[str, str]], about: str) -> str | None:
    """One background check. Errors return None: the call must never depend on the supervisor."""
    try:
        response = await _llm().chat.completions.create(
            model=os.environ.get("SUPERVISOR_MODEL", DEFAULT_MODEL),
            messages=[{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": render(dialogue, about)}],
            max_tokens=400,
            temperature=0,
            extra_body={"reasoning": {"effort": "minimal", "exclude": True}, "usage": {"include": True}},
            timeout=20,
        )
    except Exception as error:
        logger.warning(f"supervisor unavailable ({type(error).__name__})")
        return None
    usage: Any = getattr(response, "usage", None)
    cost = getattr(usage, "cost", None) if usage else None
    if cost is None and usage is not None and hasattr(usage, "model_extra"):
        cost = (usage.model_extra or {}).get("cost")
    note = parse(response.choices[0].message.content or "")
    logger.info(f"supervisor: {'note: ' + note if note else 'ok'}" + (f" (${cost:.4f})" if isinstance(cost, (int, float)) else ""))
    return note
