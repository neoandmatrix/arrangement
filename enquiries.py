"""Running a function's enquiries: find vendors, shortlist, call them one by one, record the outcome.

Started when the family submits a function from the app. Each step updates the enquiries
table, which the app polls to show progress ("calling Sweet Tooth Bakers…", "quote received").
"""

import asyncio
from typing import Any

from loguru import logger

import catalog
import channels
import discovery
import rules
import store
import workflow

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def caller_info(brief: dict[str, Any], vendor: dict[str, Any], purpose: str) -> dict[str, Any]:
    """What the vendor's ringing screen shows."""
    return {
        "family": f"{brief.get('family_name', '')} family",
        "function": catalog.function_label(brief),
        "date": brief.get("date", ""),
        "area": brief.get("area", ""),
        "category": vendor["category"],
        "purpose": purpose,  # enquiry | renegotiation | checkin
    }


async def run_event(event_id: int, brief: dict[str, Any]) -> None:
    """Discover and shortlist for every needed service, then call the shortlisted vendors."""
    queue: list[str] = []
    for category in brief.get("needs", []):
        try:
            store.save_discovered(event_id, category, await discovery.discover(category, brief))
        except Exception:
            logger.exception(f"Discovery for {category} failed")
        for row in rules.shortlist(category, store.vendors(), brief):
            vendor = store.vendor(row["vendor_id"])
            note = f"{row['distance_km']} km away" if row["distance_km"] is not None else None
            store.set_enquiry(event_id, vendor["id"], category, "shortlisted", channel=channels.channel_for(vendor), note=note)
            queue.append(vendor["id"])

    # Vendors on the app first, so answers come in while the unreachable ones are marked.
    queue.sort(key=lambda v: channels.channel_for(store.vendor(v)) != "app")
    for vendor_id in queue:
        if store.current_event()[0] != event_id:
            logger.info(f"Event {event_id} replaced by a newer one; stopping its calls")
            return
        await call_vendor(event_id, brief, vendor_id, {"mode": "vendor_call", "vendor_id": vendor_id}, "enquiry")


async def call_vendor(
    event_id: int, brief: dict[str, Any], vendor_id: str, session: dict[str, Any], purpose: str
) -> str:
    """One call, with the enquiry status kept up to date. Returns the final status."""
    vendor = store.vendor(vendor_id)
    category = vendor["category"]
    channel = channels.channel_for(vendor)
    store.set_enquiry(event_id, vendor_id, category, "ringing" if channel != "none" else "no_answer", channel=channel)
    if channel == "none":
        store.set_enquiry(event_id, vendor_id, category, "no_answer", note="No app and no phone line")
        return "no_answer"

    outcome = await channels.call(vendor, session, caller_info(brief, vendor, purpose))
    call_id = outcome["call_id"]
    if outcome["result"] != "answered":
        status = {"declined": "declined", "no_answer": "no_answer"}.get(outcome["result"], "failed")
        store.set_enquiry(event_id, vendor_id, category, status, call_id=call_id, note=outcome["result"].replace("_", " "))
        return status
    if purpose == "checkin":
        status = "checked_in"
    elif quote := store.quote_from_call(call_id):
        status = "quoted" if quote.get("available") else "declined"
    else:
        status = "callback"  # they were busy, or the call ended before a quote
    record = store.call(call_id) or {}
    note = record.get("end_reason") or ("Call ended before a quote" if status == "callback" else None)
    store.set_enquiry(event_id, vendor_id, category, status, call_id=call_id, note=note)
    return status


async def renegotiate(event_id: int, brief: dict[str, Any], vendor_id: str) -> str:
    vendor = store.vendor(vendor_id)
    store.set_enquiry(event_id, vendor_id, vendor["category"], "renegotiating")
    session = {"mode": "vendor_call", "vendor_id": vendor_id, "renegotiate": "1"}
    status = await call_vendor(event_id, brief, vendor_id, session, "renegotiation")
    if status != "quoted":
        # The earlier quote still stands.
        store.set_enquiry(event_id, vendor_id, vendor["category"], "quoted", note=f"Callback: {status.replace('_', ' ')}")
    return status


async def checkin_call(event_id: int, brief: dict[str, Any], checkin_id: int) -> str:
    checkin = store.checkin(checkin_id)
    session = {"mode": "checkin", "vendor_id": checkin["vendor_id"], "checkin_id": checkin_id}
    vendor = store.vendor(checkin["vendor_id"])
    outcome = await channels.call(vendor, session, caller_info(brief, vendor, "checkin"))
    if outcome["result"] != "answered":
        workflow.mark_unreachable(checkin_id)
    return outcome["result"]
