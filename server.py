"""FastAPI server: starts outbound Twilio calls and hosts the media-stream WebSocket.

Run:  uv run uvicorn server:app --port 8765
"""

import os
import secrets
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket
from pydantic import BaseModel, Field
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from bot import run_vendor_call
from prompts import LANGUAGE_NAMES

load_dotenv(override=True)

app = FastAPI(title="Bandobast")


class CallRequest(BaseModel):
    to: str = Field(description="Vendor phone number in E.164, e.g. +919876543210")
    language: str = "hi-IN"
    vendor_name: str = "Annapurna Caterers"
    family_name: str = "Sharma"
    say_test: bool = False


def public_host() -> str:
    """PUBLIC_HOST as a bare hostname, tolerating a pasted https:// prefix or trailing slash."""
    host = os.environ["PUBLIC_HOST"].strip()
    for scheme in ("https://", "http://", "wss://"):
        host = host.removeprefix(scheme)
    return host.rstrip("/")


def start_call(req: CallRequest) -> str:
    """Ask Twilio to dial the vendor; the answered call streams to our /ws. Returns the call SID."""
    if req.language not in LANGUAGE_NAMES:
        raise ValueError(f"Unsupported language {req.language!r}; use one of {sorted(LANGUAGE_NAMES)}")
    host = public_host()
    # Twilio fetches the call instructions from /twiml when the vendor answers.
    query = urlencode(
        {
            "language": req.language,
            "vendor_name": req.vendor_name,
            "family_name": req.family_name,
            "say_test": int(req.say_test),
        }
    )

    extra = {}
    # Trial accounts reject recording and status callbacks, so they're opt-in.
    if os.environ.get("TWILIO_FULL_ACCOUNT") == "1":
        extra = {
            "record": True,
            "status_callback": f"https://{host}/twilio/status",
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
        }

    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    call = client.calls.create(
        to=req.to,
        from_=os.environ["TWILIO_FROM_NUMBER"],
        url=f"https://{host}/twiml?{query}",
        **extra,
    )
    logger.info(f"Calling {req.vendor_name} ({req.language}): {call.sid}")
    return call.sid


@app.post("/calls")
async def create_call(req: CallRequest, authorization: str = Header(default="")):
    # The tunnel makes this endpoint public, and every call costs money: require a token.
    expected = f"Bearer {os.environ.get('CONTROL_TOKEN', '')}"
    if not os.environ.get("CONTROL_TOKEN") or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Missing or wrong CONTROL_TOKEN")
    try:
        return {"call_sid": start_call(req)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(
    language: str = "hi-IN", vendor_name: str = "", family_name: str = "", say_test: int = 0
):
    """Call instructions Twilio fetches on answer: stream the call audio to /ws.

    With say_test=1, Twilio first speaks a line and pauses, to check that our
    instructions run at all before the stream starts.
    """
    if language not in LANGUAGE_NAMES:
        language = "hi-IN"
    response = VoiceResponse()
    if say_test:
        response.say("Namaste. Yeh Bandobast ka test call hai. Ab stream shuru hoga.", language="hi-IN")
        response.pause(length=2)
    connect = Connect()
    # Twilio posts stream-started/stopped/error here; the trial account hides its error logs.
    stream = connect.stream(
        url=f"wss://{public_host()}/ws",
        status_callback=f"https://{public_host()}/twilio/stream-status",
    )
    # Custom parameters arrive in the stream's "start" message as call_data.body.
    stream.parameter(name="language", value=language)
    stream.parameter(name="vendor_name", value=vendor_name)
    stream.parameter(name="family_name", value=family_name)
    response.append(connect)
    return Response(content=str(response), media_type="application/xml")


@app.post("/twilio/stream-status")
async def twilio_stream_status(request: Request):
    form = await request.form()
    event = form.get("StreamEvent")
    detail = f" error={form.get('StreamError')}" if form.get("StreamError") else ""
    log = logger.error if event == "stream-error" else logger.info
    log(f"Stream {form.get('StreamSid')} on call {form.get('CallSid')}: {event}{detail}")
    return {}


@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    logger.info(
        f"Twilio status {form.get('CallSid')}: {form.get('CallStatus')} "
        f"(duration {form.get('CallDuration', '-')}s)"
    )
    return {}


@app.websocket("/ws")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    transport_type, call_data = await parse_telephony_websocket(websocket)
    if transport_type != "twilio":
        logger.warning(f"Rejecting non-Twilio stream: {transport_type}")
        await websocket.close()
        return
    logger.info(f"Call connected: {call_data.call_id} params={call_data.body}")
    await run_vendor_call(websocket, call_data)


@app.get("/health")
async def health():
    return {"ok": True}
