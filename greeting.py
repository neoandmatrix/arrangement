"""Mohan's first line on every outbound call, pre-generated so it plays the moment the vendor picks up.

Waiting for the LLM and then the TTS made the vendor say "hello?" into silence for a few seconds.
The line is fixed per vendor ("hello, this is Mohan, am I speaking with ...?"), so its audio is
synthesised once and kept in voice_cache/. Calls play the saved audio; if it is missing (a new
vendor, another sample rate) the call speaks the same text through live TTS and saves it for next time.

    uv run python greeting.py   # pre-generate every vendor's greeting
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import wave
from pathlib import Path
from typing import Any

from loguru import logger

ROOT = Path(__file__).parent
CACHE = Path(os.environ.get("VOICE_CACHE", ROOT / "voice_cache"))
MODEL = "timbre-v3.0"
APP_RATE = 24000  # the app and the web demo play Bandobast at 24 kHz

# Everyday phrasing in each vendor language. The name stays as written: the voice reads it fine.
_LINES = {
    "hi-IN": "हैलो, हाँ जी, मैं मोहन बोल रहा हूँ, {who} से बात हो रही है क्या?",
    "en-IN": "Hello, this is Mohan speaking. Am I speaking with {who}?",
    "kn-IN": "ಹಲೋ, ನಾನು ಮೋಹನ್ ಮಾತಾಡ್ತಿದ್ದೀನಿ, {who} ಅವರ ಜೊತೆ ಮಾತಾಡ್ತಿದ್ದೀನಾ?",
    "te-IN": "హలో, నేను మోహన్ మాట్లాడుతున్నాను, {who} గారితో మాట్లాడుతున్నానా?",
    "ta-IN": "ஹலோ, நான் மோகன் பேசுறேன், {who} கிட்ட பேசுறேனா?",
}


def opening_line(vendor: dict[str, Any], language: str) -> str | None:
    """The exact first line, or None for a language without a fixed line (the model says it then)."""
    template = _LINES.get(language)
    if not template:
        return None
    who = f"{vendor['name']} जी" if language == "hi-IN" and vendor.get("category") == "priest" else vendor["name"]
    return template.format(who=who)


def voice() -> str:
    from conversation import TTS_VOICE
    return TTS_VOICE


def _path(text: str, language: str, rate: int) -> Path:
    key = hashlib.sha256(f"{MODEL}|{voice()}|{language}|{rate}|{text}".encode()).hexdigest()[:20]
    return CACHE / f"{key}.pcm"


def cached(text: str, language: str, rate: int) -> bytes | None:
    """Saved mono PCM16 audio for this line, or None."""
    try:
        return _path(text, language, rate).read_bytes() or None
    except OSError:
        return None


def _synthesize(text: str, language: str, rate: int) -> bytes:
    from gnani.tts import AudioConfig, GnaniTTSClient

    client = GnaniTTSClient(api_key=os.environ["GNANI_API_KEY"])
    audio = client.synthesize(text, voice=voice(), model=MODEL, language=language,
                              audio_config=AudioConfig(sample_rate=rate, container="wav"))
    with wave.open(io.BytesIO(audio)) as w:
        if w.getframerate() != rate or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"Unexpected TTS audio format {w.getparams()}")
        return w.readframes(w.getnframes())


def save(text: str, language: str, rate: int) -> bytes:
    """Synthesise and keep the line (blocking; run it off the event loop)."""
    pcm = _synthesize(text, language, rate)
    path = _path(text, language, rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pcm)
    tmp.replace(path)
    return pcm


async def save_later(text: str, language: str, rate: int) -> None:
    try:
        await asyncio.to_thread(save, text, language, rate)
    except Exception as error:  # a cache miss next time is fine; never break a call over it
        logger.warning(f"Could not save greeting audio ({type(error).__name__})")


def prewarm(rate: int = APP_RATE) -> int:
    """Generate every vendor's missing greeting. Returns how many were made."""
    import store

    made = 0
    for vendor in store.vendors():
        text = opening_line(vendor, vendor["language"])
        if text and cached(text, vendor["language"], rate) is None:
            save(text, vendor["language"], rate)
            made += 1
    return made


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    print(f"Generated {prewarm()} greetings in {CACHE}")
