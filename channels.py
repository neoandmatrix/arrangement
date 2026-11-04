"""How Bandobast reaches a vendor: ring their app, else phone them, else give up.

- app:   the vendor runs the Bandobast app (vendor mode). A ring goes out over the app's live
         connection and, if they gave us a push token, Firebase. They answer with a WebRTC call
         into the same conversation pipeline as every other channel.
- phone: vendors without the app get a normal call through Twilio (server.start_call). This needs
         a real number and a Twilio account that can stream media; the synthetic vendors' numbers
         are deliberately invalid, so the demo never dials them.
- none:  neither is possible; the vendor is marked unreachable.

Calls run one at a time (CALL_LOCK): the Gnani account allows a single active request.
"""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from loguru import logger

import push
import store

RING_SECONDS = 40  # then it counts as no answer
MAX_CALL_SECONDS = 15 * 60
CALL_LOCK = asyncio.Lock()
ICE_SERVERS = [{"urls": ["stun:stun.l.google.com:19302"]}]


@dataclass
class PendingCall:
    call_id: str
    vendor_id: str
    session: dict[str, Any]  # mode, vendor_id, checkin_id, renegotiate
    caller: dict[str, Any]  # what the ringing screen shows
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    declined: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


# Live app connections per vendor, and calls that are ringing or in progress.
_sockets: dict[str, set[WebSocket]] = {}
_calls: dict[str, PendingCall] = {}


def online(vendor_id: str) -> bool:
    return bool(_sockets.get(vendor_id))


def has_app(vendor_id: str) -> bool:
    return online(vendor_id) or bool((store.device(vendor_id) or {}).get("push_token"))


def can_phone(vendor: dict[str, Any]) -> bool:
    """A real-looking number and a Twilio account; the demo vendors have neither."""
    return not vendor["phone"].startswith("+9100000") and bool(os.environ.get("TWILIO_ACCOUNT_SID"))


def channel_for(vendor: dict[str, Any]) -> str:
    if has_app(vendor["id"]):
        return "app"
    if can_phone(vendor):
        return "phone"
    return "none"


async def connect_socket(vendor_id: str, ws: WebSocket) -> None:
    _sockets.setdefault(vendor_id, set()).add(ws)
    # A call may have started ringing just before the app connected.
    for call in _calls.values():
        if call.vendor_id == vendor_id and not call.answered.is_set():
            await _send(ws, _ring_message(call))


def disconnect_socket(vendor_id: str, ws: WebSocket) -> None:
    _sockets.get(vendor_id, set()).discard(ws)


def pending(call_id: str) -> PendingCall:
    call = _calls.get(call_id)
    if not call:
        raise KeyError(f"No such call {call_id}")
    return call


async def call(vendor: dict[str, Any], session: dict[str, Any], caller: dict[str, Any]) -> dict[str, Any]:
    """Reach one vendor and hold the line until the conversation ends.

    Returns {"result": answered | declined | no_answer | unreachable | failed, "channel", "call_id"}.
    Waits its turn: one call at a time.
    """
    channel = channel_for(vendor)
    if channel == "none":
        return {"result": "unreachable", "channel": "none", "call_id": None}
    async with CALL_LOCK:
        from app_calls import busy
        if busy(vendor["id"]):
            return {"result": "no_answer", "channel": channel, "call_id": None}
        if channel == "app":
            return await _call_app(vendor, session, caller)
        return await _call_phone(vendor, session)


async def _call_app(vendor: dict[str, Any], session: dict[str, Any], caller: dict[str, Any]) -> dict[str, Any]:
    call_id = f"AP{uuid.uuid4().hex[:12]}"
    pc = PendingCall(call_id=call_id, vendor_id=vendor["id"], session=session, caller=caller)
    _calls[call_id] = pc
    result = {"channel": "app", "call_id": call_id}
    try:
        await _ring(pc)
        answered = asyncio.create_task(pc.answered.wait())
        declined = asyncio.create_task(pc.declined.wait())
        done, _ = await asyncio.wait({answered, declined}, timeout=RING_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        for t in (answered, declined):
            t.cancel()
        if not pc.answered.is_set():
            await _broadcast(vendor["id"], {"type": "cancel", "call_id": call_id})
            return {**result, "result": "declined" if pc.declined.is_set() else "no_answer"}
        try:
            await asyncio.wait_for(pc.finished.wait(), MAX_CALL_SECONDS)
        except TimeoutError:
            logger.warning(f"Call {call_id} ran over {MAX_CALL_SECONDS}s")
            return {**result, "result": "failed"}
        return {**result, "result": "answered"}
    finally:
        _calls.pop(call_id, None)


def _ring_message(pc: PendingCall) -> dict[str, Any]:
    return {"type": "ring", "call_id": pc.call_id, "ring_seconds": RING_SECONDS, "kind": "enquiry", **pc.caller}


async def _ring(pc: PendingCall) -> None:
    message = _ring_message(pc)
    sent = await _broadcast(pc.vendor_id, message)
    token = (store.device(pc.vendor_id) or {}).get("push_token")
    if token and push.configured():
        # Wakes the app when it's closed; harmless when it's open (the app ignores a call it already shows).
        sent += await push.send(token, {k: v if isinstance(v, str) else json.dumps(v) for k, v in message.items()})
    logger.info(f"Ringing {pc.vendor_id} for call {pc.call_id} ({sent} channel(s))")


async def _broadcast(vendor_id: str, message: dict[str, Any]) -> int:
    sent = 0
    for ws in list(_sockets.get(vendor_id, ())):
        sent += await _send(ws, message)
    return sent


async def _send(ws: WebSocket, message: dict[str, Any]) -> int:
    try:
        await ws.send_json(message)
        return 1
    except Exception:
        return 0


async def _call_phone(vendor: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Dial through Twilio, then wait for the call record the phone pipeline saves at hang-up."""
    from server import CallRequest, start_call  # server imports this module

    try:
        sid = await asyncio.to_thread(
            start_call,
            CallRequest(to=vendor["phone"], mode=session["mode"], vendor_id=vendor["id"], checkin_id=session.get("checkin_id")),
        )
    except Exception as e:
        logger.warning(f"Phone call to {vendor['id']} failed: {e}")
        return {"result": "failed", "channel": "phone", "call_id": None}
    for _ in range((RING_SECONDS + MAX_CALL_SECONDS) // 2):
        await asyncio.sleep(2)
        if any(c["id"] == sid for c in store.calls(None)):
            return {"result": "answered", "channel": "phone", "call_id": sid}
    return {"result": "no_answer", "channel": "phone", "call_id": sid}
