"""Phone channel: run a conversation over a Twilio Media Stream (8 kHz)."""

import os

from fastapi import WebSocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from conversation import SessionConfig, run_conversation

TWILIO_SAMPLE_RATE = 8000


async def run_phone_call(websocket: WebSocket, call_data) -> None:
    """Run the conversation for one connected Twilio call until it ends."""
    cfg = SessionConfig.from_dict(call_data.body or {}, channel="phone", call_id=call_data.call_id)
    serializer = TwilioFrameSerializer(
        stream_sid=call_data.stream_id,
        call_sid=call_data.call_id,
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
    await run_conversation(
        transport, cfg, sample_rate_in=TWILIO_SAMPLE_RATE, sample_rate_out=TWILIO_SAMPLE_RATE
    )
