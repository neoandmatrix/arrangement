"""Browser channel: talk to Bandobast from localhost through WebRTC, playing the vendor or the family.

The dashboard picks who you'll be (POST /api/browser-session), then opens the prebuilt
Pipecat UI at /client/, which calls /start and then /sessions/{id}/api/offer.
"""

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from loguru import logger
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from conversation import SessionConfig, run_conversation

SAMPLE_RATE_IN = 16000  # Prisma's wideband rate
SAMPLE_RATE_OUT = 24000

router = APIRouter()
_handler = SmallWebRTCRequestHandler()

# The dashboard's choice for the next browser session, and the sessions /start created.
_next_session: dict[str, Any] = {}
_sessions: dict[str, dict[str, Any]] = {}


def set_next_session(data: dict[str, Any]) -> None:
    SessionConfig.from_dict(data, channel="browser")  # validate now, not when the mic opens
    _next_session.clear()
    _next_session.update(data)


async def close() -> None:
    await _handler.close()


@router.post("/start")
async def start(request: Request):
    if not _next_session:
        raise HTTPException(400, "Pick a conversation on the dashboard first")
    session_id = str(uuid.uuid4())
    _sessions[session_id] = dict(_next_session)
    return {"sessionId": session_id, "iceConfig": {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}}


@router.api_route("/sessions/{session_id}/api/offer", methods=["POST", "PATCH"])
async def offer(session_id: str, request: Request, background_tasks: BackgroundTasks):
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown session")
    data = await request.json()

    if request.method == "PATCH":
        await _handler.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=data["pc_id"],
                candidates=[IceCandidate(**c) for c in data.get("candidates", [])],
            )
        )
        return {"status": "success"}

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        cfg = SessionConfig.from_dict(session, channel="browser")
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        logger.info(f"Browser session {session_id}: {session}")
        background_tasks.add_task(
            run_conversation,
            transport,
            cfg,
            sample_rate_in=SAMPLE_RATE_IN,
            sample_rate_out=SAMPLE_RATE_OUT,
        )

    return await _handler.handle_web_request(
        request=SmallWebRTCRequest(
            sdp=data["sdp"],
            type=data["type"],
            pc_id=data.get("pc_id"),
            restart_pc=data.get("restart_pc"),
        ),
        webrtc_connection_callback=on_connection,
    )
