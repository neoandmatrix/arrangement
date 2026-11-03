"""One voice conversation over any transport (Twilio phone or browser WebRTC).

audio in -> Gnani Prisma STT -> Groq LLM (+ tools) -> Gnani Timbre TTS -> audio out
"""

import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndTaskFrame,
    Frame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
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
from pipecat.transports.base_transport import BaseTransport
from pipecat.utils.text.transforms import strip_markdown
from pipecat.workers.runner import WorkerRunner
from pipecat_gnani import INDIC_AUTO_LANGUAGE_CODE, GnaniSTTService, GnaniTTSService

import catalog
import prompts
import rules
import store
import workflow

# Timbre v3.0 voices speak all 10 languages, so one voice works for every vendor.
TTS_MODEL = "timbre-v3.0"
TTS_VOICE = "Shivani"

MODES = ("intake", "vendor_call", "checkin")

CALL_CONNECTED = "[The call has connected and the other person has picked up. Say your greeting now.]"

TOOL_NAMES = ("end_call", "save_brief", "record_quote", "record_checkin")


@dataclass
class SessionConfig:
    mode: str
    channel: str  # "phone" | "browser"
    vendor_id: str | None = None
    checkin_id: int | None = None
    renegotiate: bool = False  # vendor_call: calling back about their quote, at the family's request
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
            renegotiate=str(data.get("renegotiate", "")).lower() in ("1", "true"),
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
    },
    required=["reason", "callback_requested"],
)

# What each conversation must record before it may hang up.
REQUIRED_RESULT = {"intake": "save_brief", "vendor_call": "record_quote", "checkin": "record_checkin"}

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
        "gst_included": {"type": ["boolean", "null"]},
        "advance_amount": {"type": ["integer", "null"], "description": "Rupees; 0 if no advance"},
        "includes_notes": {"type": ["string", "null"], "description": "What the price covers"},
        "notes": {"type": ["string", "null"], "description": "Anything else important, e.g. language, timing"},
    },
    required=["available", "price_amount", "price_unit"],
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

    event_id, brief = workflow.require_event()
    if cfg.mode == "vendor_call":
        vendor = store.vendor(cfg.vendor_id)
        quote = store.latest_quotes(event_id).get(vendor["id"])
        if cfg.renegotiate:
            if not quote:
                raise ValueError(f"No quote from {vendor['name']} to renegotiate")
            prompt = prompts.renegotiation_prompt(vendor=vendor, brief=brief, quote=quote)
        else:
            prompt = prompts.vendor_prompt(vendor=vendor, brief=brief)
        return prompt, [RECORD_QUOTE, END_CALL], vendor["language"], event_id

    checkin = store.checkin(cfg.checkin_id)
    cfg.vendor_id = checkin["vendor_id"]
    vendor = store.vendor(checkin["vendor_id"])
    quote = store.latest_quotes(event_id).get(vendor["id"])
    prompt = prompts.checkin_prompt(vendor=vendor, brief=brief, label=checkin["label"], quote=quote)
    return prompt, [RECORD_CHECKIN, END_CALL], vendor["language"], event_id


async def run_conversation(
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
    outcome: dict[str, Any] = {}
    previous_quote = store.latest_quotes(event_id).get(cfg.vendor_id) if cfg.renegotiate else None
    if previous_quote:
        # Savings are measured from their original opening price, not from this call's.
        outcome["first_price"] = previous_quote.get("initial_price_amount") or previous_quote.get("price_amount")

    def user_turns() -> int:
        return sum(_is_spoken_turn(m) and m["role"] == "user" for m in context.get_messages())

    def other_side_has_spoken() -> bool:
        return user_turns() > 0

    question_gate = OneQuestionPerTurn(user_turns=user_turns, english_ok=language == "en-IN")

    async def answer_pending(fn: FunctionCallParams) -> bool:
        """Hard rule: no saving or hanging up while our question is unanswered (e.g. the read-back)."""
        if not question_gate.awaiting_answer:
            return False
        log.warning(f"{fn.function_name} refused: waiting for their answer")
        await fn.result_callback(
            {"ok": False, "error": "You just asked them a question. Wait for their answer first."},
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
        if required not in outcome.get("recorded", set()) and not fn.arguments.get("callback_requested"):
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
        log.info(f"end_call: {outcome['end_reason']}")
        await fn.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
        await fn.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    async def reject(fn: FunctionCallParams, problems: list[str]) -> None:
        log.warning(f"{fn.function_name} rejected: {problems}")
        await fn.result_callback(
            {"ok": False, "error": "Not saved. Still missing or invalid: " + "; ".join(problems) + ". Ask about these, one at a time."}
        )

    def mark_recorded(name: str) -> None:
        outcome.setdefault("recorded", set()).add(name)

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

    async def record_quote(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        category = store.vendor(cfg.vendor_id)["category"]
        if problems := quote_problems(fn.arguments, category):
            await reject(fn, problems)
            return
        quote = normalize_quote(dict(fn.arguments))
        vendor_said = " ".join(_text(m) for m in context.get_messages() if _is_spoken_turn(m) and m["role"] == "user")
        if previous_quote:
            # On a callback, the amounts they quoted (and we verified) last time count as said.
            vendor_said += " " + " ".join(str(previous_quote.get(f) or "") for f in rules.AMOUNT_FIELDS)
        quote["unverified"] = rules.unverified_amounts(quote, vendor_said)
        if quote["unverified"]:
            log.warning(f"record_quote: amounts not heard from the vendor: {quote['unverified']}")
            # Send the model back to ask, twice at most; after that save with the flag for a human.
            outcome["quote_rejections"] = outcome.get("quote_rejections", 0) + 1
            if outcome["quote_rejections"] <= 2:
                names = ", ".join(f.replace("_amount", "").replace("_", " ") for f in quote["unverified"])
                await reject(fn, [f"{names}: the vendor never said this amount, ask them directly"])
                return
        # The first price we were asked to record is their opening price, whatever the model says.
        quote["initial_price_amount"] = outcome.setdefault("first_price", quote.get("price_amount"))
        if await negotiate_first(fn, category, quote):
            return
        store.save_quote(event_id, cfg.vendor_id, cfg.call_id, quote)
        mark_recorded("record_quote")
        log.info(f"record_quote {cfg.vendor_id}: {quote}")
        await fn.result_callback({"ok": True})

    async def record_checkin(fn: FunctionCallParams) -> None:
        if await answer_pending(fn):
            return
        args = fn.arguments
        if args.get("status") == "delayed" and not _valid_time(args.get("eta")):
            await reject(fn, ["the arrival time (ETA) as HH:MM"])
            return
        risk = workflow.record_checkin(cfg.checkin_id, args["status"], args.get("eta"), args.get("notes"))
        mark_recorded("record_checkin")
        log.info(f"record_checkin {cfg.checkin_id}: {args} -> risk {risk}")
        # The vendor isn't told about a switch to the backup; that's for the family.
        await fn.result_callback({"ok": True})

    async def negotiate_first(fn: FunctionCallParams, category: str, quote: dict[str, Any]) -> bool:
        """Hold a confirmed caterer or decorator quote while the rules want a counter-offer.

        The model doesn't decide when or how to negotiate: each time it tries to save, the rules
        pick the next ask (or say stop), and the save waits. Returns True if the save is held.
        """
        if category not in rules.NEGOTIABLE or not quote.get("available") or outcome.get("negotiation_done"):
            return False
        outcome["negotiation_round"] = outcome.get("negotiation_round", 0) + 1
        _, brief = workflow.require_event()
        result = rules.counter_offer(
            category,
            quote,
            brief,
            round_no=outcome["negotiation_round"],
            competing_total=rules.best_competing_total(workflow.competitor_quotes(cfg.vendor_id), brief),
            previous_ask_total=outcome.get("last_ask_total"),
        )
        log.info(f"negotiation round {outcome['negotiation_round']}: {quote.get('price_amount')} -> {result}")
        if not result["negotiate"]:
            outcome["negotiation_done"] = True
            return False
        outcome["last_ask_total"] = result.get("asked_total", outcome.get("last_ask_total"))
        if result["ask"] == "waive_transport":
            ask = "if they could include transport at no extra charge"
        else:
            unit = "per plate" if result["price_unit"] == "per_plate" else "in total"
            ask = f"if they could do {result['price_amount']} rupees {unit}"
        await fn.result_callback(
            {
                "ok": False,
                "saved": False,
                "next": f"Before saving, negotiate: politely ask {ask}. Reason you may give: {result['why']} "
                "Ask only this, warmly. When they answer, call record_quote again with the numbers they agree to now.",
            }
        )
        return True

    handlers = {
        "end_call": end_call,
        "save_brief": save_brief,
        "record_quote": record_quote,
        "record_checkin": record_checkin,
    }
    for tool in tools:
        llm.register_function(tool.name, handlers[tool.name])

    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # People pause between sentences mid-answer; too short a wait makes the bot answer twice.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.8)),
        ),
    )
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            question_gate,
            tts,
            transport.output(),
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

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        # Without this the model can decide the call is already over and hang up silently.
        # It's a user-role stage direction because some chat templates (Qwen) need a user turn.
        context.add_message({"role": "user", "content": CALL_CONNECTED})
        await task.queue_frames([LLMRunFrame()])

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

    def __init__(self, user_turns: Callable[[], int], *, english_ok: bool = True):
        super().__init__()
        self._user_turns = user_turns
        self._english_ok = english_ok
        self._replies = 0
        self._asked_at: int | None = None
        self._gate = QuestionGate()

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
            text = self._gate.feed(frame.text)
            if self._gate.asked:
                self._asked_at = self._user_turns()
            if not text:
                return
            frame.text = text
        elif isinstance(frame, LLMFullResponseEndFrame) and (text := self._gate.end()):
            await self.push_frame(LLMTextFrame(text), direction)
        await self.push_frame(frame, direction)


# Whole words only: "na" must not match Jayanagar or Indiranagar.
_PLACEHOLDER_RE = re.compile(r"\b(?:please|unknown|pending|n/?a|tbd)\b|कृपया|बताइए|\?", re.I)


def _valid_time(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-2]\d:[0-5]\d", value) is not None


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


def quote_problems(args: dict[str, Any], category: str) -> list[str]:
    """Whether they're free, and if so every item for their category, must be answered."""
    if args.get("available") is None:
        return ["whether they are available on the date"]
    if not args.get("available"):
        return []
    return [label for field, label in catalog.QUOTE_REQUIRED[category].items() if args.get(field) in (None, "")]


_TOOL_CALL_RE = re.compile(r"\b(?:" + "|".join(TOOL_NAMES) + r")\s*\(.*?\)", re.S)
_BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")


_REPEATS_RE = re.compile(r"(\S+)(?:\s+\1){2,}")  # "धन्यवाद धन्यवाद धन्यवाद" -> "धन्यवाद"


async def drop_unspeakable(text: str, _text_type: Any) -> str:
    """Never voice tool calls, bracketed asides like '[Awaiting response]', or stuck repeats."""
    text = _BRACKETS_RE.sub(" ", _TOOL_CALL_RE.sub(" ", text))
    return re.sub(r"\s+", " ", _REPEATS_RE.sub(r"\1", text)).strip()


def normalize_quote(quote: dict[str, Any]) -> dict[str, Any]:
    """The model sometimes files transport under extra charges; move it so the rules see it."""
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
