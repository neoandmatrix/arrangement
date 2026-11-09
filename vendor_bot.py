"""A simulated vendor for the web demo: an LLM persona with a hidden price floor, in its own voice.

It answers Bandobast's ring like the vendor app does and joins the same PCM WebSocket call, so
Bandobast runs its real pipeline: the vendor's speech (Gnani Timbre, a second voice) reaches
Bandobast only as audio and is transcribed by Gnani Prisma. The vendor follows what Bandobast
says from the call's live transcript (CONTEXT_TAPS) instead of running a second speech-to-text
stream, because the Gnani account allows one streaming request at a time.

Both sides are mixed into one recording for the family to listen to afterwards.
"""

import asyncio
import io
import json
import os
import random
import re
import subprocess
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import websockets
from gnani.tts import AudioConfig, GnaniTTSClient
from loguru import logger
from openai import AsyncOpenAI

import app_calls
import catalog
import channels
import price_safety
import prompts
import store
from conversation import CONTEXT_TAPS, _transcript

RATE = 16000  # what the vendor sends and the recording's rate
FRAME = 640  # 20 ms of 16 kHz PCM16
VENDOR_VOICE = os.environ.get("DEMO_VENDOR_VOICE", "Avinash")  # Bandobast's Mohan speaks as Yashraj
MAX_CALL_SECONDS = 6 * 60
MAX_TURNS = 18
SILENCE_NUDGE = 25.0  # seconds of quiet after the vendor spoke before it asks "hello?"

HELLO = {"hi-IN": "हैलो? आप सुन रहे हैं?", "kn-IN": "ಹಲೋ? ಕೇಳಿಸ್ತಿದೆಯಾ?", "te-IN": "హలో? వినిపిస్తోందా?",
         "ta-IN": "ஹலோ? கேக்குதா?", "en-IN": "Hello? Are you still there?"}

# Typical rates when the family gives no budget (rupees; caterer per plate).
DEFAULT_RATES = {"priest": 11000, "caterer": 450, "decorator": 15000, "cake": 2400, "photographer": 18000, "tent_sound": 22000}


@dataclass
class Pricing:
    unit: str  # per_plate | total
    opening: int
    floor: int
    advance: int
    transport: int = 0


def pricing(category: str, brief: dict[str, Any], seed: str) -> Pricing:
    """The vendor's secret price card, derived from the family's budget so a deal is possible but not given."""
    rng = random.Random(seed)
    guests = max(int(brief.get("guests") or 1), 1)
    budget = (brief.get("budget") or {}).get(category)
    per_plate = category == "caterer"
    if per_plate:
        fair = budget / guests if budget else DEFAULT_RATES["caterer"]
        step = 10
    else:
        fair = budget or DEFAULT_RATES[category]
        step = 500
    def r(x: float) -> int:
        return int(round(x / step) * step)
    opening = r(fair * rng.uniform(1.15, 1.35))
    floor = r(fair * rng.uniform(0.92, 1.05))
    total = opening * guests if per_plate else opening
    transport = rng.choice([0, 0, 500, 1000]) if category in ("caterer", "decorator", "tent_sound") else 0
    advance = int(round(total * 0.25 / 500) * 500) or 500
    return Pricing("per_plate" if per_plate else "total", opening, min(floor, opening - step), advance, transport)


def persona_prompt(vendor: dict[str, Any], brief: dict[str, Any], price: Pricing, *, renegotiation: bool) -> str:
    language = prompts.LANGUAGE_NAMES.get(vendor["language"], "Hindi")
    category = catalog.CATEGORIES[vendor["category"]]["name"].lower()
    unit = "per plate" if price.unit == "per_plate" else "for the whole job"
    details = (brief.get("details") or {}).get(vendor["category"], "")
    transport = f"Transport is ₹{price.transport} extra; mention it only when asked about extra charges." if price.transport else "No transport charge."
    again = (
        "\nThis is a second call: the family asked them to call you back about your earlier quote. "
        "Grumble a little, then you may come down one more small step, still never below the floor."
        if renegotiation else ""
    )
    hinglish = (" Use everyday Hinglish in Devanagari, like रेट, पर प्लेट, एडवांस, ट्रांसपोर्ट, एक्स्ट्रा चार्ज, बुकिंग, "
                "गेस्ट, ज़ीरो, not formal Hindi like प्रति प्लेट, अतिरिक्त, मेहमान, शून्य."
                if vendor["language"] == "hi-IN" else "")
    return f"""You are the owner of {vendor['name']}, a {category} in {vendor['area']}, Bengaluru. You are answering a phone call.
Speak only {language}, the way a real small-business owner talks on the phone: short, one or two sentences, warm but businesslike.{hinglish}
Write rupee amounts as digits (for example 550 रुपये); the voice reads them aloud correctly, while spelling them out goes wrong. No markdown, emojis, lists or stage directions.

The caller is Mohan, calling for a family (he is an assistant from Bandobast). The job:
{catalog.function_label(brief)} on {brief.get('date')} at {brief.get('start_time')}, in {brief.get('area')}, about {brief.get('guests')} guests.
Food: {brief.get('cuisine') or 'not specified'}. Wishes: {details or 'none'}.

You are free that day and want the job. Your price card (secret):
- Opening price: ₹{price.opening} {unit}. GST included.
- {transport}
- Advance: ₹{price.advance}.
- You can come down, but never below ₹{price.floor} {unit}. Concede at most twice, in small steps, and only when they ask for a better price. Accept any offer at or above ₹{price.floor}.
  When they name a price, never offer less than what they asked: accept it, or meet them somewhere between.{again}

Your prices come only from your price card: if they suggest a number before you quote, still give your own opening price.
Never add up totals or mention anything they did not ask about. You are free on {brief.get('date')} (say the date as they did).
Answer only what you are asked, with one amount per answer; do not recite the whole quote at once.
Never mention your lowest price, and never offer a discount on your own: bring the price down only after they ask you to.
When they read details back, check every amount against what you last agreed. If any amount is different, say no and give the right amount; only say yes when all of it is right.
Stay consistent with what you said earlier on this call (for example, GST stays included).
If they say goodbye, say a short goodbye. Never say you are an AI or a simulation."""


def short_reply(text: str, sentences: int = 2) -> str:
    """At most two sentences, enforced in code: a rambling vendor that reads back totals and says goodbye
    on its own makes the call about the simulator instead of about Bandobast."""
    # "।", "!" and "?" end a sentence even without a following space; "." only before one (not "1,500.50").
    parts = re.split(r"(?<=[!?।])\s*(?=\S)|(?<=\.)\s+", " ".join(text.split()))
    return " ".join(parts[:sentences]).strip()


# Read-back answers, said by code (see readback_reply). Corrections put "no" in its own sentence and
# then name the charge and amount plainly, so Bandobast's price check can verify the corrected amount.
YES = {"hi-IN": "हाँ जी, सही है।", "kn-IN": "ಹೌದು, ಸರಿ ಇದೆ.", "te-IN": "అవును, సరిగ్గా ఉంది.",
       "ta-IN": "ஆமாம், சரிதான்.", "en-IN": "Yes, that's right."}
NO = {"hi-IN": "नहीं जी।", "kn-IN": "ಇಲ್ಲ.", "te-IN": "కాదు.", "ta-IN": "இல்லை.", "en-IN": "No."}
RUPEES = {"hi-IN": "रुपये", "kn-IN": "ರೂಪಾಯಿ", "te-IN": "రూపాయలు", "ta-IN": "ரூபாய்", "en-IN": "rupees"}
CHARGE = {
    "hi-IN": {"price_amount": "रेट", "per_plate": "पर प्लेट रेट", "transport_charge": "ट्रांसपोर्ट",
              "extra_charges_amount": "एक्स्ट्रा चार्ज", "advance_amount": "एडवांस"},
    "kn-IN": {"price_amount": "ಬೆಲೆ", "per_plate": "ಪ್ರತಿ ಪ್ಲೇಟ್ ಬೆಲೆ", "transport_charge": "ಸಾರಿಗೆ",
              "extra_charges_amount": "ಹೆಚ್ಚುವರಿ ಶುಲ್ಕ", "advance_amount": "ಮುಂಗಡ"},
    "te-IN": {"price_amount": "ధర", "per_plate": "ప్లేటుకు ధర", "transport_charge": "రవాణా",
              "extra_charges_amount": "అదనపు ఛార్జీలు", "advance_amount": "అడ్వాన్స్"},
    "ta-IN": {"price_amount": "விலை", "per_plate": "ஒரு தட்டு விலை", "transport_charge": "போக்குவரத்து",
              "extra_charges_amount": "கூடுதல் கட்டணம்", "advance_amount": "முன்பணம்"},
    "en-IN": {"price_amount": "the price", "per_plate": "the price per plate", "transport_charge": "transport",
              "extra_charges_amount": "extra charges", "advance_amount": "the advance"},
}


def stated_terms(text: str, question: str, category: str) -> dict[str, int]:
    """Amounts per charge in one utterance, read the same way Bandobast's price check reads them.

    Zeros are only taken from plain "no charge" answers: "GST is included in this price" is not a
    price of 0, and no base price is ever 0."""
    gst = price_safety._GST.search(text) or price_safety._GST.search(question)
    return {field: amount for field, (amount, _unit) in price_safety._assertions(text, question, category).items()
            if amount or (field != "price_amount" and not gst)}


def readback_reply(bandobast: str, terms: dict[str, int], category: str, language: str) -> str | None:
    """Answer a read-back of two or more amounts in code, so the simulated vendor never confirms a wrong
    number or muddles a right one. A single amount (a counter-offer) is left to the persona."""
    if "?" not in bandobast and "？" not in bandobast:
        return None
    said = stated_terms(bandobast, "", category)
    if len(said) < 2:
        return None
    known = {f: a for f, a in said.items() if f in terms}
    if not known:
        return None
    wrong = [f for f, a in known.items() if terms[f] != a]
    lang = language if language in YES else "hi-IN"
    if not wrong:
        return YES[lang]
    labels = CHARGE[lang]
    fixes = []
    for field in wrong:
        label = labels["per_plate"] if field == "price_amount" and category == "caterer" else labels[field]
        fixes.append(f"{label} {terms[field]} {RUPEES[lang]}{'।' if lang == 'hi-IN' else '.'}")
    return " ".join([NO[lang], *fixes])


class Recorder:
    """Both sides on one wall-clock timeline, mixed to mono 16 kHz."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.cursor = {"bandobast": 0, "vendor": 0}
        self.chunks: list[tuple[int, np.ndarray]] = []

    def add(self, who: str, pcm: bytes, rate: int) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if rate != RATE and len(samples):
            n = int(len(samples) * RATE / rate)
            samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
        now = int((time.monotonic() - self.t0) * RATE)
        start = max(self.cursor[who], now)
        self.chunks.append((start, samples))
        self.cursor[who] = start + len(samples)

    def save(self, path: Path) -> Path | None:
        if not self.chunks:
            return None
        mix = np.zeros(max(s + len(c) for s, c in self.chunks), dtype=np.float32)
        for start, chunk in self.chunks:
            mix[start:start + len(chunk)] += chunk
        pcm = np.clip(mix, -32768, 32767).astype(np.int16).tobytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        wav = path.with_suffix(".wav")
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        mp3 = path.with_suffix(".mp3")
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-b:a", "48k", str(mp3)],
                           check=True, timeout=60)
            wav.unlink()
            return mp3
        except (OSError, subprocess.SubprocessError):
            return wav  # no ffmpeg: keep the larger WAV


def _llm() -> tuple[AsyncOpenAI, str, dict[str, Any]]:
    """Groq, like Bandobast. DEMO_VENDOR_PROVIDER=openrouter (with DEMO_VENDOR_MODEL) is an opt-in alternative."""
    if os.environ.get("DEMO_VENDOR_PROVIDER") == "openrouter" and (key := os.environ.get("OPENROUTER_API_KEY")):
        model = os.environ.get("DEMO_VENDOR_MODEL") or os.environ.get("OPENROUTER_MODEL", "")
        return AsyncOpenAI(api_key=key, base_url="https://openrouter.ai/api/v1"), model, {"reasoning": {"effort": "minimal", "exclude": True}}
    model = os.environ.get("DEMO_VENDOR_MODEL") or os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    return AsyncOpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1"), model, extra


def _synthesize(text: str, language: str) -> bytes:
    client = GnaniTTSClient(api_key=os.environ["GNANI_API_KEY"])
    audio = client.synthesize(text, voice=VENDOR_VOICE, model="timbre-v3.0", language=language,
                              audio_config=AudioConfig(sample_rate=RATE, container="wav"))
    with wave.open(io.BytesIO(audio)) as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"Unexpected TTS audio format {w.getparams()}")
        return w.readframes(w.getnframes())


@dataclass(eq=False)  # registered in a set of sockets, so hashed by identity
class VendorBot:
    """Stands in for one vendor's app for the length of a demo run."""

    vendor: dict[str, Any]
    brief: dict[str, Any]
    price: Pricing
    audio_base: str  # ws://127.0.0.1:PORT
    recordings: Path
    renegotiation: bool = False
    call_id: str | None = None
    context: Any = None
    recording: Path | None = None
    said: list[dict[str, str]] = field(default_factory=list)  # what the vendor actually said
    terms: dict[str, int] = field(default_factory=dict)  # charge -> latest amount the vendor stated
    done: asyncio.Event = field(default_factory=asyncio.Event)

    # The vendor "app" is online while registered here: channels.channel_for() sees a live socket.
    def __enter__(self) -> "VendorBot":
        channels._sockets.setdefault(self.vendor["id"], set()).add(self)
        return self

    def __exit__(self, *_exc) -> None:
        channels._sockets.get(self.vendor["id"], set()).discard(self)

    async def send_json(self, message: dict[str, Any]) -> None:
        """channels rings this like an app connection."""
        if message.get("type") == "ring" and self.call_id is None:
            self.call_id = message["call_id"]
            app_calls.spawn(self._answer(message["call_id"]))

    def transcript(self) -> list[dict[str, str]]:
        return _transcript(self.context) if self.context is not None else []

    async def _answer(self, call_id: str) -> None:
        try:
            await asyncio.sleep(1.5)  # a ring or two
            pc = channels.pending(call_id)
            s = app_calls.AudioSession(call_id, (self.vendor["id"],), "enquiry", pc.session)
            app_calls._sessions[call_id] = s
            s.expiry = app_calls.spawn(app_calls.expire(call_id, channels.RING_SECONDS))
            ticket = app_calls.ticket(s, self.vendor["id"])
            CONTEXT_TAPS[call_id] = lambda context: setattr(self, "context", context)
            recorder = Recorder()
            async with websockets.connect(f"{self.audio_base}{ticket['audio_path']}?token={ticket['audio_token']}",
                                          max_size=None) as ws:
                await self._talk(ws, recorder)
            self.recording = await asyncio.to_thread(recorder.save, self.recordings / call_id)
        except Exception as error:
            logger.exception(f"Demo vendor {self.vendor['id']} failed on {call_id}: {error}")
        finally:
            CONTEXT_TAPS.pop(call_id, None)
            self.done.set()

    async def _say(self, text: str, history: list[dict[str, str]], outgoing: asyncio.Queue, state: dict[str, Any]) -> None:
        # What this vendor has actually said per charge; the latest statement wins (e.g. after a concession).
        question = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        self.terms.update(stated_terms(text, question, self.vendor["category"]))
        history.append({"role": "assistant", "content": text})
        self.said.append({"role": "vendor", "text": text})
        pcm = await asyncio.to_thread(_synthesize, text, self.vendor["language"])
        for i in range(0, len(pcm), FRAME):
            outgoing.put_nowait(pcm[i:i + FRAME].ljust(FRAME, b"\0"))
        state["speaking"] = True

    async def _talk(self, ws, recorder: Recorder) -> None:
        outgoing: asyncio.Queue[bytes] = asyncio.Queue()
        state = {"last_heard": 0.0, "speaking": False, "spoke_at": 0.0}
        ended = asyncio.Event()

        async def receive() -> None:
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        state["last_heard"] = time.monotonic()
                        recorder.add("bandobast", message, 24000)
                    elif json.loads(message).get("type") in ("ended", "error"):
                        break
            except websockets.ConnectionClosed:
                pass
            ended.set()

        async def send() -> None:
            # A phone line carries audio all the time; silence between turns keeps Bandobast's VAD honest.
            silence = bytes(FRAME)
            next_at = time.monotonic()
            while not ended.is_set():
                try:
                    frame = outgoing.get_nowait()
                    recorder.add("vendor", frame, RATE)
                    state["spoke_at"] = time.monotonic()
                    state["speaking"] = not outgoing.empty()
                except asyncio.QueueEmpty:
                    frame = silence
                    state["speaking"] = False
                try:
                    await ws.send(frame)
                except websockets.ConnectionClosed:
                    break
                next_at += 0.02
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        client, model, extra = _llm()
        history = [{"role": "system", "content": persona_prompt(self.vendor, self.brief, self.price, renegotiation=self.renegotiation)}]
        heard = 0  # Bandobast lines already answered
        nudges = 0
        started = time.monotonic()
        try:
            while not ended.is_set() and len(self.said) < MAX_TURNS and time.monotonic() - started < MAX_CALL_SECONDS:
                await asyncio.sleep(0.2)
                if state["speaking"] or not outgoing.empty() or time.monotonic() - state["last_heard"] < 1.0:
                    continue
                lines = [t["text"] for t in self.transcript() if t["role"] == "bandobast"]
                if len(lines) > heard:
                    history.append({"role": "user", "content": " ".join(lines[heard:])})
                    heard = len(lines)
                    nudges = 0
                elif self.said and nudges < 2 and time.monotonic() - max(state["last_heard"], state["spoke_at"]) > SILENCE_NUDGE:
                    # Bandobast went quiet after our answer; a person would say "hello?".
                    nudges += 1
                    await self._say(HELLO.get(self.vendor["language"], HELLO["hi-IN"]), history, outgoing, state)
                    continue
                else:
                    continue
                if answer := readback_reply(history[-1]["content"], self.terms, self.vendor["category"], self.vendor["language"]):
                    await self._say(answer, history, outgoing, state)
                    continue
                reply = await client.chat.completions.create(model=model, messages=history, temperature=0.6,
                                                             max_tokens=400, extra_body=extra or None)
                text = short_reply(reply.choices[0].message.content or "")
                if text:
                    await self._say(text, history, outgoing, state)
            if not ended.is_set():
                await ws.send(json.dumps({"type": "hangup"}))
                await asyncio.wait_for(ended.wait(), 10)
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            pass
        finally:
            ended.set()
            for task in tasks:
                task.cancel()
