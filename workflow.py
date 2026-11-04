"""Domain actions shared by the conversations and the dashboard API.

Every state change that matters (booking, switching to the backup, alerting the family)
goes through here, so the rules in rules.py are applied the same way from every entry point.
"""

from typing import Any

import rules
import store


def require_event() -> tuple[int, dict[str, Any]]:
    event = store.current_event()
    if not event:
        raise LookupError("No event yet: run the family intake or load the sample brief first")
    return event


def competitor_quotes(vendor_id: str) -> list[dict[str, Any]]:
    """Quotes already collected from other vendors offering the same service."""
    event_id, _ = require_event()
    category = store.vendor(vendor_id)["category"]
    return [
        q
        for v_id, q in store.latest_quotes(event_id).items()
        if v_id != vendor_id and store.vendor(v_id)["category"] == category
    ]


def rejected(event_id: int) -> set[str]:
    return {e["vendor_id"] for e in store.enquiries(event_id) if e["status"] == "rejected"}


def comparison(event_id: int, brief: dict[str, Any]) -> dict[str, Any]:
    """Ranked quotes per category, with the rules' top pick and backup. Quotes the family rejected are left out."""
    out_of_the_running = rejected(event_id)
    quotes = {v: q for v, q in store.latest_quotes(event_id).items() if v not in out_of_the_running}
    by_id = {v["id"]: v for v in store.vendors()}
    out = {}
    for category in rules.CATEGORIES:
        ranked = rules.rank_category(category, quotes, by_id, brief)
        top, backup = rules.pick(ranked)
        out[category] = {"ranked": ranked, "top": top, "backup": backup}
    return out


def book(category: str, vendor_id: str | None = None) -> dict[str, Any]:
    """Family approved: book their choice (or the top pick), keep the next best on standby, schedule check-ins."""
    event_id, brief = require_event()
    ranked = comparison(event_id, brief)[category]["ranked"]
    pending_rates = {u["vendor_id"] for u in store.updates(event_id) if u["kind"] == "price" and u["status"] == "pending"}
    if vendor_id in pending_rates:
        raise ValueError("Review this vendor's pending price revision before booking")
    bookable = [r["vendor_id"] for r in ranked if r["bookable"] and r["vendor_id"] not in pending_rates]
    if vendor_id and vendor_id not in bookable:
        raise ValueError(f"{vendor_id} has no bookable {category} quote")
    primary = vendor_id or (bookable[0] if bookable else None)
    if not primary:
        raise ValueError(f"No bookable {category} quote yet")
    backup_id = next((v for v in bookable if v != primary), None)
    result = {"top": primary, "backup": backup_id}
    if any(b["category"] == category and b["role"] == "primary" and b["status"] == "active" for b in store.bookings(event_id)):
        raise ValueError(f"A {category} is already booked")
    store.set_booking(event_id, category, "primary", result["top"], "active")
    store.set_enquiry(event_id, primary, category, "approved")
    if result["backup"]:
        store.set_booking(event_id, category, "backup", result["backup"], "standby")
        store.set_enquiry(event_id, backup_id, category, "standby")
    store.add_checkins(event_id, result["top"], category, rules.checkin_schedule(brief, category))
    primary = store.vendor(result["top"])
    backup = store.vendor(result["backup"]) if result["backup"] else None
    store.add_alert(
        event_id,
        "info",
        f"Booked {primary['name']} as {category}"
        + (f"; {backup['name']} is on standby as backup." if backup else "; no backup available yet."),
    )
    return {"primary": result["top"], "backup": result["backup"]}


def reject(vendor_id: str) -> None:
    """The family doesn't want this vendor: drop them from the comparison."""
    event_id, _ = require_event()
    if any(b["vendor_id"] == vendor_id and b["status"] in ("active", "standby") for b in store.bookings(event_id)):
        raise ValueError("That vendor is booked; it can't be rejected now")
    store.set_enquiry(event_id, vendor_id, store.vendor(vendor_id)["category"], "rejected")


def record_checkin(checkin_id: int, status: str, eta: str | None, notes: str | None) -> dict[str, str] | None:
    """Store a check-in result, then let the rules decide whether to switch to the backup."""
    checkin = store.checkin(checkin_id)
    store.finish_checkin(checkin_id, status, eta, notes)
    event_id, brief = store.event(checkin["event_id"])
    category = checkin["category"]
    history = [
        c
        for c in store.checkins(event_id)
        if c["category"] == category and c["vendor_id"] == checkin["vendor_id"]
    ]
    risk = rules.assess_risk(history, brief, category)
    if risk:
        _act_on_risk(event_id, brief, category, checkin["vendor_id"], risk)
    return risk


def _act_on_risk(
    event_id: int, brief: dict[str, Any], category: str, vendor_id: str, risk: dict[str, str]
) -> None:
    vendor = store.vendor(vendor_id)
    if risk["action"] == "warn":
        store.add_alert(event_id, "warn", f"{vendor['name']} ({category}): {risk['reason']}")
        return

    booking = {b["role"]: b for b in store.bookings(event_id) if b["category"] == category}
    backup = booking.get("backup")
    if not backup or backup["status"] != "standby":
        store.add_alert(
            event_id,
            "critical",
            f"{vendor['name']} ({category}): {risk['reason']}. No backup on standby; call the family now.",
        )
        return

    store.set_booking(event_id, category, "primary", backup["vendor_id"], "active")
    store.set_booking(event_id, category, "backup", vendor_id, "replaced")
    store.set_enquiry(event_id, vendor_id, category, "replaced")
    store.set_enquiry(event_id, backup["vendor_id"], category, "approved")
    remaining = [
        (label, due)
        for label, due in rules.checkin_schedule(brief, category)
        if not label.startswith("D-3")
    ]
    store.add_checkins(event_id, backup["vendor_id"], category, remaining)
    new_vendor = store.vendor(backup["vendor_id"])
    store.add_alert(
        event_id,
        "critical",
        f"{vendor['name']} ({category}): {risk['reason']}. Switched to backup {new_vendor['name']}; "
        "Please contact the new vendor to confirm the arrangement.",
    )


def mark_unreachable(checkin_id: int) -> dict[str, str] | None:
    """The phone wasn't answered. In browser testing you trigger this from the dashboard."""
    return record_checkin(checkin_id, "unreachable", None, "No answer")


def record_vendor_update(event_id: int, vendor_id: str, data: dict[str, Any], vendor_said: str) -> int:
    """Price changes wait for family approval; operational risks use the same backup rules."""
    _, brief = store.event(event_id)
    vendor = store.vendor(vendor_id)
    quote = store.latest_quotes(event_id).get(vendor_id)
    if problems := rules.update_problems(data, quote, vendor_said):
        raise ValueError("; ".join(problems))
    kind = data["kind"]
    payload = {"notes": data["notes"].strip()}
    if kind == "price":
        # Only the base price changes; extras and advance must not be guessed by the model.
        payload["quote"] = {**quote, "price_amount": data["price_amount"],
                            "unverified": [f for f in quote.get("unverified", []) if f != "price_amount"]}
        payload["previous_price"] = quote["price_amount"]
        for old in store.updates(event_id):
            if old["vendor_id"] == vendor_id and old["kind"] == "price" and old["status"] == "pending":
                store.resolve_update(old["id"], "superseded")
    elif kind == "delayed":
        payload["eta"] = data["eta"]
    update_id = store.save_update(event_id, vendor_id, kind, payload, "pending" if kind == "price" else "received")
    store.add_alert(event_id, "warn" if kind != "note" else "info", f"{vendor['name']}: {payload['notes']}"
                    + (" — revised price needs your approval." if kind == "price" else ""))
    active = any(b["vendor_id"] == vendor_id and b["role"] == "primary" and b["status"] == "active"
                 for b in store.bookings(event_id))
    if active and kind in ("cancelled", "delayed"):
        risk = rules.assess_risk([{"status": kind, "eta": data.get("eta"), "label": "Vendor callback"}], brief, vendor["category"])
        if risk:
            _act_on_risk(event_id, brief, vendor["category"], vendor_id, risk)
        if kind == "cancelled" and quote:
            store.save_quote(event_id, vendor_id, None, {**quote, "available": False})
            # No standby may exist: mark the cancelled primary inactive so the family
            # can explicitly approve a replacement instead of getting stuck booked.
            for b in store.bookings(event_id):
                if b["role"] == "primary" and b["vendor_id"] == vendor_id and b["status"] == "active":
                    store.set_booking(event_id, b["category"], "primary", vendor_id, "cancelled")
                    store.set_enquiry(event_id, vendor_id, b["category"], "cancelled")
    elif kind == "cancelled":
        if quote:
            store.save_quote(event_id, vendor_id, None, {**quote, "available": False})
        store.set_enquiry(event_id, vendor_id, vendor["category"], "declined", note=payload["notes"])
        for b in store.bookings(event_id):
            if b["vendor_id"] == vendor_id and b["role"] == "backup" and b["status"] == "standby":
                store.set_booking(event_id, b["category"], "backup", vendor_id, "cancelled")
    return update_id


def decide_update(event_id: int, update_id: int, approve: bool) -> None:
    update = store.update(update_id)
    if update["event_id"] != event_id or update["kind"] != "price" or update["status"] != "pending":
        raise ValueError("This price revision is no longer pending for this function")
    vendor_id = update["vendor_id"]
    vendor = store.vendor(vendor_id)
    if approve:
        current = store.latest_quotes(event_id).get(vendor_id)
        if not current or not current.get("available"):
            raise ValueError("This vendor is no longer available")
        # Retain current operational terms, changing only the explicitly approved base price.
        store.save_quote(event_id, vendor_id, None, {**current, "price_amount": update["data"]["quote"]["price_amount"],
                         "unverified": [f for f in current.get("unverified", []) if f != "price_amount"]})
    store.resolve_update(update_id, "approved" if approve else "rejected")
    store.add_alert(event_id, "info", f"{'Approved' if approve else 'Rejected'} revised price from {vendor['name']}.")
