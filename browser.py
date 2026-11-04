"""Browser channel: talk to Bandobast from localhost through WebRTC, playing the vendor or the family.

The dashboard picks who you'll be (POST /api/browser-session), then opens the prebuilt
Pipecat UI at /client/, which calls /start and then /sessions/{id}/api/offer.
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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
_running: set[asyncio.Task] = set()  # keep references so conversations aren't garbage-collected

ICE_CONFIG = {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}


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
    return {"sessionId": session_id, "iceConfig": ICE_CONFIG}


@router.api_route("/sessions/{session_id}/api/offer", methods=["POST", "PATCH"])
async def offer(session_id: str, request: Request):
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown session")
    return await webrtc_offer(await request.json(), request.method, session, channel="browser")


async def webrtc_offer(
    data: dict[str, Any],
    method: str,
    session: dict[str, Any],
    *,
    channel: str,
    call_id: str | None = None,
    on_done: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """Answer a WebRTC offer (POST) or add ICE candidates (PATCH); a new connection starts the conversation.

    Shared by the dashboard's browser test and the app's incoming calls. on_done runs when the
    conversation has ended and its call record is saved.
    """
    if method == "PATCH":
        await _handler.handle_patch_request(
            SmallWebRTCPatchRequest(
                pc_id=data["pc_id"],
                candidates=[IceCandidate(**c) for c in data.get("candidates", [])],
            )
        )
        return {"status": "success"}

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        cfg = SessionConfig.from_dict(session, channel=channel, call_id=call_id)
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        logger.info(f"WebRTC session ({channel}) {cfg.call_id}: {session}")

        async def run() -> None:
            try:
                await run_conversation(transport, cfg, sample_rate_in=SAMPLE_RATE_IN, sample_rate_out=SAMPLE_RATE_OUT)
            except Exception:
                logger.exception(f"Conversation {cfg.call_id} failed")
            finally:
                if on_done:
                    on_done()

        task = asyncio.create_task(run())
        _running.add(task)
        task.add_done_callback(_running.discard)

    logger.info(f"WebRTC offer ({channel}) candidates: {_candidates(data.get('sdp', ''))}")
    answer = await _handler.handle_web_request(
        request=SmallWebRTCRequest(
            sdp=data["sdp"],
            type=data["type"],
            pc_id=data.get("pc_id"),
            restart_pc=data.get("restart_pc"),
        ),
        webrtc_connection_callback=on_connection,
    )
    if answer:
        logger.info(f"WebRTC answer ({channel}) candidates: {_candidates(answer.get('sdp', ''))}")
    return answer


def _candidates(sdp: str) -> list[str]:
    """'udp 172.19.216.28:50123 host' per ICE candidate: why a call connects or doesn't."""
    out = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            parts = line.split()
            # foundation component transport priority address port typ type ...
            if len(parts) >= 8:
                out.append(f"{parts[2].lower()} {parts[4]}:{parts[5]} {parts[7]}")
    return out or ["none"]
