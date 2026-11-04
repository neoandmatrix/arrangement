"""The Bandobast app's API: the family plans a function, vendors take calls.

Every request carries the access key (X-App-Key header, or ?key= on the WebSocket), because
the app reaches this server through a public tunnel and calls spend API credits. Set APP_KEY
in .env; without it only requests from this machine are accepted.

Family:  catalog, submit a function, follow its progress, approve / reject / renegotiate quotes.
Vendor:  sign in as a demo vendor, keep a live connection for rings, answer with WebRTC, see jobs.
"""

import os
import secrets
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, Field

import browser
import app_calls
import catalog
import channels
import enquiries
import prompts
import rules
import store
import workflow
from conversation import brief_problems

_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


def _key_ok(key: str, request_host: str | None, proxied: bool) -> bool:
    expected = os.environ.get("APP_KEY", "")
    if expected:
        return secrets.compare_digest(key, expected)
    return not proxied and request_host in _LOCAL_HOSTS


def app_key(request: Request, x_app_key: str = Header(default="")) -> None:
    proxied = any(h in request.headers for h in ("cf-connecting-ip", "x-forwarded-for"))
    if not _key_ok(x_app_key, request.client.host if request.client else None, proxied):
        raise HTTPException(401, "Wrong or missing app key")


router = APIRouter(prefix="/app")
api = APIRouter(dependencies=[Depends(app_key)])


# Shared


@api.get("/catalog")
async def get_catalog():
    """Functions and services for the family's form."""
    return {
        "functions": [
            {
                "key": key,
                "name": f["name"],
                "variants": [{"key": k, "name": v} for k, v in f["variants"].items()],
                "default_needs": f["default_needs"],
                "optional": f["optional"],
            }
            for key, f in catalog.FUNCTIONS.items()
        ],
        "services": [
            {
                "key": key,
                "name": c["name"],
                "details_hint": c["details"],
                "negotiable": key in rules.NEGOTIABLE,
            }
            for key, c in catalog.CATEGORIES.items()
        ],
        "languages": [{"code": code, "name": name} for code, name in prompts.LANGUAGE_NAMES.items()],
        "tent_suggested_above_guests": catalog.TENT_SUGGESTED_ABOVE_GUESTS,
    }


# Family


class Location(BaseModel):
    lat: float
    lon: float
    label: str = ""


class FunctionRequest(BaseModel):
    function_type: str
    function_variant: str
    family_name: str
    contact_name: str = ""
    family_language: str = "hi-IN"
    location: Location
    area: str
    date: str = Field(description="YYYY-MM-DD")
    start_time: str = Field(description="HH:MM, the muhurat")
    guests: int
    cuisine: str = ""
    rituals: str = ""
    needs: list[str]
    budget: dict[str, int] = {}
    details: dict[str, str] = {}


@api.post("/events")
async def create_event(req: FunctionRequest, request: Request):
    """The family submitted the form: save it and start finding and calling vendors."""
    participant_id = app_calls.identity(request)
    if not store.participant(participant_id):
        raise HTTPException(403, "Only a family phone can plan a function")
    args = req.model_dump()
    unknown = [n for n in req.needs if n not in catalog.CATEGORIES]
    problems = [f"unknown service {n}" for n in unknown]
    problems += brief_problems({**args, "city": "Bengaluru", **{f"details_{k}": v for k, v in req.details.items()}})
    if req.family_language not in prompts.LANGUAGE_NAMES:
        problems.append("the family's language")
    if problems:
        raise HTTPException(400, "; ".join(problems))
    brief = {
        **args,
        "family_id": participant_id,
        "city": "Bengaluru",
        "location": req.location.model_dump(),
        "budget": {k: v for k, v in req.budget.items() if v and k in req.needs},
        "details": {k: v.strip() for k, v in req.details.items() if v.strip() and k in req.needs},
    }
    event_id = store.save_event(brief)
    enquiries.spawn(enquiries.run_event(event_id, brief))
    logger.info(f"App: new function {event_id}: {catalog.function_label(brief)}")
    return {"event_id": event_id}


@api.get("/events/current")
async def current_event(request: Request):
    event = store.current_event()
    if not event:
        return {"event": None}
    event_id, brief = event
    if brief.get("family_id") and not app_calls.owns_event(request.headers.get("x-participant-id", ""), brief):
        return {"event": None}
    return event_status(event_id, brief)


def event_status(event_id: int, brief: dict[str, Any]) -> dict[str, Any]:
    """Everything the family's screen shows, per service."""
    comparison = workflow.comparison(event_id, brief)
    status = {e["vendor_id"]: e for e in store.enquiries(event_id)}
    found = store.discovered(event_id)
    bookings = store.bookings(event_id)
    venue = rules.venue_location(brief)
    services = []
    for category in brief.get("needs", []):
        cmp = comparison[category]
        ranked = {r["vendor_id"]: (i, r) for i, r in enumerate(cmp["ranked"])}
        vendors = []
        for vendor_id, e in status.items():
            if e["category"] != category:
                continue
            v = store.vendor(vendor_id)
            rank, row = ranked.get(vendor_id, (None, None))
            quote = store.latest_quotes(event_id).get(vendor_id) if e["status"] == "rejected" else (row or {}).get("quote")
            vendors.append(
                {
                    "vendor_id": vendor_id,
                    "name": v["name"],
                    "area": v["area"],
                    "language": v["language"],
                    "rating": v["rating"],
                    "review_count": v["review_count"],
                    "distance_km": round(rules.distance_km(*venue, v["lat"], v["lon"]), 1) if venue else None,
                    "status": e["status"],
                    "channel": e["channel"],
                    "note": e["note"],
                    "rank": rank,
                    "top": vendor_id == cmp["top"],
                    "quote": quote,
                    "landed_cost": (row or {}).get("landed_cost"),
                    "flags": (row or {}).get("flags", []),
                    "bookable": bool(row and row["bookable"]),
                    "savings": rules.savings(quote, brief) if quote else None,
                }
            )
        vendors.sort(key=lambda x: (x["rank"] is None, x["rank"] if x["rank"] is not None else 0))
        booked = {b["role"]: b for b in bookings if b["category"] == category}
        services.append(
            {
                "category": category,
                "name": catalog.CATEGORIES[category]["name"],
                "negotiable": category in rules.NEGOTIABLE,
                "budget": (brief.get("budget") or {}).get(category),
                "vendors": vendors,
                "discovered": found.get(category, []),
                "booking": {role: {"vendor_id": b["vendor_id"], "status": b["status"]} for role, b in booked.items()},
            }
        )
    return {
        "event": {"id": event_id, "brief": brief, "label": catalog.function_label(brief)},
        "services": services,
        "alerts": store.alerts(event_id),
        "checkins": store.checkins(event_id),
        "updates": store.updates(event_id),
        "calling_now": any(e["status"] in ("ringing", "in_call", "renegotiating") for e in status.values()),
    }


def _event(request: Request) -> tuple[int, dict[str, Any]]:
    try:
        event_id, brief = workflow.require_event()
        if brief.get("family_id") and not app_calls.owns_event(request.headers.get("x-participant-id", ""), brief):
            raise HTTPException(403, "This function belongs to another demo family")
        return event_id, brief
    except LookupError as e:
        raise HTTPException(404, str(e))


@api.post("/events/current/vendors/{vendor_id}/approve")
async def approve(vendor_id: str, request: Request):
    _event(request)
    try:
        return workflow.book(store.vendor(vendor_id)["category"], vendor_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@api.post("/events/current/vendors/{vendor_id}/reject")
async def reject(vendor_id: str, request: Request):
    _event(request)
    try:
        workflow.reject(vendor_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@api.post("/events/current/vendors/{vendor_id}/renegotiate")
async def renegotiate(vendor_id: str, request: Request):
    event_id, brief = _event(request)
    try:
        vendor = store.vendor(vendor_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if vendor["category"] not in rules.NEGOTIABLE:
        raise HTTPException(400, f"We don't bargain with a {vendor['category']}")
    if vendor_id not in store.latest_quotes(event_id):
        raise HTTPException(400, "No quote from this vendor yet")
    enquiries.spawn(enquiries.renegotiate(event_id, brief, vendor_id))
    return {"ok": True}


@api.post("/checkins/{checkin_id}/call")
async def call_checkin(checkin_id: int, request: Request):
    """Demo: run a scheduled check-in call now instead of waiting for its time."""
    event_id, brief = _event(request)
    try:
        checkin = store.checkin(checkin_id)
        if checkin["event_id"] != event_id or checkin["status"] != "pending":
            raise HTTPException(400, "This check-in is no longer pending for this function")
    except (KeyError, LookupError) as e:
        raise HTTPException(404, str(e))
    enquiries.spawn(enquiries.checkin_call(event_id, brief, checkin_id))
    return {"ok": True}


# Vendor


@api.get("/vendors")
async def demo_vendors(category: str | None = None):
    """The synthetic vendors a phone can sign in as."""
    return [
        {k: v[k] for k in ("id", "category", "name", "area", "language", "rating")}
        for v in store.vendors()
        if not category or v["category"] == category
    ]


class Device(BaseModel):
    vendor_id: str
    push_token: str | None = None


@api.post("/vendor/register")
async def register(device: Device, request: Request):
    try:
        vendor = store.vendor(device.vendor_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if app_calls.identity(request) != device.vendor_id:
        raise HTTPException(403, "This registration belongs to another vendor")
    store.save_device(device.vendor_id, device.push_token)
    return {"vendor": vendor, "push": bool(device.push_token)}


@api.get("/vendor/{vendor_id}/jobs")
async def vendor_jobs(vendor_id: str, request: Request):
    """This vendor's enquiries, bookings and upcoming check-ins for the current function."""
    if app_calls.identity(request) != vendor_id:
        raise HTTPException(403, "These jobs belong to another vendor")
    event = store.current_event()
    if not event:
        return {"event": None, "enquiry": None, "booking": None, "checkins": []}
    event_id, brief = event
    booking = next((b for b in store.bookings(event_id) if b["vendor_id"] == vendor_id), None)
    return {
        "event": {
            "label": catalog.function_label(brief),
            "family": brief.get("family_name"),
            "area": brief.get("area"),
            "date": brief.get("date"),
            "start_time": brief.get("start_time"),
            "guests": brief.get("guests"),
            "family_id": brief.get("family_id"),
        },
        "enquiry": store.enquiry(event_id, vendor_id),
        "quote": store.latest_quotes(event_id).get(vendor_id),
        "booking": booking,
        "checkins": [c for c in store.checkins(event_id) if c["vendor_id"] == vendor_id],
        "updates": [u for u in store.updates(event_id) if u["vendor_id"] == vendor_id],
    }


@api.api_route("/calls/{call_id}/answer", methods=["POST", "PATCH"])
async def answer(call_id: str, request: Request):
    """The vendor picked up: POST the WebRTC offer (PATCH adds ICE candidates)."""
    try:
        call = channels.pending(call_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if app_calls.identity(request) != call.vendor_id:
        raise HTTPException(403, "This call belongs to another vendor")
    data = await request.json()
    if request.method == "POST" and call.answered.is_set() and not data.get("pc_id"):
        raise HTTPException(409, "This call was already answered")
    if request.method == "POST":
        call.answered.set()
        event = store.current_event()
        if event:
            store.set_enquiry(event[0], call.vendor_id, store.vendor(call.vendor_id)["category"], "in_call", call_id=call_id)
    return await browser.webrtc_offer(
        data, request.method, call.session, channel="app", call_id=call_id, on_done=call.finished.set
    )


@api.post("/calls/{call_id}/decline")
async def decline(call_id: str, request: Request):
    if call_id.startswith("DC"):
        return await app_calls.end(call_id, request)
    try:
        pc = channels.pending(call_id)
        if app_calls.identity(request) != pc.vendor_id:
            raise HTTPException(403, "This call belongs to another vendor")
        pc.declined.set()
    except KeyError:
        pass  # already over
    return {"ok": True}


@api.post("/updates/{update_id}/{decision}")
async def decide_update(update_id: int, decision: str, request: Request):
    event_id, _ = _event(request)
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "Choose approve or reject")
    try:
        workflow.decide_update(event_id, update_id, decision == "approve")
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


api.include_router(app_calls.api)
router.include_router(api)
router.include_router(app_calls.ws_router)


@router.websocket("/vendor/ws")
async def vendor_socket(ws: WebSocket, vendor_id: str = Query(), key: str = Query(default="")):
    """The app keeps this open while running; rings arrive here."""
    proxied = any(h in ws.headers for h in ("cf-connecting-ip", "x-forwarded-for"))
    if not _key_ok(key, ws.client.host if ws.client else None, proxied):
        await ws.close(code=4401)
        return
    try:
        store.vendor(vendor_id)
    except KeyError:
        await ws.close(code=4404)
        return
    await ws.accept()
    await channels.connect_socket(vendor_id, ws)
    logger.info(f"Vendor {vendor_id} online")
    try:
        while True:
            message = await ws.receive_json()
            if message.get("type") == "ping":
                await ws.send_json({"type": "pong", "today": date.today().isoformat()})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        channels.disconnect_socket(vendor_id, ws)
        logger.info(f"Vendor {vendor_id} offline")
