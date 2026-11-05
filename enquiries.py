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
import confirmations
import confirmation_rules
import backups

_tasks: set[asyncio.Task] = set()
_confirmation_queued: set[int] = set()


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

    outcome = await channels.call(vendor, {**session, "event_id": event_id}, caller_info(brief, vendor, purpose))
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
    session = {"mode": "checkin", "vendor_id": checkin["vendor_id"], "checkin_id": checkin_id, "event_id": event_id}
    vendor = store.vendor(checkin["vendor_id"])
    outcome = await channels.call(vendor, session, caller_info(brief, vendor, "checkin"))
    if outcome["result"] != "answered":
        workflow.mark_unreachable(checkin_id)
    return outcome["result"]


async def reconfirm(event_id: int, brief: dict[str, Any], vendor_id: str) -> str:
    """A price recheck preserves approved/standby status and does not silently change bookings."""
    vendor = store.vendor(vendor_id)
    old_status = (store.enquiry(event_id, vendor_id) or {}).get("status", "quoted")
    store.set_enquiry(event_id, vendor_id, vendor["category"], "reconfirming")
    try:
        outcome = await channels.call(vendor, {"mode": "vendor_call", "vendor_id": vendor_id,
            "event_id": event_id, "reconfirm": "1"}, caller_info(brief, vendor, "reconfirmation"))
        return outcome["result"]
    finally:
        quote = store.latest_quotes(event_id).get(vendor_id)
        bookings = [b for b in store.bookings(event_id) if b["vendor_id"] == vendor_id]
        primary = next((b for b in bookings if b['role'] == 'primary'), None)
        backup = next((b for b in bookings if b['role'] == 'backup'), None)
        status = primary['status'] if primary and primary['status'] in ('awaiting_confirmation', 'confirming', 'confirmation_failed', 'awaiting_changes', 'replacement_pending') else "confirmed" if any(b["role"] == "primary" and b["status"] == "active" for b in bookings) else (
            backup['status'] if backup and backup['status'] in confirmation_rules.BACKUP_RESERVED else "declined" if quote and quote.get("available") is False else "quoted" if quote else old_status)
        store.set_enquiry(event_id, vendor_id, vendor["category"], status)
        if primary and primary['status'] in ('awaiting_confirmation', 'confirmation_failed', 'awaiting_changes'):
            if not any(u['vendor_id'] == vendor_id and u['status'] == 'pending' for u in store.updates(event_id)):
                try:
                    confirmation_id = confirmations.request(event_id, vendor['category'])
                    spawn(confirmation_call(confirmation_id))
                except ValueError:
                    pass  # unresolved prices stay blocked; never auto-confirm them
        elif backup and backup['status'] in confirmation_rules.BACKUP_RESERVED:
            try:
                confirmation_id = backups.retry(event_id, vendor['category'])
                if confirmation_id:
                    queue_confirmation(confirmation_id)
            except ValueError:
                pass


async def confirmation_call(confirmation_id: int) -> str:
    """One confirmation attempt; its persisted ID prevents duplicate calls or stale successes."""
    initial = store.confirmation(confirmation_id)
    await stop_superseded_confirmation_calls(initial['event_id'], initial['category'])
    try:
        record = confirmations.claim(confirmation_id)
    except (ValueError, LookupError) as error:
        confirmations.finish(confirmation_id, 'failed', str(error))
        return 'superseded'
    if record is None:
        return 'already_started'
    event_id, brief = store.event(record['event_id'])
    vendor = store.vendor(record['vendor_id'])
    try:
        purpose = 'standby_confirmation' if record['purpose'] == 'standby' else 'backup_activation' if record['purpose'] == 'replacement' else 'booking_confirmation'
        outcome = await channels.call(vendor, {'mode': 'confirmation', 'confirmation_id': confirmation_id,
            'event_id': event_id, 'vendor_id': vendor['id']}, caller_info(brief, vendor, purpose))
        latest = store.confirmation(confirmation_id)
        if latest['status'] == 'calling':
            status = 'no_answer' if outcome['result'] in ('no_answer', 'unreachable') else 'declined' if outcome['result'] == 'declined' else 'failed'
            # Declining a ring is not a spoken refusal of the booking.
            if outcome['result'] == 'declined':
                status = 'no_answer'
            confirmations.finish(confirmation_id, status, 'No answer' if status == 'no_answer' else 'Call ended without booking confirmation', call_id=outcome.get('call_id'))
        queue_pending_confirmations(event_id)
        return store.confirmation(confirmation_id)['status']
    except Exception:
        confirmations.finish(confirmation_id, 'failed', 'Confirmation call failed')
        logger.error('Booking confirmation {} failed', confirmation_id)
        return 'failed'


def queue_confirmation(confirmation_id: int) -> None:
    if confirmation_id in _confirmation_queued:
        return
    _confirmation_queued.add(confirmation_id)
    async def run():
        try:
            await confirmation_call(confirmation_id)
        finally:
            _confirmation_queued.discard(confirmation_id)
    spawn(run())


def queue_pending_confirmations(event_id: int) -> None:
    for record in store.confirmations(event_id):
        latest = store.latest_confirmation(event_id, record['category'], record['role'])
        if record['status'] == 'pending' and latest and latest['id'] == record['id']:
            queue_confirmation(record['id'])


async def stop_superseded_confirmation_calls(event_id: int, category: str) -> None:
    """Only close superseded confirmation calls, never unrelated enquiries or direct calls."""
    import app_calls
    stale_ids = {c['id'] for c in store.confirmations(event_id) if c['category'] == category and c['status'] == 'superseded'}
    for pending in list(channels._calls.values()):
        if pending.session.get('mode') == 'confirmation' and pending.session.get('confirmation_id') in stale_ids:
            pending.declined.set()
            await app_calls.finish(pending.call_id, 'Booking confirmation was superseded')
