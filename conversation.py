"""One voice conversation over any transport (Twilio phone or browser WebRTC).

audio in -> Gnani Prisma STT -> Groq LLM (+ tools) -> Gnani Timbre TTS -> audio out
"""

import asyncio
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    EndTaskFrame,
    Frame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    LLMContextFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.worker import ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.openrouter.llm import OpenRouterLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transcriptions.language import Language
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.empty_user_turn import DEFAULT_EMPTY_USER_TURN_INTERRUPTED_PROMPT, EmptyUserTurnConfig
from pipecat.transports.base_transport import BaseTransport
from pipecat.utils.text.transforms import strip_markdown
from pipecat.workers.runner import WorkerRunner
from pipecat_gnani import INDIC_AUTO_LANGUAGE_CODE, GnaniSTTService, GnaniTTSService

import catalog
import greeting
import prompts
import price_safety
import rules
import store
import supervisor
import workflow
import confirmation_rules
import confirmations
import checkin_dispatch
import checkin_policy

# Timbre v3.0 voices speak all 10 languages, so one voice works for every vendor.
TTS_MODEL = "timbre-v3.0"
TTS_VOICE = os.environ.get("BANDOBAST_VOICE", "Yashraj")  # a man's voice: the agent is "Mohan" (prompts.AGENT_NAME)

MODES = ("intake", "vendor_call", "checkin", "form", "vendor_update", "confirmation")

CALL_CONNECTED = "[The call has connected and the other person has picked up. Say your greeting now.]"

# call_id -> callback given the live LLM context when that call starts. The web demo uses it to
# show the transcript as it happens and to let its simulated vendor follow what Bandobast said.
CONTEXT_TAPS: dict[str, Callable[[LLMContext], None]] = {}

TOOL_NAMES = ("end_call", "save_brief", "record_quote", "record_checkin", "fill_form", "record_update", "start_confirmation", "record_confirmation")


@dataclass
class SessionConfig:
    mode: str
    channel: str  # "phone" | "browser"
    vendor_id: str | None = None
    checkin_id: int | None = None
    checkin_attempt_id: int | None = None
    confirmation_id: int | None = None
    renegotiate: bool = False  # vendor_call: calling back about their quote, at the family's request
    reconfirm: bool = False  # verification of quote charges, not confirmation of a booking
    event_id: int | None = None
    language: str = "hi-IN"
    draft: dict[str, Any] = field(default_factory=dict)
    on_result: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    call_id: str = field(default_factory=lambda: f"BR{uuid.uuid4().hex[:12]}")

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, channel: str, call_id: str | None = None) -> "SessionConfig":
        mode = data.get("mode", "vendor_call")
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode!r}")
        checkin_id = data.get("checkin_id")
        cfg = cls(
            mode=mode,
            channel=channel,
            vendor_id=data.get("vendor_id") or None,
            checkin_id=int(checkin_id) if checkin_id not in (None, "") else None,
            checkin_attempt_id=int(data['checkin_attempt_id']) if data.get('checkin_attempt_id') not in (None, '') else None,
            confirmation_id=int(data['confirmation_id']) if data.get('confirmation_id') not in (None, '') else None,
            renegotiate=str(data.get("renegotiate", "")).lower() in ("1", "true"),
            reconfirm=str(data.get("reconfirm", "")).lower() in ("1", "true"),
            event_id=int(data['event_id']) if data.get('event_id') not in (None, '') else None,
            language=data.get("language", "hi-IN"),
            draft=dict(data.get("draft") or {}),
        )
        if call_id:
            cfg.call_id = call_id
        return cfg


# Tool schemas

END_CALL = FunctionSchema(
    name="end_call",
    description="Hang up after saying goodbye. Only after the details are recorded, unless they asked to talk later.",
    properties={
        "reason": {"type": "string", "description": "Why the call is ending."},
        "callback_requested": {
            "type": "boolean",
            "description": "True only if they are busy and asked to be called back later.",
        },
        'callback_at': {'type': ['string', 'null'], 'description': 'For a check-in callback, the vendor-requested venue-local ISO date/time. Null if they did not give a time.'},
        "improper_request": {
            "type": "boolean",
            "description": "True only if they insisted on an improper demand (alcohol, gifts, off-record cash) after you declined once.",
        },
    },
    required=["reason", "callback_requested"],
)

SMART_TURN_STOP_SECS = float(os.environ.get("SMART_TURN_STOP_SECS", "1.2"))
MIN_INTERRUPT_WORDS = int(os.environ.get("MIN_INTERRUPT_WORDS", "3"))

EMPTY_TURN_PROMPT = (
    "They just spoke but the words were not recognised. In one short line, in their language, say you could "
    "not hear clearly and ask them to say it again (in Hindi: \"सॉरी, आवाज़ क्लियर नहीं आई, फिर से बोलिए?\"). "
    "If your last question is still unanswered, repeat it briefly. Don't call any tool."
)

ASK_AGAIN = {"hi-IN": "सॉरी, आवाज़ क्लियर नहीं आई, फिर से बोलिए?",
             "en-IN": "Sorry, I couldn't hear that clearly. Could you say it again?"}

EMPTY_TURN_PROMPTS = (EMPTY_TURN_PROMPT, DEFAULT_EMPTY_USER_TURN_INTERRUPTED_PROMPT)

# Fixed lines, filled with the call's price plan (rules.price_plan). Code picks them; the model only says them.
# When a rate is far above normal (price_safety.rechecks): first assume we misheard, then name our maximum.
HIGH_PRICE_LINES = (
    "सॉरी सर, मैंने रेट ठीक से सुना नहीं, आप रिपीट कर सकते हैं क्या?",
    "सर, ये अमाउंट बहुत हाई है, कुछ रीज़नेबल बोलिए, कितना कर सकते हैं?",
)
HIGH_PRICE_MEANING = (
    "Sorry sir, I didn't hear the rate properly, could you repeat it?",
    "Sir, this amount is very high, please say something reasonable. How much can you do?",
)
# Bargaining (rules.counter_offer): ask about 10% below their quote; when in range, settle it out loud.
BARGAIN_LINES = {
    "maximum": "सर, ये रेट बहुत हाई है, हम मैक्सिमम {amount} रुपये {unit} तक कर सकते हैं, इतने में हो जाएगा?",
    "counter": "सर, {amount} रुपये {unit} कर दीजिए, हो जाएगा?",
    "accept": "ठीक है सर, {amount} रुपये {unit} फाइनल।",
    "pass_on": "ठीक है सर, मैं {amount} रुपये {unit} फैमिली को बता दूँगा, वो डिसाइड करेंगे।",
}
BARGAIN_MEANING = {
    "maximum": "Sir, that rate is very high. The most we can do is {amount} rupees {unit_en}. Can you do it for that?",
    "counter": "Sir, could you do it for {amount} rupees {unit_en}?",
    "accept": "Okay sir, {amount} rupees {unit_en} is final.",
    "pass_on": "Okay sir, I'll tell the family {amount} rupees {unit_en}; they will decide.",
}
UNIT_WORDS = {"per_plate": ("पर प्लेट", "per plate"), "total": ("टोटल", "in total")}


def fixed_text(language: str, hindi: str, meaning: str, **values: Any) -> str | None:
    """The exact words code speaks itself (Hindi, English), or None where the model must translate."""
    unit, unit_en = UNIT_WORDS[values.pop("unit_key", "total")]
    filled = {**values, "unit": unit, "unit_en": unit_en}
    if language == "hi-IN":
        return hindi.format(**filled)
    if language == "en-IN":
        return meaning.format(**filled)
    return None


def fixed_line(language: str, hindi: str, meaning: str, **values: Any) -> str:
    """The instruction to say one fixed line, for languages code can't speak verbatim."""
    unit, unit_en = UNIT_WORDS[values.get("unit_key", "total")]
    text = fixed_text(language, hindi, meaning, **values)
    if text:
        return f'Say exactly this and nothing else: "{text}"'
    filled = {k: v for k, v in values.items() if k != "unit_key"}
    return f'Say only this, in their language, with the amount as digits: "{meaning.format(**filled, unit=unit, unit_en=unit_en)}"'


# The vendor asking about the job before quoting: size, people, place, what's needed.
ASKS_SCOPE = re.compile(r"कितना बड़ा|कितनी बड़ी|कितने (?:लोग|गेस्ट|आदमी|मेहमान)|कितनी जगह|जगह कितनी|कहाँ (?:है|पर)|कहां (?:है|पर)|"
                        r"क्या[- ]क्या|कैसे[- ]कैसे|\bhow (?:big|many|much space)\b|\bwhere is\b|\bwhat (?:all|exactly)\b", re.I)
def _amounts(text: str) -> list[int]:
    return [amount for _, _, amount in price_safety.amount_mentions(text)]


def amount_in_readback(question: str) -> bool:
    """Our line read amounts back (two or more numbers) and asked if that's right."""
    return len(_amounts(question)) >= 2


_AGREES = re.compile(r"हो जाएगा|हो जायेगा|कर देंगे|चलेगा|ठीक है|डन|\bdone\b|\bok(?:ay)?\b|ओके|हाँ|हां|\byes\b", re.I)


def agrees(text: str) -> bool:
    """Yes to our counter-offer, with no new number of their own and no "no"."""
    return bool(_AGREES.search(text)) and not _amounts(text) and not price_safety._DENIAL.search(text)


# The vendor asking when: date, muhurat, delivery or arrival time.
ASKS_TIME = re.compile(r"कितने बजे|किस (?:टाइम|time|समय)|कब (?:तक|देना|चाहिए|आना|पहुँचना|पहुंचना|है|का)|"
                       r"मुहूर्त क्या|मुहुर्त क्या|मुहूर्त कब|(?:टाइम|time|समय) क्या|कौन सी (?:तारीख|डेट|date)|"
                       r"\bwhat time\b|\bwhen (?:is|do|should)\b", re.I)
RATE_QUESTION = {True: "एक प्लेट का रेट कितना है?", False: "टोटल रेट कितना है?"}  # keyed by is-caterer
UNIT_WORDS = {"per_plate": ("पर प्लेट", "per plate"), "total": ("टोटल", "in total")}


def fixed_text(language: str, hindi: str, meaning: str, **values: Any) -> str | None:
    """The exact words code speaks itself (Hindi, English), or None where the model must translate."""
    unit, unit_en = UNIT_WORDS[values.pop("unit_key", "total")]
    filled = {**values, "unit": unit, "unit_en": unit_en}
    if language == "hi-IN":
        return hindi.format(**filled)
    if language == "en-IN":
        return meaning.format(**filled)
    return None


def fixed_line(language: str, hindi: str, meaning: str, **values: Any) -> str:
    """The instruction to say one fixed line, for languages code can't speak verbatim."""
    unit, unit_en = UNIT_WORDS[values.get("unit_key", "total")]
    text = fixed_text(language, hindi, meaning, **values)
    if text:
        return f'Say exactly this and nothing else: "{text}"'
    filled = {k: v for k, v in values.items() if k != "unit_key"}
    return f'Say only this, in their language, with the amount as digits: "{meaning.format(**filled, unit=unit, unit_en=unit_en)}"'


# Outbound calls that open with Mohan's fixed greeting (prompts._opening).
GREETED_MODES = ("vendor_call", "checkin", "confirmation")


def greeting_frames(text: str, language: str, rate: int) -> list[Frame]:
    """Saved greeting audio if we have it; otherwise the same text through live TTS, saved for next time."""
    pcm = greeting.cached(text, language, rate)
    if pcm is None:
        asyncio.get_running_loop().create_task(greeting.save_later(text, language, rate))
        return [TTSSpeakFrame(text, append_to_context=False)]
    context_id = f"greeting-{uuid.uuid4().hex[:8]}"
    return [TTSStartedFrame(context_id=context_id),
            TTSAudioRawFrame(audio=pcm, sample_rate=rate, num_channels=1, context_id=context_id),
            TTSStoppedFrame(context_id=context_id)]


# What each conversation must record before it may hang up.
REQUIRED_RESULT = {"intake": "save_brief", "vendor_call": "record_quote", "checkin": "record_checkin",
                   "form": "fill_form", "vendor_update": "record_update", "confirmation": "record_confirmation"}

SAVE_BRIEF = FunctionSchema(
    name="save_brief",
    description="Save the confirmed details of the family's function.",
    properties={
        "function_type": {"type": "string", "enum": list(catalog.FUNCTIONS)},
        "function_variant": {
            "type": "string",
            "enum": [v for f in catalog.FUNCTIONS.values() for v in f["variants"]],
        },
        "family_name": {"type": "string"},
        "contact_name": {"type": "string"},
        "city": {"type": "string"},
        "area": {"type": "string"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "start_time": {"type": "string", "description": "Muhurat start, HH:MM 24-hour"},
        "guests": {"type": "integer"},
        "cuisine": {"type": "string"},
        "rituals": {"type": "string"},
        "needs": {"type": "array", "items": {"type": "string", "enum": list(catalog.CATEGORIES)}},
        **{
            f"budget_{key}": {"type": ["integer", "null"], "description": f"Rupees for the {c['name'].lower()}"}
            for key, c in catalog.CATEGORIES.items()
        },
        **{
            f"details_{key}": {"type": ["string", "null"], "description": f"What they want: {c['details']}"}
            for key, c in catalog.CATEGORIES.items()
            if c["details"]
        },
    },
    required=["function_type", "function_variant", "family_name", "city", "area", "date", "start_time", "guests", "needs"],
)

RECORD_QUOTE = FunctionSchema(
    name="record_quote",
    description="Record the vendor's confirmed offer. Use null for anything not said.",
    properties={
        "available": {"type": ["boolean", "null"], "description": "Free on the date and time"},
        "price_amount": {"type": ["integer", "null"], "description": "Their current price in rupees"},
        "price_unit": {"type": "string", "enum": ["per_plate", "total"]},
        "transport_charge": {
            "type": ["integer", "null"],
            "description": "Transport or travel charge in rupees; 0 if they said none. Never put transport in extra charges.",
        },
        "extra_charges_amount": {
            "type": ["integer", "null"],
            "description": "Other charges in rupees besides transport: samagri, serving staff, flowers etc.",
        },
        "extra_charges_notes": {"type": ["string", "null"]},
        "extras_at_actuals": {"type": ["boolean", "null"],
                              "description": "True if they said some costs (e.g. materials) will be billed later at actual cost, "
                                             "with no fixed amount now. Then extra_charges_amount is 0 and the note says what."},
        "gst_included": {"type": ["boolean", "null"], "description": "True if their price includes GST; false if GST is extra"},
        "gst_percent": {"type": ["integer", "null"], "enum": [5, 12, 18, 28, None],
                        "description": "If GST is extra: the percent the vendor said (5, 12, 18 or 28). Null if included."},
        "advance_amount": {"type": ["integer", "null"], "description": "Rupees; 0 if no advance"},
        "includes_notes": {"type": ["string", "null"], "description": "What the price covers"},
        "notes": {"type": ["string", "null"], "description": "Anything else important, e.g. language, timing"},
    },
    required=[],  # code checks what is missing (quote_problems); a strict schema made Groq fail the whole call
)

RECORD_CHECKIN = FunctionSchema(
    name="record_checkin",
    description="Record the result of this follow-up call.",
    properties={
        "status": {"type": "string", "enum": ["confirmed", "delayed", "cancelled", "unsure"]},
        "eta": {"type": ["string", "null"], "description": "Arrival time HH:MM 24-hour, if given"},
        "notes": {"type": ["string", "null"]},
    },
    required=["status"],
)

FILL_FORM = FunctionSchema(
    name="fill_form",
    description="Update only form fields the family actually answered. After the final read-back is confirmed, set complete=true.",
    properties={**SAVE_BRIEF.properties, "complete": {"type": "boolean"}},
    required=["complete"],
)

RECORD_UPDATE = FunctionSchema(
    name="record_update",
    description="Save the vendor's confirmed update. A new base price is a proposal, never an agreed booking change.",
    properties={
        "kind": {"type": "string", "enum": ["price", "cancelled", "delayed", "note"]},
        "price_amount": {"type": ["integer", "null"], "description": "New base rate in the EXISTING quote's unit; null otherwise."},
        "price_unit": {"type": ["string", "null"], "enum": ["per_plate", "total", None],
                       "description": "Confirm the unit with the vendor for a price change. Must match the existing quote."},
        "eta": {"type": ["string", "null"], "description": "HH:MM for a delay"},
        "notes": {"type": "string", "description": "Brief factual summary in the vendor's language"},
    },
    required=["kind", "notes"],
)

START_CONFIRMATION = FunctionSchema(name='start_confirmation',
    description='After the greeting is answered, start the code-generated read-back of the family-approved booking terms. Do not speak these questions yourself.',
    properties={}, required=[])

RECORD_CONFIRMATION = FunctionSchema(name='record_confirmation',
    description='Process their answer to the last code-generated question. Code validates a spoken yes before advancing. A change or refusal never confirms a booking.',
    properties={
        'status': {'type': 'string', 'enum': ['agreed', 'changed', 'declined', 'callback', 'repeat', 'ai_disclosure']},
        'change_type': {'type': ['string', 'null'], 'enum': ['quote', 'event', None],
                        'description': 'For changes: quote = price/advance/service coverage; event = date/time/venue change.'},
        'notes': {'type': ['string', 'null'], 'description': 'What changed, why declined, or the requested callback time.'},
    }, required=['status'])


def make_llm() -> OpenRouterLLMService | GroqLLMService:
    """Groq by default (lowest latency); LLM_PROVIDER=openrouter for Gemini Flash via OpenRouter."""
    if os.environ.get("LLM_PROVIDER", "groq") == "openrouter":
        settings = OpenRouterLLMService.Settings(
            model=os.environ.get("OPENROUTER_MODEL", "google/gemini-3.8-flash"),
            temperature=0.3,
        )
        # Gemini thinks before answering, which costs call latency. "off" disables it;
        # "exclude" keeps any reasoning out of the reply so it is never spoken.
        effort = os.environ.get("OPENROUTER_REASONING", "low")
        reasoning = {"enabled": False} if effort == "off" else {"effort": effort, "exclude": True}
        settings.extra = {"extra_body": {"reasoning": reasoning}}
        # e.g. OPENROUTER_PROVIDER=groq: gpt-oss on Groq's fast servers, without Groq's free-tier
        # limit of 8,000 tokens a minute (a call needs far more).
        if upstream := os.environ.get("OPENROUTER_PROVIDER"):
            settings.provider = {"order": [upstream], "allow_fallbacks": True}
        return OpenRouterLLMService(api_key=os.environ["OPENROUTER_API_KEY"], settings=settings)

    settings = GroqLLMService.Settings(model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))
    # Reasoning models think before replying, which adds delay; non-reasoning models reject this.
    if effort := os.environ.get("GROQ_REASONING_EFFORT", "low"):
        settings.reasoning_effort = effort
    return GroqLLMService(api_key=os.environ["GROQ_API_KEY"], settings=settings)


def _plan(cfg: SessionConfig) -> tuple[str, list[FunctionSchema], str, int | None]:
    """System prompt, tools, conversation language and event id for this session."""
    if cfg.mode == "intake":
        return prompts.intake_prompt(), [SAVE_BRIEF, END_CALL], "hi-IN", None

    if cfg.mode == "form":
        return prompts.form_prompt(cfg.language, cfg.draft), [FILL_FORM, END_CALL], cfg.language, None

    if cfg.mode == 'confirmation':
        if not cfg.confirmation_id:
            raise ValueError('Booking confirmation ID is required')
        record = store.confirmation(cfg.confirmation_id)
        if cfg.vendor_id not in (None, record['vendor_id']) or cfg.event_id not in (None, record['event_id']):
            raise ValueError('Wrong vendor or function for this confirmation')
        cfg.vendor_id, cfg.event_id = record['vendor_id'], record['event_id']
        confirmations.bind(cfg.confirmation_id, cfg.call_id, vendor_id=cfg.vendor_id, event_id=cfg.event_id)
        vendor = store.vendor(cfg.vendor_id)
        return prompts.confirmation_prompt(vendor, record['terms']), [START_CONFIRMATION, RECORD_CONFIRMATION, RECORD_QUOTE, END_CALL], vendor['language'], cfg.event_id

    event_id, brief = store.event(cfg.event_id) if cfg.event_id else workflow.require_event()
    if cfg.mode == "vendor_update":
        vendor = store.vendor(cfg.vendor_id)
        quote = store.latest_quotes(event_id).get(vendor["id"])
        return prompts.update_prompt(vendor, brief, quote), [RECORD_UPDATE, END_CALL], vendor["language"], event_id
    if cfg.mode == "vendor_call":
        vendor = store.vendor(cfg.vendor_id)
        quote = store.latest_quotes(event_id).get(vendor["id"])
        if cfg.reconfirm:
            if not quote:
                raise ValueError(f"No quote from {vendor['name']} to recheck")
            prompt = prompts.reconfirmation_prompt(vendor=vendor, brief=brief, quote=quote)
        elif cfg.renegotiate:
            if not quote:
                raise ValueError(f"No quote from {vendor['name']} to renegotiate")
            prompt = prompts.renegotiation_prompt(vendor=vendor, brief=brief, quote=quote)
        else:
            prompt = prompts.vendor_prompt(vendor=vendor, brief=brief)
        return prompt, [RECORD_QUOTE, END_CALL], vendor["language"], event_id

    checkin = store.checkin(cfg.checkin_id)
    if cfg.event_id not in (None, checkin['event_id']) or cfg.vendor_id not in (None, checkin['vendor_id']):
        raise ValueError('Wrong function or vendor for this check-in')
    cfg.event_id = event_id = checkin['event_id']
    _, brief = store.event(event_id)
    cfg.vendor_id = checkin["vendor_id"]
    if cfg.checkin_attempt_id is None:
        # Local browser/CLI testing uses the same durable claim as automatic calls.
        cfg.checkin_attempt_id = checkin_dispatch.enqueue(checkin['id'], manual=True, origin='browser')
        if not store.claim_checkin_attempt(cfg.checkin_attempt_id, checkin_policy.now().isoformat(timespec='seconds')):
            raise ValueError('Another caller already claimed this check-in')
    checkin_dispatch.bind(cfg.checkin_attempt_id, cfg.checkin_id, cfg.call_id, cfg.vendor_id, cfg.event_id)
    vendor = store.vendor(checkin["vendor_id"])
    quote = store.latest_quotes(event_id).get(vendor["id"])
    prompt = prompts.checkin_prompt(vendor=vendor, brief=brief, label=checkin["label"], quote=quote)
    return prompt, [RECORD_CHECKIN, END_CALL], vendor["language"], event_id


async def run_conversation(
    transport: BaseTransport, cfg: SessionConfig, *, sample_rate_in: int, sample_rate_out: int
) -> None:
    try:
        await _run_conversation(transport, cfg, sample_rate_in=sample_rate_in, sample_rate_out=sample_rate_out)
    finally:
        if cfg.mode == 'checkin' and cfg.checkin_attempt_id:
            try:
                checkin_dispatch.fail(cfg.checkin_attempt_id, 'Voice call ended without a saved check-in result')
            except LookupError:
                pass


async def _run_conversation(
    transport: BaseTransport, cfg: SessionConfig, *, sample_rate_in: int, sample_rate_out: int
) -> None:
    """Run one conversation until hang-up, then save the call record."""
    system_prompt, tools, language, event_id = _plan(cfg)
    log = logger.bind(call=cfg.call_id)

    stt = GnaniSTTService(
        api_key=os.environ["GNANI_API_KEY"],
        sample_rate=sample_rate_in,
        settings=GnaniSTTService.Settings(
            model="prisma-v3.0",
            # Vendors often switch between their language, Hindi and English mid-call.
            language=INDIC_AUTO_LANGUAGE_CODE,
            language_candidates=list(dict.fromkeys([Language(language), Language.HI_IN, Language.EN_IN])),
            format="transcribe",  # numbers as digits, easier to extract prices
        ),
    )
    tts = GnaniTTSService(
        api_key=os.environ["GNANI_API_KEY"],
        model=TTS_MODEL,
        voice_id=TTS_VOICE,
        sample_rate=sample_rate_out,
        # "auto" reads the script, so Hindi replies to a Kannada vendor are still pronounced right.
        settings=GnaniTTSService.Settings(language="auto"),
        # Only the voice sees these; the transcript keeps what the LLM wrote.
        text_transforms=[("*", strip_markdown), ("*", drop_unspeakable)],
    )
    llm = make_llm()
    log.info(f"LLM: {type(llm).__name__} {llm._settings.model}")

    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt}],
        tools=ToolsSchema(standard_tools=tools),
    )
    if tap := CONTEXT_TAPS.get(cfg.call_id):
        tap(context)
    outcome: dict[str, Any] = {}
    confirmation_state: dict[str, Any] = {'controlled': False, 'started': False, 'index': 0, 'ready_turn': None, 'tts_context': None}
    booking_record = store.confirmation(cfg.confirmation_id) if cfg.mode == 'confirmation' else None
    booking_questions = confirmation_rules.questions(booking_record['terms'], language) if booking_record else []
    draft = dict(cfg.draft)
    previous_quote = booking_record['approved_quote'] if booking_record else store.latest_quotes(event_id).get(cfg.vendor_id) if cfg.renegotiate or cfg.reconfirm else None
    if previous_quote and previous_quote.get("initial_price_verified") is True and not price_safety.issues(previous_quote, store.event(event_id)[1], store.vendor(cfg.vendor_id)["category"]):
        opening = previous_quote.get("opening_quote")
        if isinstance(opening, dict) and not price_safety.issues(opening, store.event(event_id)[1], store.vendor(cfg.vendor_id)["category"]):
            outcome["opening_quote"] = opening

    def user_turns() -> int:
        # An unrecognised utterance still counts as their turn: they did speak, so our "please repeat"
        # must not be held back as a second question in a row.
        return sum((_is_spoken_turn(m) and m["role"] == "user") or _is_empty_turn(m) for m in context.get_messages())

    def other_side_has_spoken() -> bool:
        return user_turns() > 0

    question_gate = OneQuestionPerTurn(user_turns=user_turns, english_ok=language == "en-IN", tool_only=lambda: confirmation_state['controlled'])
    auto_hangup = AutoHangup(user_turns=user_turns, awaiting_answer=lambda: question_gate.awaiting_answer, outcome=outcome)

    if booking_record:
        @tts.event_handler('on_tts_request')
        async def on_confirmation_tts(_tts, context_id, text):
            expected = confirmation_state.get('question', '')
            if expected and ' '.join(text.split()) == ' '.join(expected.split()):
                confirmation_state['tts_context'] = context_id

    async def ask_approved_question() -> None:
        index = confirmation_state['index']
        text = booking_questions[index]['text']
        confirmation_state.update(question=text, ready_turn=None, tts_context=None, controlled=True)
        context.add_message({'role': 'assistant', 'content': text})
        question_gate.mark_question()
        frame = TTSSpeakFrame(text, append_to_context=False)
        frame.interruptible = False
        await task.queue_frames([frame])

    async def start_confirmation(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        if not other_side_has_spoken():
            await reject(fn, ['Greet the vendor and wait for their answer before starting'])
            return
        try:
            confirmations.current(cfg.confirmation_id)
        except ValueError as error:
            await reject(fn, [str(error)])
            return
        if confirmation_state['started']:
            await fn.result_callback({'ok': False, 'error': 'Already started; process the latest answer, do not restart.'}, properties=FunctionCallResultProperties(run_llm=False))
            return
        confirmation_state['started'] = True
        await ask_approved_question()
        await fn.result_callback({'ok': True, 'next': 'The approved question is being spoken. Wait for their answer, then call record_confirmation.'}, properties=FunctionCallResultProperties(run_llm=False))

    async def advance_confirmation(*, run_llm_when_complete: bool = False) -> bool:
        if not confirmation_state['started'] or confirmation_state['ready_turn'] is None:
            raise ValueError('Start the approved read-back and wait for the question to finish before accepting an answer')
        last = next(m for m in reversed(context.get_messages()) if _is_spoken_turn(m) and m['role'] == 'user')
        complete = confirmations.accept_answer(cfg.confirmation_id, cfg.call_id, confirmation_state['index'], _text(last),
            user_turn=user_turns(), ready_turn=confirmation_state['ready_turn'], question=confirmation_state['question'], tts_context=confirmation_state['tts_context'])
        if complete:
            confirmation_state['controlled'] = False
            mark_recorded('record_confirmation')
            message = ('Code confirmed standby availability only. The vendor is NOT booked and no advance is due now. Say this clearly, thank them and end_call.'
                if booking_record['purpose'] == 'standby' else 'Code confirmed every approved booking term. Tell the vendor the replacement booking is confirmed.' if booking_record['purpose'] == 'replacement' else 'Code confirmed every approved booking term. Tell the vendor the booking is confirmed.')
            context.add_message({'role': 'system', 'content': message + ' Say goodbye and end_call. If the vendor now changes or withdraws terms, record changed or declined instead; do not ignore a last-minute revision.'})
            if cfg.on_result:
                await cfg.on_result({'type': 'standby_confirmed' if booking_record['purpose'] == 'standby' else 'booking_confirmed', 'confirmation_id': cfg.confirmation_id})
            if run_llm_when_complete:
                await task.queue_frames([LLMRunFrame()])
        else:
            confirmation_state['index'] += 1
            await ask_approved_question()
        return complete

    async def record_confirmation(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        if not other_side_has_spoken():
            await reject(fn, ['The vendor must speak first'])
            return
        status = fn.arguments.get('status')
        notes = str(fn.arguments.get('notes') or '').strip()
        try:
            if status in ('repeat', 'ai_disclosure'):
                record = store.confirmation(cfg.confirmation_id)
                latest = store.latest_confirmation(event_id, record['category'], record['role'])
                if not latest or latest['id'] != cfg.confirmation_id:
                    raise ValueError('The booking confirmation was superseded')
                if status == 'ai_disclosure':
                    text = confirmation_rules.DISCLOSURE.get(language, confirmation_rules.DISCLOSURE['en-IN'])
                    context.add_message({'role': 'assistant', 'content': text})
                    await task.queue_frames([TTSSpeakFrame(text, append_to_context=False)])
                if record['status'] == 'confirmed':
                    await fn.result_callback({'ok': True, 'next': 'Standby availability was agreed, not a booking; answer honestly and end_call.' if record['purpose'] == 'standby' else 'The booking was already confirmed; answer honestly and end_call.'})
                    return
                if record['status'] != 'calling':
                    await fn.result_callback({'ok': False, 'next': 'This booking is not confirmed. Continue collecting the revised quote if needed, otherwise say goodbye. Do not read back or confirm superseded terms.'})
                    return
                if not confirmation_state['started']:
                    confirmation_state['started'] = True
                await ask_approved_question()
                await fn.result_callback({'ok': True, 'next': 'The approved question is repeated. Wait for their answer.'}, properties=FunctionCallResultProperties(run_llm=False))
                return
            if status == 'agreed':
                complete = await advance_confirmation()
                if complete:
                    await fn.result_callback({'ok': True, 'next': 'Standby terms are agreed, but they are NOT booked and no advance is due now. Thank them and end_call.' if booking_record['purpose'] == 'standby' else 'All approved terms were confirmed. Tell them the booking is confirmed, say goodbye and end_call.'})
                else:
                    await fn.result_callback({'ok': True, 'next': 'Wait for their answer to the next approved question, then record_confirmation.'}, properties=FunctionCallResultProperties(run_llm=False))
                return
            if status not in ('changed', 'declined', 'callback') or not notes:
                raise ValueError('Record what changed, why they declined, or when they requested a callback')
            if status == 'changed':
                change_type = fn.arguments.get('change_type')
                if change_type not in ('quote', 'event'):
                    raise ValueError('Identify whether quote terms or event date/time/venue changed')
                update_id = confirmations.change(cfg.confirmation_id, cfg.call_id, notes, event_change=change_type == 'event')
                confirmation_state.update(controlled=False, changed=True, change_notes=notes)
                if cfg.on_result:
                    await cfg.on_result({'type': 'booking_pending_review'})
                if update_id:
                    mark_recorded('record_confirmation')
                    await fn.result_callback({'ok': True, 'next': 'The date/time/venue change was sent to the family. No booking is confirmed. Say goodbye and end_call.'})
                else:
                    await fn.result_callback({'ok': True, 'next': 'Collect the full revised quote and service coverage, one question at a time. Use record_quote; it must pass price checks and will wait for family approval. Do not negotiate or confirm the booking.'})
                return
            if store.confirmation(cfg.confirmation_id)['status'] == 'confirmed':
                if status == 'declined':
                    workflow.record_vendor_update(event_id, cfg.vendor_id, {'kind': 'cancelled', 'notes': notes}, _transcript(context), call_id=cfg.call_id)
                else:
                    confirmation_state['controlled'] = False
                    await fn.result_callback({'ok': True, 'next': 'The booking was already confirmed; acknowledge their request to talk later, say goodbye and end_call.'})
                    return
            else:
                confirmations.finish(cfg.confirmation_id, status, notes, call_id=cfg.call_id)
            confirmation_state['controlled'] = False
            mark_recorded('record_confirmation')
            if status == 'declined' and cfg.on_result:
                await cfg.on_result({'type': 'booking_declined'})
            await fn.result_callback({'ok': True, 'next': 'No booking is confirmed. Say goodbye and end_call.'})
        except (ValueError, LookupError) as error:
            latest = store.latest_confirmation(event_id, store.vendor(cfg.vendor_id)['category'], store.confirmation(cfg.confirmation_id)['role'])
            if not latest or latest['id'] != cfg.confirmation_id or store.confirmation(cfg.confirmation_id)['status'] != 'calling':
                confirmation_state['controlled'] = False
                outcome['end_reason'] = 'Booking confirmation was superseded or changed'
                await fn.result_callback({'ok': False, 'error': str(error)}, properties=FunctionCallResultProperties(run_llm=False))
                await fn.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
                return
            if status == 'agreed' and confirmation_state['started'] and confirmation_state['ready_turn'] is not None:
                await ask_approved_question()
            await fn.result_callback({'ok': False, 'error': str(error), 'next': 'Do not claim confirmation. If the vendor changed terms use changed; if they need a callback record it.'}, properties=FunctionCallResultProperties(run_llm=False) if confirmation_state['controlled'] else None)

    async def answer_pending(fn: FunctionCallParams) -> bool:
        """Hard rule: no saving or hanging up while our question is unanswered (e.g. the read-back)."""
        if not question_gate.awaiting_answer:
            return False
        if fn.function_name == "record_quote" and outcome.get("readback_confirmed_at") == user_turns():
            # They just said yes to the read-back; a stray "?" in the same reply as the save (seen live:
            # "धन्यवाद। परिवार ... ?") must not throw the confirmed quote away.
            return False
        log.warning(f"{fn.function_name} refused: waiting for their answer")
        await fn.result_callback(
            {"ok": False, "error": f"You just asked them a question. Wait for their answer, then call {fn.function_name} again."},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        return True

    async def end_call(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        # Hard rule: never hang up before the other person has said anything.
        if not other_side_has_spoken():
            log.warning("end_call refused: the other side hasn't spoken yet")
            await fn.result_callback({"ok": False, "error": "The call has just started. Greet them first."})
            return
        # Hard rule: don't hang up before the job is done, unless they asked to be called later.
        required = REQUIRED_RESULT[cfg.mode]
        if required not in outcome.get("recorded", set()) and not fn.arguments.get("callback_requested") and not fn.arguments.get("improper_request"):
            log.warning(f"end_call refused: {required} not done yet")
            await fn.result_callback(
                {
                    "ok": False,
                    "error": f"Not done yet: you must confirm the details and call {required} first. "
                    "Continue with the next unanswered question.",
                }
            )
            return
        outcome["end_reason"] = fn.arguments.get("reason", "")
        if cfg.mode == 'checkin' and fn.arguments.get('callback_requested'):
            callback_at = None
            if fn.arguments.get('callback_at'):
                try:
                    callback_at = checkin_policy.venue_time(fn.arguments['callback_at'])
                    if callback_at <= checkin_policy.now():
                        raise ValueError('Callback time is in the past')
                except (ValueError, TypeError):
                    await reject(fn, ['a future callback date/time in venue time (IST)'])
                    return
            checkin_dispatch.fail(cfg.checkin_attempt_id, outcome['end_reason'] or 'Vendor requested a callback', status='callback', callback_at=callback_at)
        if cfg.mode == 'confirmation' and fn.arguments.get('callback_requested'):
            confirmations.finish(cfg.confirmation_id, 'callback', outcome['end_reason'] or 'Vendor requested a callback', call_id=cfg.call_id)
        log.info(f"end_call: {outcome['end_reason']}")
        await fn.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
        await fn.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    async def reject(fn: FunctionCallParams, problems: list[str]) -> None:
        log.warning(f"{fn.function_name} rejected: {problems}")
        await fn.result_callback(
            {"ok": False, "error": "Not saved. Still missing or invalid: " + "; ".join(problems) + ". Ask about these, one at a time. "
             f"As soon as they answer, call {fn.function_name} again with everything, including what they just said."}
        )

    def mark_recorded(name: str) -> None:
        outcome.setdefault("recorded", set()).add(name)
        if name == REQUIRED_RESULT[cfg.mode] and cfg.mode not in ("intake", "form"):
            auto_hangup.arm()

    async def save_brief(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        args = dict(fn.arguments)
        if problems := brief_problems(args):
            await reject(fn, problems)
            return
        budget = {c: args.pop(f"budget_{c}", None) for c in catalog.CATEGORIES}
        details = {c: args.pop(f"details_{c}", None) for c in catalog.CATEGORIES}
        brief = {
            **args,
            "budget": {k: v for k, v in budget.items() if v and k in args["needs"]},
            "details": {k: v for k, v in details.items() if v and k in args["needs"]},
            "family_language": "hi-IN",
        }
        outcome["event_id"] = store.save_event(brief)
        mark_recorded("save_brief")
        log.info(f"save_brief -> event {outcome['event_id']}: {brief}")
        await fn.result_callback({"ok": True})

    async def fill_form(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        args = {k: v for k, v in fn.arguments.items() if k in SAVE_BRIEF.properties and k not in ("city", "area")}
        merged = {**draft, **args}
        # Validate partial fields without allowing a model to pick or move the map pin.
        problems = []
        if "function_type" in args and args["function_type"] not in catalog.FUNCTIONS:
            problems.append("a supported occasion")
        function = catalog.FUNCTIONS.get(merged.get("function_type", ""))
        if "function_variant" in args and (not function or args["function_variant"] not in function["variants"]):
            problems.append("a variant of the selected occasion")
        if "needs" in args and (not isinstance(args["needs"], list) or any(n not in catalog.CATEGORIES for n in args["needs"])):
            problems.append("supported services")
        if "guests" in args and (type(args["guests"]) is not int or args["guests"] <= 0):
            problems.append("a positive guest count")
        for key, value in args.items():
            if key.startswith("budget_") and value is not None and (type(value) is not int or value < 0):
                problems.append("a non-negative budget")
        if fn.arguments.get("complete"):
            problems += brief_problems({**merged, "city": "Bengaluru", "area": "Bengaluru"})
        if problems:
            await reject(fn, problems)
            return
        draft.update(args)
        if cfg.on_result:
            await cfg.on_result({"type": "form", "draft": draft, "complete": bool(fn.arguments.get("complete"))})
        if fn.arguments.get("complete"):
            mark_recorded("fill_form")
        await fn.result_callback({"ok": True, "next": "Continue asking the next missing detail, or say review the form and select the venue before submitting if complete."})

    async def record_update(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        if "record_update" in outcome.get("recorded", set()):
            await fn.result_callback({"ok": True, "next": "Already saved. Say goodbye and end_call."})
            return
        if not other_side_has_spoken():
            await reject(fn, ["the vendor has not spoken yet; greet them and ask what changed"])
            return
        dialogue = _transcript(context)
        try:
            update_id = workflow.record_vendor_update(event_id, cfg.vendor_id, dict(fn.arguments), dialogue, call_id=cfg.call_id)
        except ValueError as e:
            await reject(fn, [str(e)])
            return
        mark_recorded("record_update")
        if cfg.on_result:
            await cfg.on_result({"type": "update_saved", "update_id": update_id})
        await fn.result_callback({"ok": True, "next": "Tell them the family has been notified; a revised price still needs family approval. Say goodbye and end_call."})

    async def record_quote(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        if not other_side_has_spoken():
            await reject(fn, ["the vendor must speak before any quote is recorded"])
            return
        category = store.vendor(cfg.vendor_id)["category"]
        if outcome.get("rate_settled") is not None and fn.arguments.get("available") is not False:
            # Code settled the rate in the call ("ठीक है सर, 14000 रुपये टोटल फाइनल।"): that is the price.
            fn.arguments["price_amount"] = outcome["rate_settled"]
            fn.arguments["price_unit"] = outcome.get("rate_unit") or fn.arguments.get("price_unit")
        fill_stated_charges(fn.arguments, _transcript(context), category)
        if problems := quote_problems(fn.arguments, category):
            await reject(fn, problems)
            return
        _, brief = store.event(event_id)
        quote = checked_quote(dict(fn.arguments), _transcript(context), brief, category, cfg.call_id, previous_quote)
        if outcome.get("rate_settled") is not None and quote.get("price_amount") == outcome["rate_settled"]:
            # The settled rate may be our counter they agreed to ("हाँ, हो जाएगा"): it's settled either way.
            quote["amount_checks"]["vendor_numbers"] = sorted(set(quote["amount_checks"]["vendor_numbers"]) | {outcome["rate_settled"]})
            quote["unverified"] = [f for f in quote["unverified"] if f != "price_amount"]
        problems = price_safety.issues(quote, brief, category)
        if problems:
            # Bounded recovery, not bounded safety: unresolved quotes remain blocked even after retries.
            outcome["quote_rejections"] = outcome.get("quote_rejections", 0) + 1
            if outcome["quote_rejections"] <= 1:  # ask once; then save it blocked rather than loop (seen live: 4 repeats)
                await reject(fn, [p["reason"] + ". Ask only about this charge and have them repeat its amount in rupees, not just yes" for p in problems])
                return
        if not problems and quote.get("available"):
            # A hallucinated or suspicious first attempt must never become advertised savings.
            opening = outcome.setdefault("opening_quote", dict(quote))
            quote["initial_price_amount"] = opening["price_amount"]
            quote["initial_price_unit"] = opening["price_unit"]
            quote["initial_price_verified"] = True
            quote["opening_quote"] = opening
            if cfg.mode != 'confirmation' and not cfg.reconfirm and await negotiate_first(fn, category, quote):
                return
        if cfg.mode == 'confirmation' and not confirmation_state.get('changed'):
            await reject(fn, ['Use start_confirmation and record_confirmation for unchanged approved terms; record_quote is only for a reported change'])
            return
        result = workflow.record_quote(event_id, cfg.vendor_id, cfg.call_id, quote)
        mark_recorded("record_quote")
        if cfg.mode == 'confirmation':
            if not result.get('update_id'):
                update_id = store.save_update(event_id, cfg.vendor_id, 'confirmation_change', {
                    'notes': confirmation_state.get('change_notes', 'Vendor reported a booking change without a full revised quote'),
                    'confirmation_id': cfg.confirmation_id, 'review_note': 'No changed quote was provided. Review the request before retrying original booking terms.'}, 'pending')
                result['update_id'] = update_id
            mark_recorded('record_confirmation')
        log.info(f"record_quote {cfg.vendor_id}: {quote}")
        if result.get("update_id") and cfg.on_result:
            await cfg.on_result({"type": "update_saved", "update_id": result["update_id"]})
        await fn.result_callback({"ok": True, "bookable": not problems, "next":
            "The family must review the corrected charges; nothing agreed was changed." if result.get("update_id") else
            "The quote is saved; the family will review it. Thank them, say the family will get back to them, and say goodbye. "
            "Don't ask anything again." if problems else "The quote is recorded; the family will decide."})

    async def record_checkin(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        if not other_side_has_spoken():
            await reject(fn, ['The vendor must speak before a check-in can be recorded'])
            return
        args = fn.arguments
        if args.get('status') == 'confirmed':
            last = next(m for m in reversed(context.get_messages()) if _is_spoken_turn(m) and m['role'] == 'user')
            if not confirmation_rules.agreed(_text(last)):
                await reject(fn, ['Read back the check-in result and arrival time, then obtain a clear spoken yes before recording confirmed'])
                return
        if args.get("status") == "delayed" and not _valid_time(args.get("eta")):
            await reject(fn, ["the arrival time (ETA) as HH:MM"])
            return
        try:
            risk = workflow.record_checkin(cfg.checkin_id, args['status'], args.get('eta'), args.get('notes'), attempt_id=cfg.checkin_attempt_id, call_id=cfg.call_id)
        except (KeyError, LookupError, ValueError) as error:
            await fn.result_callback({'ok': False, 'error': str(error)}, properties=FunctionCallResultProperties(run_llm=False))
            outcome['end_reason'] = 'Check-in attempt no longer current or result invalid'
            await fn.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
            return
        mark_recorded("record_checkin")
        log.info(f"record_checkin {cfg.checkin_id}: {args} -> risk {risk}")
        # The vendor isn't told about a switch to the backup; that's for the family.
        await fn.result_callback({"ok": True})

    def high_price_line(asks: int, category: str, brief: dict[str, Any]) -> tuple[str | None, str]:
        """(words code speaks itself, or None; the instruction for the model otherwise)."""
        plan = rules.price_plan(category, brief)
        values = {"unit_key": plan["unit"]}
        return (fixed_text(language, HIGH_PRICE_LINES[asks], HIGH_PRICE_MEANING[asks], **values),
                fixed_line(language, HIGH_PRICE_LINES[asks], HIGH_PRICE_MEANING[asks], **values))

    def speak_fixed(text: str) -> list[Frame]:
        """Mohan says a code-picked line word for word, without an LLM turn, and then waits for the answer."""
        question_gate.mark_question()
        return [TTSSpeakFrame(text)]

    def ask_to_repeat() -> list[Frame]:
        """Spoken by code in Hindi and English; other languages get the model to say it."""
        if language in ASK_AGAIN:
            return speak_fixed(ASK_AGAIN[language])
        context.add_message({"role": "developer", "content": EMPTY_TURN_PROMPT})
        return [LLMRunFrame()]

    def advance_hint() -> list[Frame] | None:
        """An advance above the whole quote (or above half of it) gets an answer right away, from code.

        Seen live: "एडवांस 50,000" on a 10,000 job was read back and saved without a word.
        """
        if cfg.mode != "vendor_call" or outcome.get("advance_asks", 0) >= 2:
            return None
        dialogue = _transcript(context)
        if not dialogue or dialogue[-1]["role"] != "vendor_or_family":
            return None
        question = next((t["text"] for t in reversed(dialogue) if t["role"] == "bandobast"), "")
        if "advance_amount" not in price_safety._labels(question) and "advance_amount" not in price_safety._labels(dialogue[-1]["text"]):
            return None
        numbers = set(_amounts(dialogue[-1]["text"]))
        if len(numbers) != 1:
            return None
        advance = numbers.pop()
        vendor = store.vendor(cfg.vendor_id)
        _, brief = store.event(event_id)
        total = quote_total_so_far(dialogue, vendor["category"], brief)
        if not total or advance <= total * rules.ADVANCE_LIMIT_SHARE:
            return None
        outcome["advance_asks"] = outcome.get("advance_asks", 0) + 1
        if advance > total:
            hi = f"सर, एडवांस टोटल से ज़्यादा नहीं हो सकता, टोटल लगभग {total} रुपये बन रहा है। एडवांस कितना लेंगे?"
            en = f"Sir, the advance can't be more than the total, which comes to about {total} rupees. How much advance would you take?"
        else:
            half = rules._nice(total * rules.ADVANCE_LIMIT_SHARE, "total", coarse=False)
            hi = f"सर, आधे से ज़्यादा एडवांस मुश्किल है, {half} रुपये तक एडवांस हो जाएगा?"
            en = f"Sir, more than half in advance is difficult. Would {half} rupees as advance work?"
        log.info(f"advance {advance} against a total of about {total}: asked again")
        if language in ("hi-IN", "en-IN"):
            return speak_fixed(hi if language == "hi-IN" else en)
        context.add_message({"role": "developer", "content": f'Say only this, in their language, with amounts as digits: "{en}" Then wait.'})
        return None

    def quote_total_so_far(dialogue: list[dict[str, str]], category: str, brief: dict[str, Any]) -> int | None:
        """Base rate (settled, or as stated) plus transport and extras stated so far; None before any rate."""
        stated = price_safety.stated(dialogue, category)
        rate = outcome.get("rate_settled") or (stated.get("price_amount") or (None, None))[0]
        if not rate:
            return None
        unit = outcome.get("rate_unit") or (stated.get("price_amount") or (None, None))[1] or rules.price_plan(category, brief)["unit"]
        base = rate * int(brief.get("guests") or 1) if unit == "per_plate" else rate
        extras = sum((stated.get(f) or (0, None))[0] for f in ("transport_charge", "extra_charges_amount"))
        return int(base + extras)

    def timing_hint() -> list[Frame] | None:
        """When the vendor asks what time or when ("cake कितने बजे देना है?"), code answers and re-asks.

        Seen live: the model ignored "कितने बजे देना है?" four times and kept asking "केक बना सकते हैं?".
        """
        if cfg.mode != "vendor_call" or language not in ("hi-IN", "en-IN") or outcome.get("timing_told", 0) >= 2:
            return None
        dialogue = _transcript(context)
        if not dialogue or dialogue[-1]["role"] != "vendor_or_family" or not ASKS_TIME.search(dialogue[-1]["text"]):
            return None
        vendor = store.vendor(cfg.vendor_id)
        _, brief = store.event(event_id)
        hi, en = catalog.timing_line(vendor["category"], brief)
        pending = next((t["text"] for t in reversed(dialogue) if t["role"] == "bandobast"), "")
        pending = pending if pending.rstrip().endswith("?") and not ASKS_TIME.search(pending) else ""
        text = f"{hi if language == 'hi-IN' else en} {pending}".strip()
        outcome["timing_told"] = outcome.get("timing_told", 0) + 1
        log.info("vendor asked about the time: told the fixed timing")
        return speak_fixed(text) if text.endswith("?") else [TTSSpeakFrame(text)]

    def function_details_hint() -> list[Frame] | None:
        """When the vendor asks how big the function is or where, code answers with the fixed details line.

        Seen live: "function कितना बड़ा है?" got "रेट कितना है?" four times. At most twice a call.
        """
        if cfg.mode != "vendor_call" or language not in ("hi-IN", "en-IN") or outcome.get("details_told", 0) >= 2:
            return None
        dialogue = _transcript(context)
        if not dialogue or dialogue[-1]["role"] != "vendor_or_family" or not ASKS_SCOPE.search(dialogue[-1]["text"]):
            return None
        vendor = store.vendor(cfg.vendor_id)
        _, brief = store.event(event_id)
        scope, scope_en = catalog.scope_line(vendor["category"], brief)
        rate_known = "price_amount" in price_safety.stated(dialogue, vendor["category"])
        if language == "hi-IN":
            text = f"{scope}।" if rate_known else f"{scope}। {'पर प्लेट रेट' if vendor['category'] == 'caterer' else 'इसका रेट'} कितना होगा?"
        else:
            text = f"{scope_en}." if rate_known else f"{scope_en}. How much would it be?"
        outcome["details_told"] = outcome.get("details_told", 0) + 1
        log.info("vendor asked about the function: told the fixed details")
        if text.endswith("?"):
            return speak_fixed(text)
        return [TTSSpeakFrame(text)]

    def bargain_now(category: str, brief: dict[str, Any], amount: int, unit: str) -> list[Frame] | None:
        """Answer a rate the moment it's named: counter about 10% lower, or settle it out loud.

        Settling ("ठीक है सर, 700 रुपये पर प्लेट फाइनल।") ends the rate talk: the model is told it's final
        and goes on to the next question, so a vendor who says "पहले plate final करिए" gets exactly that.
        """
        if category not in rules.NEGOTIABLE:
            return None
        round_no = outcome.get("bargain_round", 0) + 1
        result = rules.counter_offer(category, {"price_amount": amount, "price_unit": unit}, brief, round_no=round_no,
                                     previous_ask=outcome.get("last_ask"), previous_rate=outcome.get("last_rate"))
        if result.get("negotiate"):
            outcome["bargain_round"] = round_no
            outcome["last_ask"], outcome["last_rate"] = result["price_amount"], amount
            log.info(f"rate {amount}: {result['reason']} {result['price_amount']} (round {round_no})")
            return say_or_instruct(result["reason"], result["price_amount"], result["price_unit"])
        # a fair first quote needs no announcement; after any back-and-forth, close it out loud
        close = result.get("close", "accept")
        return settle(amount, unit, announce=round_no > 1 or close == "pass_on", kind=close)

    def settle(amount: int, unit: str, *, announce: bool, kind: str = "accept") -> list[Frame] | None:
        outcome["rate_settled"] = amount
        outcome["negotiation_done"] = True  # settled in the call; no second bargaining at save
        log.info(f"rate {amount} settled")
        context.add_message({"role": "developer", "content": f"The rate is settled at {amount} rupees "
                             f"{UNIT_WORDS[unit][1]}. Never discuss, question or bargain the rate again. "
                             "Go on with the next unanswered question."})
        if not announce:
            return None
        frames = say_or_instruct(kind, amount, unit) or []
        return frames + [LLMRunFrame()]

    def say_or_instruct(kind: str, amount: int, unit: str) -> list[Frame] | None:
        values = {"amount": amount, "unit_key": unit}
        text = fixed_text(language, BARGAIN_LINES[kind], BARGAIN_MEANING[kind], **values)
        if text:
            return speak_fixed(text) if text.endswith("?") else [TTSSpeakFrame(text)]
        context.add_message({"role": "developer", "content": fixed_line(language, BARGAIN_LINES[kind], BARGAIN_MEANING[kind], **values)})
        return None

    def high_price_hint() -> list[Frame] | None:
        """Push back the moment the vendor names a rate far above normal, not at the read-back.

        Runs on each vendor turn before the model replies. If they just stated the base rate and it's
        unusually high, the model is told the exact line to say now (the same lines and counter as at save).
        """
        if cfg.mode != "vendor_call" or cfg.reconfirm or outcome.get("rate_settled") is not None:
            return
        dialogue = _transcript(context)
        if not dialogue or dialogue[-1]["role"] != "vendor_or_family":
            return
        # Their reply can arrive as two messages ("एक मिनट..." then "5000 रुपये"): read all since our last line.
        replies, question = [], ""
        seen = outcome.get("rate_seen", 0)  # messages already handled: never re-read "3500" as a new answer
        for index in range(len(dialogue) - 1, -1, -1):
            turn = dialogue[index]
            if turn["role"] == "bandobast":
                question = turn["text"]
                break
            if index >= seen:
                replies.insert(0, turn["text"])
        outcome["rate_seen"] = len(dialogue)
        if not replies:
            return None
        said_text = " ".join(replies)
        category = store.vendor(cfg.vendor_id)["category"]
        if amount_in_readback(question) and price_safety._confirms_readback(said_text):
            outcome["readback_confirmed_at"] = user_turns()
        last_ask = outcome.get("last_ask")
        if last_ask and last_ask in _amounts(question) and (agrees(said_text) or set(_amounts(said_text)) == {last_ask}):
            # "900 में हो जाएगा" / "हाँ, हो जाएगा" to our "900 कर दीजिए": they accepted our figure.
            _, brief = store.event(event_id)
            return settle(last_ask, outcome.get("rate_unit") or rules.price_plan(category, brief)["unit"], announce=True)
        said = price_safety._assertions(said_text, question, category).get("price_amount")
        if not said and "price_amount" in price_safety._labels(question) and len(set(_amounts(said_text))) == 1:
            # "खाना पर plate rate होगा आपका कुछ, यही 2800 रुपये।": the label and the number sit in different clauses.
            said = (_amounts(said_text)[0], price_safety._unit(said_text, category) or price_safety._unit(question, category))
        if not said and (outcome.get("high_price_asks") or outcome.get("bargain_round")):
            # They are answering our "please repeat" or our counter-offer: read the reply as a rate.
            said = price_safety._assertions(said_text, RATE_QUESTION[category == "caterer"], category).get("price_amount")
            if not said:
                # "नहीं, 700 करिए, 700 में हो जाएगा।": in bargaining a no with one new number is their counter.
                numbers = {amount for _, _, amount in price_safety.amount_mentions(said_text)} - {outcome.get("last_ask")}
                if len(numbers) == 1:
                    said = (numbers.pop(), None)
        if not said:
            return
        amount, unit = said
        _, brief = store.event(event_id)
        unit = unit or outcome.get("rate_unit") or rules.price_plan(category, brief)["unit"]  # "50,000" after "please repeat"
        outcome["rate_unit"] = unit
        return bargain_now(category, brief, amount, unit)  # no "please repeat" first: bargaining starts at once
        asks = outcome.get("high_price_asks", 0)
        outcome["high_price_asks"] = asks + 1
        log.info(f"high price {amount} said: push back {asks + 1} now")
        text, say = high_price_line(asks, category, brief)
        if text:
            return speak_fixed(text)
        context.add_message({"role": "developer", "content": f"The rate they just said ({amount}) is far above normal. {say} "
                             "Then wait for their answer. Don't call any tool now."})
        return None

    reviewing: dict[str, Any] = {"task": None, "count": 0}

    def start_review() -> None:
        """Ask the stronger supervisor model to look over the call, in the background (supervisor.py)."""
        if cfg.mode != "vendor_call" or not supervisor.enabled() or reviewing["count"] >= supervisor.max_reviews():
            return
        if reviewing["task"] and not reviewing["task"].done():
            return  # one review at a time; the next vendor turn starts a fresh one
        dialogue = _transcript(context)
        if not dialogue or dialogue[-1]["role"] != "vendor_or_family":
            return
        reviewing["count"] += 1
        vendor = store.vendor(cfg.vendor_id)
        about = (f"{vendor['name']}, a {catalog.CATEGORIES[vendor['category']]['spoken']}, speaks "
                 f"{prompts.LANGUAGE_NAMES.get(language, language)}. Saved so far: "
                 f"{', '.join(sorted(outcome.get('recorded', set()))) or 'nothing'}.")
        if vendor["category"] in rules.NEGOTIABLE:
            plan = rules.price_plan(vendor["category"], store.event(event_id)[1])
            about += (f" Price plan fixed by code ({plan['unit'].replace('_', ' ')}): expect {plan['expected']}, "
                      f"maximum {plan['maximum']}; Mohan may say these amounts, and code picks the bargaining lines.")

        async def run() -> None:
            note = await supervisor.review(dialogue, about)
            if note:
                outcome.setdefault("supervisor_notes", []).append(note)
                context.add_message({"role": "developer", "content": f"Note from your supervisor, who is listening to this call: {note}"})

        reviewing["task"] = asyncio.get_running_loop().create_task(run())

    async def push_back_on_high_price(fn: FunctionCallParams, quote: dict[str, Any], brief: dict[str, Any], category: str) -> bool:
        """A rate far above normal gets a polite "sorry, didn't catch that", then "that's too high".

        Code picks the line, not the model. The third time the amount stands: it is saved, and the
        family sees it against their budget. Returns True if the save is held.
        """
        if cfg.mode != "vendor_call" or cfg.reconfirm or not quote.get("available"):
            return False
        if "price_amount" not in price_safety.rechecks(quote, brief, category):
            return False
        asks = outcome.get("high_price_asks", 0)
        if asks >= len(HIGH_PRICE_LINES):
            return False
        outcome["high_price_asks"] = asks + 1
        text, say = high_price_line(asks, category, brief)
        log.info(f"high price {quote.get('price_amount')}: push back {asks + 1}")
        await hold_save(fn, text, say)
        return True

    async def hold_save(fn: FunctionCallParams, text: str | None, say: str) -> None:
        """Hold the save while Mohan asks a code-picked question; spoken by code where it can be."""
        if text:
            await fn.result_callback({"ok": False, "saved": False, "next": f'Not saved yet. Mohan asked: "{text}" '
                                      "When they answer, call record_quote again with the numbers they give now."},
                                     properties=FunctionCallResultProperties(run_llm=False))
            for frame in speak_fixed(text):
                await fn.llm.push_frame(frame)
            return
        await fn.result_callback({"ok": False, "saved": False, "next": f"Not saved yet. {say} When they answer, "
                                  "call record_quote again with the numbers they give now."})

    async def negotiate_first(fn: FunctionCallParams, category: str, quote: dict[str, Any]) -> bool:
        """Hold a confirmed caterer or decorator quote while the rules want a counter-offer.

        The model doesn't decide when or how to negotiate: each time it tries to save, the rules
        pick the next ask (or say stop), and the save waits. Returns True if the save is held.
        """
        if category not in rules.NEGOTIABLE or not quote.get("available") or outcome.get("negotiation_done"):
            return False
        outcome["negotiation_round"] = outcome.get("negotiation_round", 0) + 1
        _, brief = store.event(event_id)
        result = rules.counter_offer(
            category,
            quote,
            brief,
            round_no=outcome["negotiation_round"],
            previous_ask=outcome.get("last_ask"),
            previous_rate=outcome.get("last_rate"),
        )
        log.info(f"negotiation round {outcome['negotiation_round']}: {quote.get('price_amount')} -> {result}")
        if not result["negotiate"]:
            outcome["negotiation_done"] = True
            return False
        outcome["last_ask"], outcome["last_rate"] = result["price_amount"], quote.get("price_amount")
        values = {"amount": result["price_amount"], "unit_key": result["price_unit"]}
        hindi, meaning = BARGAIN_LINES[result["reason"]], BARGAIN_MEANING[result["reason"]]
        await hold_save(fn, fixed_text(language, hindi, meaning, **values), fixed_line(language, hindi, meaning, **values))
        return True

    handlers = {
        "end_call": end_call,
        "save_brief": save_brief,
        "record_quote": record_quote,
        "record_checkin": record_checkin,
        "fill_form": fill_form,
        "record_update": record_update,
        'start_confirmation': start_confirmation,
        'record_confirmation': record_confirmation,
    }
    for tool in tools:
        llm.register_function(tool.name, handlers[tool.name])

    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # People pause between sentences mid-answer; too short a wait makes the bot answer twice.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.8)),
            # Smart Turn often judges short Hindi answers ("हाँ, बोलिए") unfinished and then waited its default
            # 3 s of silence: about 40% of turns in a live call lost exactly 3 s. Cap that wait.
            # Seen live: background voices ("हाँ, cash लेते हैं", a TV) set off the voice detector and cut every
            # reply off within half a second, so Mohan went silent. A turn now starts on transcribed words, and
            # cutting Mohan off mid-sentence takes MIN_INTERRUPT_WORDS words.
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=MIN_INTERRUPT_WORDS)],
                stop=[TurnAnalyzerUserTurnStopStrategy(
                turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=SMART_TURN_STOP_SECS)))]),
            # On a phone line, speech the STT returns nothing for is usually real speech, not noise.
            # Left unanswered, both sides wait in silence (seen live: 24 s of dead air, then a hang-up).
            empty_user_turn=EmptyUserTurnConfig(idle_prompt=EMPTY_TURN_PROMPT, max_consecutive_recoveries=1),
        ),
    )
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            UnheardSpeech(ask_to_repeat),
            *([ConfirmationAnswerRouter(confirmation_state, advance_confirmation)] if booking_record else []),
            BeforeReply(start_review, high_price_hint, function_details_hint, timing_hint, advance_hint),
            llm,
            question_gate,
            tts,
            transport.output(),
            auto_hangup,
            *([ConfirmationSpeechTracker(confirmation_state, user_turns)] if booking_record else []),
            aggregators.assistant(),
        ]
    )

    latency = UserBotLatencyObserver()
    latencies: list[float] = []

    @latency.event_handler("on_latency_measured")
    async def on_latency_measured(_observer, seconds: float):
        latencies.append(seconds)
        log.info(f"user->bot latency: {seconds:.2f}s")

    @latency.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(_observer, breakdown):
        for line in breakdown.turn_contribution_lines():
            log.debug(f"latency  {line}")

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=sample_rate_in,
            audio_out_sample_rate=sample_rate_out,
            enable_metrics=True,
        ),
        observers=[latency],
        # If the LLM, STT or TTS fails for good (bad key, quota), hang up instead of leaving dead air.
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )

    @task.event_handler("on_pipeline_error")
    async def on_pipeline_error(_task, frame):
        # Many errors are transient (a service reconnects); it only explains the end if nothing else does.
        outcome["last_error"] = f"error: {getattr(frame, 'error', frame)}"[:300]

    opening = greeting.opening_line(store.vendor(cfg.vendor_id), language) if cfg.mode in GREETED_MODES and cfg.vendor_id else None

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        # Without this the model can decide the call is already over and hang up silently.
        # It's a user-role stage direction because some chat templates (Qwen) need a user turn.
        context.add_message({"role": "user", "content": CALL_CONNECTED})
        if not opening:
            await task.queue_frames([LLMRunFrame()])
            return
        # The first line is fixed, so it plays at once from saved audio instead of waiting for the
        # LLM and the TTS. Then the model waits for their answer like after any question.
        context.add_message({"role": "assistant", "content": opening})
        question_gate.mark_greeted()
        await task.queue_frames(greeting_frames(opening, language, sample_rate_out))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await task.cancel()

    started_at, t0 = store.now(), time.time()
    log.info(f"Conversation start: mode={cfg.mode} channel={cfg.channel} vendor={cfg.vendor_id}")
    try:
        await WorkerRunner(handle_sigint=False).run(task)
    finally:
        store.save_call(
            {
                "id": cfg.call_id,
                "event_id": outcome.get("event_id", event_id),
                "vendor_id": cfg.vendor_id,
                "mode": cfg.mode,
                "channel": cfg.channel,
                "started_at": started_at,
                "duration_secs": round(time.time() - t0, 1),
                "end_reason": outcome.get("end_reason") or outcome.get("last_error"),
                "latencies": [round(s, 2) for s in latencies],
                "transcript": _transcript(context),
            }
        )
        log.info("Call saved")
        if booking_record:
            confirmations.finish(cfg.confirmation_id, 'failed', outcome.get('last_error') or outcome.get('end_reason') or 'Call ended without confirmation', call_id=cfg.call_id)
        if event_id:
            import enquiries
            enquiries.queue_pending_confirmations(event_id)


class AutoHangup(FrameProcessor):
    """Hang up from our side once the call's job is done and the goodbye has been spoken.

    The model is told to call end_call, but sometimes it just says goodbye and waits, leaving the
    vendor holding a silent line. Once armed (the quote or update is saved), the first goodbye we
    finish speaking starts a short timer; if the other side doesn't speak and we didn't ask anything,
    the call ends. If they do speak, the bot answers and the timer starts again after that reply.
    """

    GRACE_SECS = 4.0

    def __init__(self, *, user_turns: Callable[[], int], awaiting_answer: Callable[[], bool], outcome: dict[str, Any],
                 grace_secs: float | None = None):
        super().__init__()
        self.user_turns = user_turns
        self.awaiting_answer = awaiting_answer
        self.outcome = outcome
        self.grace = self.GRACE_SECS if grace_secs is None else grace_secs
        self.armed = False
        self.spoke_since_armed = False
        self._timer: asyncio.Task | None = None

    def arm(self) -> None:
        self.armed = True
        self.spoke_since_armed = False

    def _cancel(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _hang_up_later(self, turns: int) -> None:
        await asyncio.sleep(self.grace)
        if self.user_turns() != turns or self.awaiting_answer():
            return
        self.outcome.setdefault("end_reason", "Hung up automatically after the goodbye")
        logger.info("auto hang-up: job done and goodbye spoken")
        await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            self._cancel()
        elif isinstance(frame, BotStartedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM:
            self._cancel()
            if self.armed:
                self.spoke_since_armed = True
        elif (isinstance(frame, BotStoppedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM
              and self.armed and self.spoke_since_armed and not self.awaiting_answer()):
            self._cancel()
            self._timer = asyncio.create_task(self._hang_up_later(self.user_turns()))
        await self.push_frame(frame, direction)

    async def cleanup(self):
        self._cancel()
        await super().cleanup()


class ConfirmationSpeechTracker(FrameProcessor):
    """A yes only counts after the exact code-approved question has reached the audio output."""

    def __init__(self, state: dict[str, Any], user_turns: Callable[[], int]):
        super().__init__()
        self.state = state
        self.user_turns = user_turns

    def mark_delivery(self, context_id: str | None) -> bool:
        if context_id and context_id == self.state.get('tts_context'):
            self.state['ready_turn'] = self.user_turns()
            return True
        return False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStoppedFrame) and direction == FrameDirection.DOWNSTREAM:
            self.mark_delivery(frame.context_id)
        await self.push_frame(frame, direction)


class UnheardSpeech(FrameProcessor):
    """"Sorry, I couldn't hear that" when they clearly spoke but no words came through.

    Turns start on transcribed words (so background noise can't cut Mohan off), which means speech the
    STT returns nothing for never starts a turn, and both sides would wait in silence. This watches the
    voice detector instead: at least MIN_SPEECH_SECS of speech, then GRACE_SECS with no words, no turn
    and Mohan not talking, and code asks them to repeat. Once, until they are heard again.
    """

    MIN_SPEECH_SECS = 1.0
    GRACE_SECS = 2.5

    def __init__(self, ask_again: Callable[[], list[Frame]], *, grace_secs: float | None = None):
        super().__init__()
        self.ask_again = ask_again
        self.grace = self.GRACE_SECS if grace_secs is None else grace_secs
        self.armed = True
        self.bot_speaking = False
        self.speech_started: float | None = None
        self.heard = False
        self._timer: asyncio.Task | None = None

    def _cancel(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _ask_later(self) -> None:
        await asyncio.sleep(self.grace)
        if self.heard or self.bot_speaking or not self.armed:
            return
        self.armed = False
        logger.info("speech with no words: asking them to repeat")
        for frame in self.ask_again():
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self._cancel()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
        elif isinstance(frame, (UserStartedSpeakingFrame, TranscriptionFrame, InterimTranscriptionFrame)):
            self.heard = True  # words came through: the normal turn handles it
            self.armed = True
            self._cancel()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.speech_started, self.heard = time.monotonic(), False
            self._cancel()
        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self.speech_started is not None:
            spoke = time.monotonic() - self.speech_started - (frame.stop_secs or 0)
            self.speech_started = None
            if spoke >= self.MIN_SPEECH_SECS and self.armed and not self.heard and not self.bot_speaking:
                self._cancel()
                self._timer = asyncio.get_running_loop().create_task(self._ask_later())
        await self.push_frame(frame, direction)

    async def cleanup(self):
        self._cancel()
        await super().cleanup()


class BeforeReply(FrameProcessor):
    """Runs checks on the context after each of their turns, just before the model replies."""

    def __init__(self, *hooks: Callable[[], list[Frame] | None]):
        super().__init__()
        self.hooks = hooks

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            for hook in self.hooks:
                try:
                    instead = hook()
                except Exception as error:  # a hint must never break the call
                    logger.warning(f"BeforeReply hook failed ({type(error).__name__}: {error})")
                    continue
                if instead:
                    # Code answers this turn itself (a fixed line): no LLM run.
                    for replacement in instead:
                        await self.push_frame(replacement, direction)
                    return
        await self.push_frame(frame, direction)


class ConfirmationAnswerRouter(FrameProcessor):
    """Clear completed yes-answers advance in code; only changes/unclear speech need another LLM call."""

    def __init__(self, state: dict[str, Any], advance: Callable[..., Any]):
        super().__init__()
        self.state = state
        self.advance = advance

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM and self.state['controlled']:
            try:
                await self.advance(run_llm_when_complete=True)
                return
            except (ValueError, LookupError):
                pass  # The model extracts a change/refusal; it cannot bypass the same confirmation guards.
        await self.push_frame(frame, direction)


class QuestionGate:
    """Decides what of one streamed reply gets spoken: one short question, then the bot listens.

    Models tend to run through the whole script in one reply, or recap every detail before each
    question. Text before the first question is held back; only the question and at most one
    short lead-in sentence are released, and everything after the question is dropped. A reply
    with no question (a goodbye) is released whole when it ends.
    """

    MAX_LEAD_IN_WORDS = 10
    _SENTENCE_END = re.compile(r"(?<=[।.!])\s+")
    _LATIN_ONLY = re.compile(r"^[\sA-Za-z,.;:'\"!-]+$")

    def __init__(self, *, may_ask: bool = True, greeting: bool = False, english_ok: bool = True):
        # may_ask=False: our last question is still unanswered, so this whole reply is dropped.
        # greeting=True: the opening reply, spoken untrimmed.
        # english_ok=False: drop all-English sentences, which in a Hindi call are leaked model notes.
        self._may_ask = may_ask
        self._greeting = greeting
        self._english_ok = english_ok
        self._asked = False
        self._held = ""

    @property
    def asked(self) -> bool:
        """Whether this reply spoke a question."""
        return self._asked and self._may_ask

    def feed(self, chunk: str) -> str:
        """Text to speak now for this streamed chunk ('' to hold or drop it)."""
        if self._asked:
            return ""
        cut = min((i for i in (chunk.find("?"), chunk.find("？")) if i >= 0), default=-1)
        if cut < 0:
            self._held += chunk
            return ""
        self._asked = True
        text, self._held = self._held + chunk[: cut + 1], ""
        if not self._may_ask:
            return ""
        sentences = self._sentences(text)
        if not sentences:
            return ""
        if self._greeting:
            return " ".join(sentences)
        question, lead_in = sentences[-1], sentences[-2] if len(sentences) > 1 else ""
        if lead_in and len(lead_in.split()) <= self.MAX_LEAD_IN_WORDS:
            return f"{lead_in} {question}"
        return question

    def end(self) -> str:
        """Held text to speak when a reply ends without a question."""
        text, self._held = ("" if self._asked or not self._may_ask else self._held), ""
        return " ".join(self._sentences(text))

    def _sentences(self, text: str) -> list[str]:
        sentences = [s for s in self._SENTENCE_END.split(text.strip()) if s.strip()]
        if not self._english_ok:
            sentences = [s for s in sentences if not self._LATIN_ONLY.match(s)]
        return sentences


class OneQuestionPerTurn(FrameProcessor):
    """Applies QuestionGate to each LLM reply on its way to the voice.

    It also stops back-to-back replies: after a tool result the model often replies again
    before the other person has answered, so until they speak, nothing new is said.
    Dropped text never reaches the voice, and so never reaches the conversation history either.

    user_turns returns how many times the other person has spoken so far.
    """

    def __init__(self, user_turns: Callable[[], int], *, english_ok: bool = True, tool_only: Callable[[], bool] | None = None):
        super().__init__()
        self._user_turns = user_turns
        self._english_ok = english_ok
        self._replies = 0
        self._asked_at: int | None = None
        self._gate = QuestionGate()
        self._tool_only = tool_only or (lambda: False)

    def mark_question(self) -> None:
        self._asked_at = self._user_turns()

    def mark_greeted(self) -> None:
        """Code already spoke the opening question; the model's first reply is not the greeting."""
        self._replies += 1
        self.mark_question()

    @property
    def awaiting_answer(self) -> bool:
        """Our last spoken question hasn't been answered yet."""
        return self._asked_at == self._user_turns()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._gate = QuestionGate(
                may_ask=not self.awaiting_answer, greeting=self._replies == 0, english_ok=self._english_ok
            )
            self._replies += 1
        elif isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._tool_only():
                return
            text = self._gate.feed(frame.text)
            if self._gate.asked:
                self._asked_at = self._user_turns()
            if not text:
                return
            frame.text = text
        elif isinstance(frame, LLMFullResponseEndFrame) and not self._tool_only() and (text := self._gate.end()):
            await self.push_frame(LLMTextFrame(text), direction)
        await self.push_frame(frame, direction)


# Whole words only: "na" must not match Jayanagar or Indiranagar.
_PLACEHOLDER_RE = re.compile(r"\b(?:please|unknown|pending|n/?a|tbd)\b|कृपया|बताइए|\?", re.I)


def _valid_time(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is not None


def brief_problems(args: dict[str, Any]) -> list[str]:
    """What's missing or made-up in a brief the model wants to save."""
    problems = []
    function = catalog.FUNCTIONS.get(args.get("function_type", ""))
    if not function:
        problems.append("the function (we arrange: " + ", ".join(f["name"] for f in catalog.FUNCTIONS.values()) + ")")
    elif args.get("function_variant") not in function["variants"]:
        problems.append("which " + function["name"].lower() + ": " + ", ".join(function["variants"].values()))
    for name in ("family_name", "city", "area"):
        value = str(args.get(name) or "").strip()
        if not value or _PLACEHOLDER_RE.search(value):
            problems.append(name.replace("_", " "))
    try:
        if datetime.strptime(str(args.get("date")), "%Y-%m-%d").date() < datetime.now().date():
            problems.append("the date (it is in the past; use the coming date, YYYY-MM-DD)")
    except ValueError:
        problems.append("the date")
    if not _valid_time(args.get("start_time")):
        problems.append("the muhurat start time")
    if not isinstance(args.get("guests"), int) or args["guests"] <= 0:
        problems.append("the number of guests")
    needs = args.get("needs") or []
    if not needs:
        problems.append("which services they need")
    if "cake" in needs and not str(args.get("details_cake") or "").strip():
        problems.append("the cake they want: " + catalog.CATEGORIES["cake"]["details"])
    return problems


def fill_stated_charges(args: dict[str, Any], dialogue: list[dict[str, str]], category: str) -> None:
    """Fill a charge the model left empty with what the vendor clearly said for it.

    Seen live: the vendor said "कोई extra staff नहीं चाहिए", the model saved extras as null, the save was
    refused, and the call ended without a quote. Only empty fields are filled, never the model's values,
    and only from the vendor's own words; the price audit still checks the result.
    """
    if not args.get("available"):
        return
    for field, (amount, unit) in price_safety.stated(dialogue, category).items():
        if args.get(field) is None:
            args[field] = amount
            if field == "price_amount" and unit and not args.get("price_unit"):
                args["price_unit"] = unit


def quote_problems(args: dict[str, Any], category: str) -> list[str]:
    """Whether they're free, and if so every item for their category, must be answered."""
    if args.get("available") is None:
        return ["whether they are available on the date"]
    if not args.get("available"):
        return []
    improper = ["an improper payment term (they asked for alcohol or similar): don't record it; say 'सर, प्लीज़ प्रोफेशनली बात कीजिए' "
                "and ask for a normal rupee quote"] if any(isinstance(v, str) and _IMPROPER_RE.search(v) for v in args.values()) else []
    return [label for field, label in catalog.QUOTE_REQUIRED[category].items() if args.get(field) in (None, "")] + price_safety.value_problems(args, category) + improper


# Non-money "payments" a vendor might slip into a quote. Never saved as terms the family could approve.
_IMPROPER_RE = re.compile(
    r"\b(?:alcohol|liquor|whisk(?:e)?y|beer|rum|vodka|wine|daaru|daru|sharab|cigarettes?|ganja)\b|"
    r"दारू|शराब|बियर|बीयर|व्हिस्की|विस्की|सिगरेट|ಮದ್ಯ|ಎಣ್ಣೆ ಬಾಟಲ್", re.I)


def checked_quote(args: dict[str, Any], dialogue: list[dict[str, str]], brief: dict[str, Any], category: str,
                  call_id: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """The model may supply quote fields, never audit metadata or a verified opening price."""
    quote = normalize_quote({k: v for k, v in args.items() if k in RECORD_QUOTE.properties})
    quote["amount_checks"] = price_safety.audit(quote, dialogue, category, call_id=call_id, previous=previous)
    quote["unverified"] = [field for field in price_safety.FIELDS if not quote["amount_checks"]["fields"][field]["statements"]
                           and not price_safety.grounded(quote, quote["amount_checks"], field)]
    return quote


_TOOL_CALL_RE = re.compile(r"\b(?:" + "|".join(TOOL_NAMES) + r")\s*\(.*?\)", re.S)
_BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")


_REPEATS_RE = re.compile(r"(\S+)(?:\s+\1){2,}")  # "धन्यवाद धन्यवाद धन्यवाद" -> "धन्यवाद"


async def drop_unspeakable(text: str, _text_type: Any) -> str:
    """Never voice tool calls, bracketed asides like '[Awaiting response]', or stuck repeats."""
    text = _BRACKETS_RE.sub(" ", _TOOL_CALL_RE.sub(" ", text))
    return re.sub(r"\s+", " ", _REPEATS_RE.sub(r"\1", text)).strip()


def normalize_quote(quote: dict[str, Any]) -> dict[str, Any]:
    """The model sometimes files transport under extra charges; move it so the rules see it."""
    if quote.get("extras_at_actuals") is True and quote.get("extra_charges_amount") is None:
        quote["extra_charges_amount"] = 0  # nothing fixed now; the rest is billed later at actual cost
    notes = (quote.get("extra_charges_notes") or "").lower()
    if quote.get("transport_charge") is None and quote.get("extra_charges_amount") and (
        "transport" in notes or "ट्रांसपोर्ट" in notes or "travel" in notes
    ):
        quote["transport_charge"] = quote["extra_charges_amount"]
        quote["extra_charges_amount"] = 0
    return quote


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _is_empty_turn(message: Any) -> bool:
    """The note pipecat adds when they spoke but the STT returned no words."""
    return isinstance(message, dict) and message.get("role") == "developer" and message.get("content") in EMPTY_TURN_PROMPTS


def _is_spoken_turn(message: Any) -> bool:
    """A real turn by either side, not a tool message or our stage direction."""
    return (
        isinstance(message, dict)
        and message.get("role") in ("user", "assistant")
        and bool(_text(message))
        and _text(message) != CALL_CONNECTED
    )


def _transcript(context: LLMContext) -> list[dict[str, str]]:
    return [
        {"role": "vendor_or_family" if m["role"] == "user" else "bandobast", "text": _text(m)}
        for m in context.get_messages()
        if _is_spoken_turn(m)
    ]
