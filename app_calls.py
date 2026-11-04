"""App calls over PCM WebSockets. Direct human calls are relayed, never transcribed or recorded.

The shared app key is demo access control, not production identity. Audio uses short-lived,
participant-bound random tickets so the key isn't placed in an audio URL.
"""

import asyncio
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame, EndFrame
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

import channels
import push
import store
import confirmation_rules
from conversation import SessionConfig, run_conversation

api = APIRouter()
ws_router = APIRouter()
_presence: dict[str, set[WebSocket]] = {}
_sessions: dict[str, "AudioSession"] = {}
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def valid_participant(participant_id: str) -> bool:
    return bool(store.participant(participant_id)) or any(v["id"] == participant_id for v in store.vendors())


def identity(request: Request) -> str:
    participant_id = request.headers.get("x-participant-id", "")
    if not valid_participant(participant_id):
        raise HTTPException(401, "Register this demo phone first")
    return participant_id


def owns_event(participant_id: str, brief: dict[str, Any]) -> bool:
    return participant_id == brief.get("family_id")


def direct_allowed(caller: str, target: str, event_id: int) -> bool:
    _, brief = store.event(event_id)
    family = brief.get("family_id")
    if not family or caller == target or family not in (caller, target):
        return False
    vendor = target if caller == family else caller
    return any(b["vendor_id"] == vendor and b["role"] == "primary" and b["status"] in confirmation_rules.RESERVED
               for b in store.bookings(event_id))


def busy(participant_id: str) -> bool:
    return any(participant_id in s.participants for s in _sessions.values())


@dataclass
class AudioSession:
    id: str
    participants: tuple[str, ...]
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    sockets: dict[str, WebSocket] = field(default_factory=dict)
    send_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    answered: bool = False
    owns_lock: bool = False
    expiry: asyncio.Task | None = None
    worker: asyncio.Task | None = None


async def send_json(ws: WebSocket, data: dict[str, Any]) -> None:
    try:
        await ws.send_json(data)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def notify(participant_id: str, data: dict[str, Any]) -> int:
    count = 0
    for ws in list(_presence.get(participant_id, ())):
        try:
            await ws.send_json(data)
            count += 1
        except (RuntimeError, WebSocketDisconnect):
            pass
    token = (store.participant(participant_id) or store.device(participant_id) or {}).get("push_token")
    if token and push.configured():
        try:
            count += await push.send(token, {k: v if isinstance(v, str) else json.dumps(v) for k, v in data.items()})
        except Exception:
            logger.warning("Direct-call push failed")
    return count


async def finish(session_id: str, reason: str = "Call ended") -> None:
    s = _sessions.pop(session_id, None)
    if not s:
        return
    if s.expiry and s.expiry is not asyncio.current_task():
        s.expiry.cancel()
    for ws in list(s.sockets.values()):
        await send_json(ws, {"type": "ended", "reason": reason})
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
    if s.kind == "direct":
        for participant_id in s.participants:
            await notify(participant_id, {"type": "cancel", "call_id": s.id})
    if s.owns_lock and s.worker is None:
        s.owns_lock = False
        channels.CALL_LOCK.release()
    if s.kind == "enquiry" and s.worker is None:
        try:
            pc = channels.pending(s.id)
            if not pc.answered.is_set():
                pc.declined.set()
            pc.finished.set()
        except KeyError:
            pass


async def expire(session_id: str, seconds: int) -> None:
    await asyncio.sleep(seconds)
    await finish(session_id, "No answer" if seconds == channels.RING_SECONDS else "Call time limit reached")


def ticket(s: AudioSession, participant_id: str) -> dict[str, Any]:
    token = s.tokens.setdefault(participant_id, secrets.token_urlsafe(32))
    return {"call_id": s.id, "audio_path": f"/app/audio/{s.id}", "audio_token": token,
            "sample_rate_in": 16000, "sample_rate_out": 16000 if s.kind == "direct" else 24000,
            "kind": s.kind}


@api.post("/participants/register")
async def register(request: Request):
    data = await request.json()
    participant_id = data.get("participant_id", "")
    if not isinstance(participant_id, str) or not re.fullmatch(r"F[0-9a-f]{8}", participant_id):
        raise HTTPException(400, "Invalid demo family ID")
    store.save_participant(participant_id, data.get("push_token"))
    return {"participant_id": participant_id}


@api.post("/assistant-calls")
async def assistant_call(request: Request):
    participant_id = identity(request)
    data = await request.json()
    mode = data.get("mode")
    if mode == "form":
        if not store.participant(participant_id):
            raise HTTPException(403, "Voice filling is for a family phone")
        import prompts
        if data.get("language", "hi-IN") not in prompts.LANGUAGE_NAMES:
            raise HTTPException(400, "Unsupported language")
        if data.get("draft") is not None and not isinstance(data["draft"], dict):
            raise HTTPException(400, "Form draft must contain named fields")
        session_data = {"mode": "form", "language": data.get("language", "hi-IN"), "draft": data.get("draft") or {}}
    elif mode == "vendor_update":
        event = store.current_event()
        if not event or not store.enquiry(event[0], participant_id):
            raise HTTPException(400, "No enquiry or booking for this vendor yet")
        session_data = {"mode": mode, "event_id": event[0], "vendor_id": participant_id}
    else:
        raise HTTPException(400, "Unsupported assistant call")
    if busy(participant_id) or channels.CALL_LOCK.locked():
        raise HTTPException(409, "Bandobast is on another call. Please try again shortly.")
    await channels.CALL_LOCK.acquire()
    s = AudioSession("AI" + secrets.token_hex(6), (participant_id,), "assistant", session_data, owns_lock=True)
    _sessions[s.id] = s
    s.expiry = spawn(expire(s.id, channels.RING_SECONDS))
    return ticket(s, participant_id)


@api.post("/direct-calls")
async def direct_call(request: Request):
    caller = identity(request)
    data = await request.json()
    target = data.get("target_id", "")
    event = store.current_event()
    if not event or not direct_allowed(caller, target, event[0]):
        raise HTTPException(403, "Direct calls are only between the family and its active booked vendor")
    if busy(caller) or busy(target) or any(pc.vendor_id in (caller, target) for pc in channels._calls.values()):
        raise HTTPException(409, "One of these phones is already on a call")
    s = AudioSession("DC" + secrets.token_hex(6), (caller, target), "direct", {"event_id": event[0]})
    _sessions[s.id] = s
    s.expiry = spawn(expire(s.id, channels.RING_SECONDS))
    _, brief = event
    name = f"{brief.get('family_name', 'Family')} family" if caller == brief.get("family_id") else store.vendor(caller)["name"]
    sent = await notify(target, {"type": "ring", "call_id": s.id, "kind": "direct", "caller_name": name,
                               "caller_id": caller, "family": brief.get("family_name", ""), "purpose": "direct",
                               "function": "Direct call", "ring_seconds": channels.RING_SECONDS})
    if not sent:
        await finish(s.id, "The other phone is offline. Ask them to open Bandobast.")
        raise HTTPException(409, "The other phone is offline. Ask them to open Bandobast.")
    return {**ticket(s, caller), "caller_name": name, "target_id": target}


@api.post("/calls/{call_id}/connect")
async def connect(call_id: str, request: Request):
    participant_id = identity(request)
    s = _sessions.get(call_id)
    if not s:
        try:
            pc = channels.pending(call_id)
        except KeyError:
            raise HTTPException(404, "This call has ended")
        if pc.vendor_id != participant_id:
            raise HTTPException(403, "This call is for another vendor")
        if busy(participant_id):
            raise HTTPException(409, "This phone is already on a call")
        s = AudioSession(call_id, (participant_id,), "enquiry", pc.session)
        _sessions[call_id] = s
        s.expiry = spawn(expire(call_id, channels.RING_SECONDS))
    if participant_id not in s.participants:
        raise HTTPException(403, "This call is for another phone")
    return ticket(s, participant_id)


@api.post("/sessions/{call_id}/end")
async def end(call_id: str, request: Request):
    participant_id = identity(request)
    s = _sessions.get(call_id)
    if s and participant_id not in s.participants:
        raise HTTPException(403, "This call belongs to another phone")
    await finish(call_id)
    return {"ok": True}


class PCMSerializer(FrameSerializer):
    """Binary messages are mono signed little-endian PCM16; text carries call controls."""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "clear"})
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            if 0 < len(data) <= 6400 and len(data) % 2 == 0:
                return InputAudioRawFrame(data, sample_rate=16000, num_channels=1)
            return None
        try:
            if json.loads(data).get("type") == "hangup":
                return EndFrame()
        except (ValueError, AttributeError):
            pass
        return None


@ws_router.websocket("/audio/{call_id}")
async def audio(ws: WebSocket, call_id: str, token: str = ""):
    token = ws.headers.get("authorization", "").removeprefix("Bearer ") or token
    s = _sessions.get(call_id)
    participant_id = next((p for p, t in s.tokens.items() if secrets.compare_digest(t, token)), None) if s else None
    if not s or not participant_id or participant_id in s.sockets:
        await ws.close(code=4403)
        return
    if s.kind == "direct" and not direct_allowed(*s.participants, s.data["event_id"]):
        await ws.close(code=4403)
        await finish(call_id, "Booking is no longer active")
        return
    if s.kind == "enquiry":
        try:
            pc = channels.pending(call_id)
        except KeyError:
            await ws.close(code=4404)
            await finish(call_id)
            return
        if pc.answered.is_set():
            await ws.close(code=4409)
            return
        pc.answered.set()
        event_id = pc.session.get("event_id")
        if event_id:
            store.set_enquiry(event_id, participant_id, store.vendor(participant_id)["category"], "in_call", call_id=call_id)
    await ws.accept()
    s.worker = asyncio.current_task()
    s.sockets[participant_id] = ws
    s.send_locks[participant_id] = asyncio.Lock()
    try:
        if s.kind == "direct":
            if participant_id == s.participants[1]:
                s.answered = True
                if s.expiry:
                    s.expiry.cancel()
                s.expiry = spawn(expire(call_id, channels.MAX_CALL_SECONDS))
            if s.answered:
                for socket in s.sockets.values():
                    await send_json(socket, {"type": "connected"})
            else:
                await send_json(ws, {"type": "ringing"})
            await relay(s, participant_id, ws)
        else:
            if s.expiry:
                s.expiry.cancel()
            s.expiry = spawn(expire(call_id, channels.MAX_CALL_SECONDS))
            async def result(data: dict[str, Any]) -> None:
                await send_json(ws, data)
            cfg = SessionConfig.from_dict(s.data, channel="app", call_id=call_id)
            cfg.on_result = result
            transport = FastAPIWebsocketTransport(ws, FastAPIWebsocketParams(
                audio_in_enabled=True, audio_out_enabled=True, serializer=PCMSerializer(), add_wav_header=False,
            ))
            await send_json(ws, {"type": "connected"})
            await run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as error:
        # Avoid expanded trace locals: this scope holds the participant's audio ticket.
        logger.error("App audio call {} failed ({})", call_id, type(error).__name__)
        await send_json(ws, {"type": "error", "reason": "Bandobast could not complete this call. Please try again."})
    finally:
        await finish(call_id)
        # Keep the Gnani single-call lease until pipeline cleanup has really completed,
        # even when another task closes the socket or the ring/call deadline expires.
        if s.owns_lock:
            s.owns_lock = False
            channels.CALL_LOCK.release()
        if s.kind == "enquiry":
            try:
                channels.pending(call_id).finished.set()
            except KeyError:
                pass


async def relay(s: AudioSession, participant_id: str, ws: WebSocket) -> None:
    """No storage or LLM on the human-to-human audio path; send backpressure bounds buffering."""
    target = next(p for p in s.participants if p != participant_id)
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None and s.answered and target in s.sockets:
            if 0 < len(data) <= 6400 and len(data) % 2 == 0:
                async with s.send_locks[target]:
                    await s.sockets[target].send_bytes(data)
        elif message.get("text"):
            try:
                if json.loads(message["text"]).get("type") == "hangup":
                    return
            except (ValueError, AttributeError):
                pass


@ws_router.websocket("/presence/ws")
async def presence(ws: WebSocket, participant_id: str = "", key: str = ""):
    from app_api import _key_ok
    proxied = any(h in ws.headers for h in ("cf-connecting-ip", "x-forwarded-for"))
    key = ws.headers.get("x-app-key", "") or key
    if not _key_ok(key, ws.client.host if ws.client else None, proxied) or not valid_participant(participant_id):
        await ws.close(code=4401)
        return
    await ws.accept()
    _presence.setdefault(participant_id, set()).add(ws)
    vendor = any(v["id"] == participant_id for v in store.vendors())
    if vendor:
        await channels.connect_socket(participant_id, ws)
    for s in list(_sessions.values()):
        if s.kind == "direct" and s.participants[1] == participant_id and not s.answered:
            await send_json(ws, {"type": "ring", "call_id": s.id, "kind": "direct", "purpose": "direct",
                                 "caller_id": s.participants[0], "caller_name": s.participants[0], "ring_seconds": channels.RING_SECONDS})
    try:
        while True:
            message = await ws.receive_json()
            if message.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        _presence.get(participant_id, set()).discard(ws)
        if vendor:
            channels.disconnect_socket(participant_id, ws)


async def close() -> None:
    for session_id in list(_sessions):
        await finish(session_id, "Server shutting down")
