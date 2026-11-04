"""FastAPI server: dashboard, browser test sessions, and Twilio phone calls.

Run:  uv run uvicorn server:app --port 8765
Then open http://localhost:8765
"""

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI
from pydantic import BaseModel, Field
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

import app_api
import app_calls
import enquiries
import confirmations
import browser
import catalog
import prompts
import rules
import store
import workflow
from bot import run_phone_call
from conversation import SessionConfig

# Values already in the environment win, so a run can override e.g. GROQ_MODEL=... uv run uvicorn ...
load_dotenv(Path(__file__).parent / ".env")

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    confirmations.recover_interrupted()
    yield
    await app_calls.close()
    await browser.close()


app = FastAPI(title="Bandobast", lifespan=lifespan)
app.include_router(app_api.router)  # the Flutter app (access key required)


def local_only(request: Request) -> None:
    """Dashboard and browser sessions spend API credits: refuse anything coming through the tunnel."""
    proxied = any(h in request.headers for h in ("cf-connecting-ip", "x-forwarded-for"))
    if proxied or (request.client and request.client.host not in ("127.0.0.1", "::1", "localhost")):
        raise HTTPException(403, "The dashboard is only available on localhost")


# Dashboard + browser testing (localhost only)

app.include_router(browser.router, dependencies=[Depends(local_only)])
app.mount("/client", SmallWebRTCPrebuiltUI)


@app.get("/", dependencies=[Depends(local_only)])
async def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/api/state", dependencies=[Depends(local_only)])
async def state():
    vendors = store.vendors()
    names = {
        "categories": {k: c["name"] for k, c in catalog.CATEGORIES.items()},
        "functions": {k: f["name"] for k, f in catalog.FUNCTIONS.items()},
    }
    event = store.current_event()
    if not event:
        return {"event": None, "vendors": vendors, "names": names}
    event_id, brief = event
    return {
        "event": {"id": event_id, "brief": brief, "label": catalog.function_label(brief)},
        "vendors": vendors,
        "names": names,
        "comparison": workflow.comparison(event_id, brief),
        "bookings": store.bookings(event_id),
        "checkins": store.checkins(event_id),
        "alerts": store.alerts(event_id),
        "calls": store.calls(event_id),
    }


class BrowserSession(BaseModel):
    mode: str
    vendor_id: str | None = None
    checkin_id: int | None = None


@app.post("/api/browser-session", dependencies=[Depends(local_only)])
async def browser_session(req: BrowserSession):
    try:
        if req.mode != "intake":
            workflow.require_event()
        if req.mode == "vendor_call":
            store.vendor(req.vendor_id or "")
        if req.mode == "checkin":
            store.checkin(req.checkin_id or 0)
        browser.set_next_session(req.model_dump())
    except (LookupError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"open": "/client/"}


class SampleBrief(BaseModel):
    kind: str = "griha_pravesh"


@app.post("/api/sample-brief", dependencies=[Depends(local_only)])
async def sample_brief(req: SampleBrief):
    if req.kind not in prompts.SAMPLE_BRIEFS:
        raise HTTPException(400, f"Unknown sample {req.kind}")
    return {"event_id": store.save_event(prompts.SAMPLE_BRIEFS[req.kind])}


@app.post("/api/book/{category}", dependencies=[Depends(local_only)])
async def book(category: str):
    if category not in rules.CATEGORIES:
        raise HTTPException(400, f"Unknown category {category}")
    try:
        result = workflow.book(category)
        enquiries.spawn(enquiries.confirmation_call(result['confirmation_id']))
        return result
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/checkins/{checkin_id}/unreachable", dependencies=[Depends(local_only)])
async def checkin_unreachable(checkin_id: int):
    try:
        return {"risk": workflow.mark_unreachable(checkin_id)}
    except (LookupError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/reset", dependencies=[Depends(local_only)])
async def reset():
    store.reset()
    return {"ok": True}


# Phone calls (Twilio)


class CallRequest(BaseModel):
    to: str = Field(description="Phone number in E.164, e.g. +919876543210")
    mode: str = "vendor_call"
    vendor_id: str | None = "C01"
    checkin_id: int | None = None
    confirmation_id: int | None = None
    event_id: int | None = None
    reconfirm: bool = False
    renegotiate: bool = False
    say_test: bool = False


def public_host() -> str:
    """PUBLIC_HOST as a bare hostname, tolerating a pasted https:// prefix or trailing slash."""
    host = os.environ["PUBLIC_HOST"].strip()
    for scheme in ("https://", "http://", "wss://"):
        host = host.removeprefix(scheme)
    return host.rstrip("/")


def start_call(req: CallRequest) -> str:
    """Ask Twilio to dial; the answered call streams to our /ws. Returns the call SID."""
    session = {"mode": req.mode, "vendor_id": req.vendor_id or "", "checkin_id": req.checkin_id or '',
               'confirmation_id': req.confirmation_id or '', 'event_id': req.event_id or '',
               'reconfirm': int(req.reconfirm), 'renegotiate': int(req.renegotiate)}
    SessionConfig.from_dict(session, channel="phone")
    host = public_host()
    # Twilio fetches the call instructions from /twiml when the call is answered.
    query = urlencode({**session, "say_test": int(req.say_test)})

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
    logger.info(f"Calling ({req.mode}, vendor {req.vendor_id}): {call.sid}")
    return call.sid


@app.post("/calls")
async def create_call(req: CallRequest, authorization: str = Header(default="")):
    # The tunnel makes this endpoint public, and every call costs money: require a token.
    expected = f"Bearer {os.environ.get('CONTROL_TOKEN', '')}"
    if not os.environ.get("CONTROL_TOKEN") or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Missing or wrong CONTROL_TOKEN")
    try:
        return {"call_sid": start_call(req)}
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(mode: str = "vendor_call", vendor_id: str = "", checkin_id: str = "", say_test: int = 0,
                confirmation_id: str = '', event_id: str = '', reconfirm: str = '', renegotiate: str = ''):
    """Call instructions Twilio fetches on answer: stream the call audio to /ws.

    With say_test=1, Twilio first speaks a line and pauses, to check that our
    instructions run at all before the stream starts.
    """
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
    stream.parameter(name="mode", value=mode)
    stream.parameter(name="vendor_id", value=vendor_id)
    stream.parameter(name="checkin_id", value=checkin_id)
    for name, value in {'confirmation_id': confirmation_id, 'event_id': event_id, 'reconfirm': reconfirm, 'renegotiate': renegotiate}.items():
        stream.parameter(name=name, value=value)
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
    await run_phone_call(websocket, call_data)


@app.get("/health")
async def health():
    return {"ok": True}
