"""Public web demo: try Bandobast in a browser, as the family or as a vendor.

Family:  fill a short form; Bandobast calls one simulated vendor per chosen service (max two). The
         vendor is an LLM persona with its own voice (vendor_bot.py) on a real voice call; the
         visitor follows the transcript live, hears the recording, then approves, rejects or asks
         Bandobast to renegotiate.
Vendor:  see a family's request, accept it and talk to Bandobast with the browser's microphone,
         over the same PCM WebSocket the app uses. Human calls are not recorded.

Enabled with DEMO_ENABLED=1. Run it on its own database (BANDOBAST_DB=demo.db), because the
demo creates a function per visitor. Every call spends Gnani and LLM credits, so calls are
capped per day (DEMO_DAILY_CALLS) and per visitor IP (DEMO_IP_CALLS); one call runs at a time.
"""

import asyncio
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field

import app_calls
import catalog
import channels
import enquiries
import price_safety
import prompts
import rules
import store
from conversation import _transcript, brief_problems
from vendor_bot import VendorBot, pricing

ROOT = Path(__file__).parent
RECORDINGS = Path(os.environ.get("DEMO_RECORDINGS", ROOT / "demo_recordings"))
USAGE_PATH = RECORDINGS / "usage.json"
MAX_SERVICES = 2

AREAS = {
    "HSR Layout": (12.9116, 77.6474), "Koramangala": (12.9352, 77.6245), "Jayanagar": (12.9250, 77.5938),
    "Indiranagar": (12.9719, 77.6412), "BTM Layout": (12.9166, 77.6101), "JP Nagar": (12.9063, 77.5857),
    "Malleshwaram": (13.0035, 77.5709), "Whitefield": (12.9698, 77.7500), "Marathahalli": (12.9591, 77.6974),
    "Electronic City": (12.8452, 77.6602), "Bellandur": (12.9304, 77.6784), "Hebbal": (13.0358, 77.5970),
}

router = APIRouter(prefix="/demo")


def enabled() -> bool:
    return os.environ.get("DEMO_ENABLED") == "1"


# Limits


def _limits() -> tuple[int, int]:
    return int(os.environ.get("DEMO_DAILY_CALLS", "40")), int(os.environ.get("DEMO_IP_CALLS", "4"))


def _usage() -> dict[str, Any]:
    try:
        usage = json.loads(USAGE_PATH.read_text())
    except (OSError, ValueError):
        usage = {}
    if usage.get("day") != date.today().isoformat():
        usage = {"day": date.today().isoformat(), "total": 0, "ips": {}}
    return usage


def visitor(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def spend(request: Request, calls: int) -> None:
    """Reserve calls from today's allowance, or refuse with a message the page shows."""
    daily, per_ip = _limits()
    usage, ip = _usage(), visitor(request)
    if usage["total"] + calls > daily:
        raise HTTPException(429, "Today's demo calls are used up (they cost real API credits). Please try again tomorrow.")
    if usage["ips"].get(ip, 0) + calls > per_ip:
        raise HTTPException(429, f"You have used your {per_ip} demo calls for today. Thanks for trying Bandobast!")
    usage["total"] += calls
    usage["ips"][ip] = usage["ips"].get(ip, 0) + calls
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage))


# Runs (family side)


@dataclass
class DemoCall:
    category: str
    vendor_id: str
    purpose: str = "enquiry"  # enquiry | renegotiation
    status: str = "waiting"  # waiting | ringing | in_call | done | failed
    bot: VendorBot | None = None
    call_id: str | None = None
    recording: Path | None = None
    vendor_lines: list[dict[str, str]] = field(default_factory=list)


@dataclass
class DemoRun:
    id: str
    event_id: int
    brief: dict[str, Any]
    calls: list[DemoCall] = field(default_factory=list)
    decisions: dict[str, str] = field(default_factory=dict)  # vendor_id -> approved | rejected
    task: asyncio.Task | None = None


_runs: dict[str, DemoRun] = {}
_queue: list[str] = []  # call keys waiting or running, for "N ahead of you"


async def _run_calls(run: DemoRun, calls: list[DemoCall], audio_base: str) -> None:
    for call in calls:
        key = f"{run.id}:{id(call)}"
        _queue.append(key)
        try:
            await _run_one(run, call, audio_base)
        except Exception:
            logger.exception(f"Demo run {run.id} call to {call.vendor_id} failed")
            call.status = "failed"
        finally:
            _queue.remove(key)
            _save(run)


async def _run_one(run: DemoRun, call: DemoCall, audio_base: str) -> None:
    vendor = store.vendor(call.vendor_id)
    price = pricing(call.category, run.brief, seed=f"{run.id}:{call.vendor_id}")
    renegotiation = call.purpose == "renegotiation"
    with VendorBot(vendor, run.brief, price, audio_base, RECORDINGS / run.id, renegotiation=renegotiation) as bot:
        call.bot = bot
        call.status = "ringing"
        if renegotiation:
            status = await enquiries.renegotiate(run.event_id, run.brief, call.vendor_id)
        else:
            status = await enquiries.call_vendor(run.event_id, run.brief, call.vendor_id,
                                                 {"mode": "vendor_call", "vendor_id": call.vendor_id}, "enquiry")
        if bot.call_id:
            await asyncio.wait_for(bot.done.wait(), 30)
    call.call_id = bot.call_id
    call.recording = bot.recording
    call.vendor_lines = bot.said
    call.status = "done" if bot.call_id else "failed"
    logger.info(f"Demo run {run.id}: {vendor['name']} -> {status}")


def _call_view(run: DemoRun, call: DemoCall) -> dict[str, Any]:
    vendor = store.vendor(call.vendor_id)
    bot = call.bot
    if bot and bot.context is not None and call.status != "done":
        transcript = _transcript(bot.context)
        if bot.call_id and call.status == "ringing":
            call.status = "in_call"
    elif call.call_id and (record := store.call(call.call_id)):
        transcript = record["transcript"]
    else:
        transcript = []
    position = None
    key = f"{run.id}:{id(call)}"
    if call.status in ("waiting", "ringing") and key in _queue:
        position = _queue.index(key)
    return {
        "category": call.category,
        "service": catalog.CATEGORIES[call.category]["name"],
        "vendor": {"id": vendor["id"], "name": vendor["name"], "area": vendor["area"], "rating": vendor["rating"],
                   "language": prompts.LANGUAGE_NAMES.get(vendor["language"], vendor["language"])},
        "purpose": call.purpose,
        "status": call.status,
        "ahead": position,
        "transcript": [{"who": "Mohan (Bandobast)" if t["role"] == "bandobast" else vendor["name"], "text": t["text"]} for t in transcript],
        "recording": f"/demo/api/runs/{run.id}/recordings/{call.call_id}" if call.recording else None,
    }


def _quote_view(run: DemoRun, vendor_id: str, category: str) -> dict[str, Any] | None:
    quote = store.latest_quotes(run.event_id).get(vendor_id)
    if not quote:
        return None
    return {
        "available": quote.get("available"),
        "price": quote.get("price_amount"),
        "unit": quote.get("price_unit"),
        "opening_price": quote.get("initial_price_amount"),
        "transport": quote.get("transport_charge"),
        "extras": quote.get("extra_charges_amount"),
        "extras_notes": quote.get("extra_charges_notes"),
        "advance": quote.get("advance_amount"),
        "gst_included": quote.get("gst_included"),
        "gst_percent": quote.get("gst_percent") if quote.get("gst_included") is False else None,
        "extras_at_actuals": quote.get("extras_at_actuals") is True,
        "includes": quote.get("includes_notes"),
        "landed_cost": rules.landed_cost(quote, run.brief),
        "savings": rules.savings(quote, run.brief),
        "budget": (run.brief.get("budget") or {}).get(category),
        "needs_recheck": bool(quote.get("available") and price_safety.issues(quote, run.brief, category)),
        "negotiable": category in rules.NEGOTIABLE,
    }


def run_view(run: DemoRun) -> dict[str, Any]:
    services = []
    for category in run.brief["needs"]:
        calls = [c for c in run.calls if c.category == category]
        vendor_id = calls[-1].vendor_id
        busy = any(c.status in ("waiting", "ringing", "in_call") for c in calls)
        services.append({
            "category": category,
            "name": catalog.CATEGORIES[category]["name"],
            "vendor_id": vendor_id,
            "calls": [_call_view(run, c) for c in calls],
            "quote": None if busy else _quote_view(run, vendor_id, category),
            "busy": busy,
            "decision": run.decisions.get(vendor_id),
        })
    return {"id": run.id, "label": catalog.function_label(run.brief), "brief": run.brief, "services": services,
            "running": any(s["busy"] for s in services)}


def _save(run: DemoRun) -> None:
    """Runs outlive a server restart, so a shared /demo?run= link keeps working."""
    data = {"id": run.id, "event_id": run.event_id, "brief": run.brief, "decisions": run.decisions,
            "calls": [{"category": c.category, "vendor_id": c.vendor_id, "purpose": c.purpose, "status": c.status,
                       "call_id": c.call_id, "recording": c.recording.name if c.recording else None,
                       "vendor_lines": c.vendor_lines} for c in run.calls]}
    path = RECORDINGS / run.id / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


def _load(run_id: str) -> DemoRun | None:
    if not re.fullmatch(r"[\w-]{8,40}", run_id):
        return None
    try:
        data = json.loads((RECORDINGS / run_id / "run.json").read_text())
    except (OSError, ValueError):
        return None
    run = DemoRun(data["id"], data["event_id"], data["brief"], decisions=data["decisions"])
    for c in data["calls"]:
        # A call that was in progress when the server stopped did not finish.
        status = c["status"] if c["status"] in ("done", "failed") else "failed"
        recording = RECORDINGS / run_id / c["recording"] if c["recording"] else None
        run.calls.append(DemoCall(c["category"], c["vendor_id"], c["purpose"], status, None, c["call_id"], recording, c["vendor_lines"]))
    return run


def _run(run_id: str) -> DemoRun:
    run = _runs.get(run_id) or _load(run_id)
    if not run:
        raise HTTPException(404, "This demo run has expired. Please start a new one.")
    _runs[run.id] = run
    return run


def _audio_base(request: Request) -> str:
    server = request.scope.get("server") or ("127.0.0.1", int(os.environ.get("PORT", "8765")))
    return f"ws://127.0.0.1:{server[1]}"


# API


@router.get("")
async def page():
    return FileResponse(ROOT / "static" / "demo.html")


@router.get("/api/config")
async def config():
    daily, per_ip = _limits()
    usage = _usage()
    return {
        "functions": [{"key": k, "name": f["name"], "variants": f["variants"], "default_needs": f["default_needs"],
                       "optional": f["optional"]} for k, f in catalog.FUNCTIONS.items()],
        "services": [{"key": k, "name": c["name"], "details_hint": c["details"], "negotiable": k in rules.NEGOTIABLE,
                      "default_budget": _default_budget(k)} for k, c in catalog.CATEGORIES.items()],
        "areas": list(AREAS),
        "max_services": MAX_SERVICES,
        "calls_left_today": max(daily - usage["total"], 0),
        "busy": channels.CALL_LOCK.locked(),
        "queue": len(_queue),
    }


def _default_budget(category: str, guests: int = 40) -> int:
    from vendor_bot import DEFAULT_RATES
    rate = DEFAULT_RATES[category]
    return rate * guests if category == "caterer" else rate


@router.get("/api/shortlist")
async def shortlist(category: str, area: str):
    if category not in catalog.CATEGORIES or area not in AREAS:
        raise HTTPException(400, "Unknown service or area")
    lat, lon = AREAS[area]
    rows = rules.shortlist(category, store.vendors(), {"location": {"lat": lat, "lon": lon}})
    if not rows:  # far from every demo vendor: offer them all, nearest first
        rows = rules.shortlist(category, store.vendors(), {})
    out = []
    for row in rows:
        v = store.vendor(row["vendor_id"])
        out.append({"id": v["id"], "name": v["name"], "area": v["area"], "rating": v["rating"],
                    "language": prompts.LANGUAGE_NAMES.get(v["language"], v["language"]), "distance_km": row["distance_km"]})
    return {"vendors": out}


class FamilyRun(BaseModel):
    function_type: str
    function_variant: str
    family_name: str = Field(max_length=40)
    area: str
    date: str
    start_time: str
    guests: int = Field(ge=5, le=1000)
    cuisine: str = Field(default="", max_length=120)
    services: dict[str, str] = Field(description="category -> vendor id")
    budget: dict[str, int] = {}
    details: dict[str, str] = {}


@router.post("/api/family-runs")
async def start_family_run(req: FamilyRun, request: Request):
    if not req.services or len(req.services) > MAX_SERVICES:
        raise HTTPException(400, f"Pick one or two services (at most {MAX_SERVICES})")
    if req.area not in AREAS:
        raise HTTPException(400, "Pick an area from the list")
    for category, vendor_id in req.services.items():
        try:
            if store.vendor(vendor_id)["category"] != category:
                raise KeyError(vendor_id)
        except KeyError:
            raise HTTPException(400, f"Unknown {category} vendor")
    needs = list(req.services)
    details = {k: v.strip()[:200] for k, v in req.details.items() if k in needs and v.strip()}
    lat, lon = AREAS[req.area]
    brief = {
        "function_type": req.function_type, "function_variant": req.function_variant,
        "family_name": req.family_name.strip(), "contact_name": f"{req.family_name.strip()} family",
        "family_language": "hi-IN", "city": "Bengaluru", "area": req.area,
        "location": {"lat": lat, "lon": lon, "label": f"{req.area}, Bengaluru"},
        "date": req.date, "start_time": req.start_time, "guests": req.guests,
        "cuisine": req.cuisine.strip() or "vegetarian", "rituals": "",
        "needs": needs,
        "budget": {k: int(v) for k, v in req.budget.items() if k in needs and isinstance(v, int) and 0 < v <= 10_000_000},
        "details": details,
    }
    problems = brief_problems({**brief, **{f"details_{k}": v for k, v in details.items()}})
    if problems:
        raise HTTPException(400, "Please fix: " + "; ".join(problems))
    spend(request, len(needs))
    event_id = store.save_event(brief)
    run = DemoRun(secrets.token_urlsafe(12), event_id, brief)
    for category, vendor_id in req.services.items():
        store.set_enquiry(event_id, vendor_id, category, "shortlisted", channel="app")
        run.calls.append(DemoCall(category, vendor_id))
    _runs[run.id] = run
    _save(run)
    run.task = app_calls.spawn(_run_calls(run, list(run.calls), _audio_base(request)))
    logger.info(f"Demo run {run.id} (event {event_id}) from {visitor(request)}: {needs}")
    return {"run_id": run.id}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    return run_view(_run(run_id))


@router.get("/api/runs/{run_id}/recordings/{call_id}")
async def recording(run_id: str, call_id: str):
    run = _run(run_id)
    call = next((c for c in run.calls if c.call_id == call_id and c.recording), None)
    if not call or not call.recording.exists():
        raise HTTPException(404, "No recording for this call")
    return FileResponse(call.recording, media_type="audio/mpeg" if call.recording.suffix == ".mp3" else "audio/wav")


@router.post("/api/runs/{run_id}/vendors/{vendor_id}/{action}")
async def decide(run_id: str, vendor_id: str, action: str, request: Request):
    run = _run(run_id)
    call = next((c for c in reversed(run.calls) if c.vendor_id == vendor_id), None)
    if not call:
        raise HTTPException(404, "That vendor isn't part of this demo")
    if any(c.status in ("waiting", "ringing", "in_call") for c in run.calls if c.vendor_id == vendor_id):
        raise HTTPException(409, "Bandobast is still on this call")
    quote = store.latest_quotes(run.event_id).get(vendor_id)
    if not quote or not quote.get("available"):
        raise HTTPException(400, "There is no quote from this vendor to act on")
    category = call.category
    if action == "approve":
        # The app would now call the vendor to confirm these exact terms and schedule check-ins.
        run.decisions[vendor_id] = "approved"
        store.set_enquiry(run.event_id, vendor_id, category, "approved", note="Approved in the web demo")
    elif action == "reject":
        run.decisions[vendor_id] = "rejected"
        store.set_enquiry(run.event_id, vendor_id, category, "rejected")
    elif action == "renegotiate":
        if category not in rules.NEGOTIABLE:
            raise HTTPException(400, f"Bandobast doesn't bargain with a {catalog.CATEGORIES[category]['name'].lower()}")
        if sum(c.purpose == "renegotiation" for c in run.calls if c.vendor_id == vendor_id) >= 1:
            raise HTTPException(400, "Bandobast has already called back once; this is their best price")
        spend(request, 1)
        run.decisions.pop(vendor_id, None)
        again = DemoCall(category, vendor_id, purpose="renegotiation")
        run.calls.append(again)
        app_calls.spawn(_run_calls(run, [again], _audio_base(request)))
    else:
        raise HTTPException(400, "Unknown action")
    _save(run)
    return run_view(run)


# Vendor side: the visitor is the vendor


class VendorCall(BaseModel):
    vendor_id: str


@router.get("/api/vendor-personas")
async def vendor_personas():
    """One vendor per language and service, for the visitor to play."""
    out = []
    for v in store.vendors():
        if v["category"] in ("caterer", "decorator", "photographer", "cake"):
            out.append({"id": v["id"], "name": v["name"], "service": catalog.CATEGORIES[v["category"]]["name"],
                        "category": v["category"], "language": prompts.LANGUAGE_NAMES.get(v["language"], v["language"]),
                        "area": v["area"]})
    return {"vendors": out}


def _request_brief(vendor: dict[str, Any]) -> dict[str, Any]:
    """A synthetic family request for this vendor's service, a week from today."""
    sample = prompts.SAMPLE_NAAMKARAN if vendor["category"] in ("cake", "photographer", "tent_sound") else prompts.SAMPLE_BRIEF
    category = vendor["category"]
    lat, lon = AREAS.get(sample["area"], AREAS["HSR Layout"])
    return {
        **sample,
        "date": (date.today() + timedelta(days=7)).isoformat(),
        "location": {"lat": lat, "lon": lon, "label": f"{sample['area']}, Bengaluru"},
        "needs": [category],
        "budget": {category: (sample.get("budget") or {}).get(category) or _default_budget(category, sample["guests"])},
        "details": {k: v for k, v in (sample.get("details") or {}).items() if k == category},
    }


@router.get("/api/vendor-request")
async def vendor_request(vendor_id: str):
    try:
        vendor = store.vendor(vendor_id)
    except KeyError:
        raise HTTPException(404, "Unknown vendor")
    brief = _request_brief(vendor)
    return {"label": catalog.function_label(brief), "family": f"{brief['family_name']} family", "area": brief["area"],
            "date": brief["date"], "start_time": brief["start_time"], "guests": brief["guests"],
            "cuisine": brief.get("cuisine"), "details": brief["details"].get(vendor["category"]),
            "service": catalog.CATEGORIES[vendor["category"]]["name"],
            "language": prompts.LANGUAGE_NAMES.get(vendor["language"], vendor["language"])}


@router.post("/api/vendor-calls")
async def accept_request(req: VendorCall, request: Request):
    """The visitor accepted the request: start a Bandobast enquiry call to their browser."""
    try:
        vendor = store.vendor(req.vendor_id)
    except KeyError:
        raise HTTPException(404, "Unknown vendor")
    if channels.CALL_LOCK.locked() or app_calls.busy(vendor["id"]):
        raise HTTPException(409, "Bandobast is on another demo call right now (it takes one call at a time). Please try again in a couple of minutes.")
    spend(request, 1)
    await channels.CALL_LOCK.acquire()
    brief = _request_brief(vendor)
    event_id = store.save_event(brief)
    store.set_enquiry(event_id, vendor["id"], vendor["category"], "in_call", channel="app")
    s = app_calls.AudioSession("WD" + secrets.token_hex(6), (vendor["id"],), "assistant",
                               {"mode": "vendor_call", "vendor_id": vendor["id"], "event_id": event_id}, owns_lock=True)
    app_calls._sessions[s.id] = s
    s.expiry = app_calls.spawn(app_calls.expire(s.id, channels.RING_SECONDS))
    _vendor_calls[s.id] = event_id
    logger.info(f"Demo vendor call {s.id} as {vendor['id']} from {visitor(request)}")
    return app_calls.ticket(s, vendor["id"])


_vendor_calls: dict[str, int] = {}


@router.get("/api/vendor-calls/{call_id}")
async def vendor_call_result(call_id: str):
    event_id = _vendor_calls.get(call_id)
    if event_id is None:
        raise HTTPException(404, "Unknown call")
    record = store.call(call_id)
    if not record:
        return {"done": False}
    _, brief = store.event(event_id)
    vendor_id = record["vendor_id"]
    quote = store.latest_quotes(event_id).get(vendor_id)
    category = store.vendor(vendor_id)["category"]
    view = None
    if quote:
        view = {"available": quote.get("available"), "price": quote.get("price_amount"), "unit": quote.get("price_unit"),
                "opening_price": quote.get("initial_price_amount"), "transport": quote.get("transport_charge"),
                "advance": quote.get("advance_amount"), "landed_cost": rules.landed_cost(quote, brief),
                "budget": (brief.get("budget") or {}).get(category)}
    return {"done": True, "transcript": [{"who": "Mohan (Bandobast)" if t["role"] == "bandobast" else "You", "text": t["text"]}
                                         for t in record["transcript"]], "quote": view}
