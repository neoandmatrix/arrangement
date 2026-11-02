"""Voice pipeline for one outbound vendor call over a Twilio Media Stream.

Twilio audio (8 kHz) -> Gnani Prisma STT -> Groq LLM -> Gnani Timbre TTS -> Twilio.
"""

import json
import os
import time
from pathlib import Path

from fastapi import WebSocket
from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner
from pipecat_gnani import GnaniSTTService, GnaniTTSService

from prompts import vendor_call_prompt

TWILIO_SAMPLE_RATE = 8000
CALLS_DIR = Path(__file__).parent / "calls"

# Timbre v3.0 voices speak all 10 languages, so one voice works for every vendor.
TTS_MODEL = "timbre-v3.0"
TTS_VOICE = "Shivani"

END_CALL_TOOL = FunctionSchema(
    name="end_call",
    description="Hang up the phone after saying goodbye.",
    properties={
        "reason": {
            "type": "string",
            "description": "Why the call is ending, e.g. 'questions answered' or 'vendor busy, call back at 6 pm'.",
        }
    },
    required=["reason"],
)


async def run_vendor_call(websocket: WebSocket, call_data) -> None:
    """Run the conversation for one connected Twilio call until it ends."""
    params = call_data.body or {}
    language = params.get("language", "hi-IN")
    vendor_name = params.get("vendor_name", "the caterer")
    family_name = params.get("family_name", "Sharma")
    call_sid = call_data.call_id

    serializer = TwilioFrameSerializer(
        stream_sid=call_data.stream_id,
        call_sid=call_sid,
        account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        auth_token=os.environ["TWILIO_AUTH_TOKEN"],
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt = GnaniSTTService(
        api_key=os.environ["GNANI_API_KEY"],
        sample_rate=TWILIO_SAMPLE_RATE,
        settings=GnaniSTTService.Settings(
            model="prisma-v3.0",
            language=Language(language),
            format="transcribe",  # numbers come back as digits, easier to extract quotes
        ),
    )
    tts = GnaniTTSService(
        api_key=os.environ["GNANI_API_KEY"],
        model=TTS_MODEL,
        voice_id=TTS_VOICE,
        sample_rate=TWILIO_SAMPLE_RATE,
        settings=GnaniTTSService.Settings(language=Language(language)),
    )
    llm_settings = GroqLLMService.Settings(model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"))
    # Reasoning models think before replying, which adds delay on a call; keep it low.
    # Non-reasoning models (e.g. Llama) reject this setting, so leave it empty for them.
    if effort := os.environ.get("GROQ_REASONING_EFFORT", "low"):
        llm_settings.reasoning_effort = effort
    llm = GroqLLMService(api_key=os.environ["GROQ_API_KEY"], settings=llm_settings)

    end_reason: dict[str, str] = {}

    async def end_call(fn: FunctionCallParams) -> None:
        # Hard rule: never hang up before the vendor has said anything.
        if not any(m.get("role") == "user" for m in context.get_messages() if isinstance(m, dict)):
            logger.warning(f"[{call_sid}] end_call refused: vendor hasn't spoken yet")
            await fn.result_callback(
                {"ok": False, "error": "The call has just started. Greet the vendor first; do not hang up."}
            )
            return
        end_reason["reason"] = fn.arguments.get("reason", "")
        logger.info(f"[{call_sid}] end_call: {end_reason['reason']}")
        await fn.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
        # The serializer hangs up the Twilio call when the task ends.
        await fn.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    llm.register_function("end_call", end_call)

    context = LLMContext(
        messages=[
            {
                "role": "system",
                "content": vendor_call_prompt(
                    language=language, vendor_name=vendor_name, family_name=family_name
                ),
            }
        ],
        tools=ToolsSchema(standard_tools=[END_CALL_TOOL]),
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.5)),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
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
        logger.info(f"[{call_sid}] user->bot latency: {seconds:.2f}s")

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            enable_metrics=True,
        ),
        observers=[latency],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        # The vendor has picked up: tell the LLM so it greets instead of assuming the call is over.
        context.add_message(
            {
                "role": "system",
                "content": "The vendor has just picked up the phone. Start the call with your greeting.",
            }
        )
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await task.cancel()

    started = time.time()
    try:
        await WorkerRunner(handle_sigint=False).run(task)
    finally:
        _save_call_log(
            call_sid,
            {
                "call_sid": call_sid,
                "params": params,
                "duration_secs": round(time.time() - started, 1),
                "end_reason": end_reason.get("reason"),
                "latencies_secs": [round(s, 2) for s in latencies],
                "messages": context.get_messages(),
            },
        )


def _save_call_log(call_sid: str, record: dict) -> None:
    CALLS_DIR.mkdir(exist_ok=True)
    path = CALLS_DIR / f"{call_sid}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    logger.info(f"Saved call log: {path}")
