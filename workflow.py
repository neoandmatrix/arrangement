"""Domain actions shared by the conversations and the dashboard API.

Every state change that matters (booking, switching to the backup, alerting the family)
goes through here, so the rules in rules.py are applied the same way from every entry point.
"""

from typing import Any
import uuid

import rules
import price_safety
import store
import confirmation_rules
import confirmations
import backups


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
    """Family approved: reserve their choice and create an immutable vendor-confirmation request."""
    event_id, brief = require_event()
    ranked = comparison(event_id, brief)[category]["ranked"]
    pending_rates = {u["vendor_id"] for u in store.updates(event_id) if u["kind"] in ("price", "quote") and u["status"] == "pending"}
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
    if any(b["category"] == category and b["role"] == "primary" and b["status"] in confirmation_rules.RESERVED for b in store.bookings(event_id)):
        raise ValueError(f"A {category} is already booked")
    old_backup = next((b for b in store.bookings(event_id) if b['category'] == category and b['role'] == 'backup'), None)
    if old_backup and old_backup['status'] in confirmation_rules.BACKUP_RESERVED and old_backup['vendor_id'] not in (primary, backup_id):
        store.set_enquiry(event_id, old_backup['vendor_id'], category, 'quoted', note='Backup selection changed')
    store.set_booking(event_id, category, "primary", result["top"], "awaiting_confirmation")
    store.set_enquiry(event_id, primary, category, "awaiting_confirmation")
    if result["backup"]:
        store.set_booking(event_id, category, "backup", result["backup"], "awaiting_backup_confirmation")
        store.set_enquiry(event_id, backup_id, category, "awaiting_backup_confirmation")
    elif old_backup:
        store.set_booking(event_id, category, 'backup', old_backup['vendor_id'], 'cancelled')
    primary = store.vendor(result["top"])
    backup = store.vendor(result["backup"]) if result["backup"] else None
    confirmation_id = confirmations.request(event_id, category)
    return {"primary": result["top"], "backup": result["backup"], "confirmation_id": confirmation_id}


def reject(vendor_id: str) -> None:
    """The family doesn't want this vendor: drop them from the comparison."""
    event_id, _ = require_event()
    if any(b["vendor_id"] == vendor_id and b["status"] in (*confirmation_rules.RESERVED, *confirmation_rules.BACKUP_RESERVED) for b in store.bookings(event_id)):
        raise ValueError("That vendor is booked; it can't be rejected now")
    store.set_enquiry(event_id, vendor_id, store.vendor(vendor_id)["category"], "rejected")


def record_checkin(checkin_id: int, status: str, eta: str | None, notes: str | None) -> dict[str, str] | None:
    """Store a check-in result, then let the rules decide whether to switch to the backup."""
    checkin = store.checkin(checkin_id)
    primary = confirmations.primary(checkin['event_id'], checkin['category'])
    if not primary or primary['vendor_id'] != checkin['vendor_id'] or primary['status'] != 'active':
        raise ValueError('Check-ins only apply to the currently active booked vendor')
    confirmed = confirmations.summary(checkin['event_id'], checkin['category'])
    if not confirmed or confirmed['status'] != 'confirmed':
        raise ValueError('Vendor booking confirmation is required before check-ins')
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

    backups.begin_replacement(event_id, category, vendor_id, risk['reason'])


def mark_unreachable(checkin_id: int) -> dict[str, str] | None:
    """The phone wasn't answered. In browser testing you trigger this from the dashboard."""
    return record_checkin(checkin_id, "unreachable", None, "No answer")


def record_vendor_update(event_id: int, vendor_id: str, data: dict[str, Any], vendor_said: str | list[dict[str, str]], *, call_id: str | None = None) -> int:
    """Price changes wait for family approval; operational risks use the same backup rules."""
    _, brief = store.event(event_id)
    vendor = store.vendor(vendor_id)
    quote = store.latest_quotes(event_id).get(vendor_id)
    dialogue = [{"role": "user", "text": vendor_said}] if isinstance(vendor_said, str) else vendor_said
    said = " ".join(m["text"] for m in dialogue if m["role"] in ("user", "vendor_or_family"))
    if problems := rules.update_problems(data, quote, said):
        raise ValueError("; ".join(problems))
    kind = data["kind"]
    payload = {"notes": data["notes"].strip()}
    if kind == "price":
        # Only the base price changes; extras and advance must not be guessed by the model.
        payload["quote"] = {**quote, "price_amount": data["price_amount"],
                            "unverified": [f for f in quote.get("unverified", []) if f != "price_amount"],
                            "price_change_from": quote["price_amount"]}
        payload["quote"]["amount_checks"] = price_safety.audit(payload["quote"], dialogue, vendor["category"],
            call_id=call_id or f"update-{uuid.uuid4().hex}", previous=quote)
        price_issues = [p for p in price_safety.issues(payload["quote"], brief, vendor["category"]) if p["field"] == "price_amount"]
        if price_issues:
            raise ValueError("; ".join(p["reason"] for p in price_issues))
        payload["flags"] = rules.quote_flags(payload["quote"], vendor, brief)
        payload["previous_price"] = quote["price_amount"]
        _supersede_prices(event_id, vendor_id)
    elif kind == "delayed":
        payload["eta"] = data["eta"]
    update_id = store.save_update(event_id, vendor_id, kind, payload, "pending" if kind == "price" else "received")
    if kind == 'price':
        confirmations.hold(event_id, vendor_id, 'Vendor proposed a revised price; family review and a new confirmation are required')
    if kind == 'cancelled':
        primary = confirmations.primary(event_id, vendor['category'])
        if primary and primary['vendor_id'] == vendor_id and primary['status'] in confirmation_rules.RESERVED and primary['status'] != 'active':
            latest = store.latest_confirmation(event_id, vendor['category'])
            if latest and latest['status'] in ('pending', 'calling'):
                confirmations.finish(latest['id'], 'declined', 'Vendor cancelled before booking confirmation')
            else:
                store.set_booking(event_id, vendor['category'], 'primary', vendor_id, 'declined')
                store.set_enquiry(event_id, vendor_id, vendor['category'], 'declined')
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
            store.clear_vendor_pending_checkins(event_id, vendor['category'], vendor_id)
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
            if b["vendor_id"] == vendor_id and b["role"] == "backup" and b["status"] in confirmation_rules.BACKUP_RESERVED:
                record = store.latest_confirmation(event_id, vendor['category'], 'backup')
                if record and record['status'] in ('pending', 'calling'):
                    confirmations.finish(record['id'], 'declined', payload['notes'], call_id=call_id)
                store.set_booking(event_id, b["category"], "backup", vendor_id, "cancelled")
    return update_id


def decide_update(event_id: int, update_id: int, approve: bool) -> dict[str, Any]:
    update = store.update(update_id)
    if update['kind'] == 'backup_activation':
        return {'confirmation_id': backups.decide_activation(event_id, update_id, approve)}
    if update["event_id"] != event_id or update["kind"] not in ("price", "quote", "confirmation_change") or update["status"] != "pending":
        raise ValueError("This price revision is no longer pending for this function")
    vendor_id = update["vendor_id"]
    vendor = store.vendor(vendor_id)
    if update['kind'] == 'confirmation_change' and approve:
        raise ValueError('A date/time/venue change cannot be accepted without rescheduling the function. Reject to retry original terms, or cancel this selection.')
    if approve:
        current = store.latest_quotes(event_id).get(vendor_id)
        if not current or not current.get("available"):
            raise ValueError("This vendor is no longer available")
        proposed = update["data"]["quote"]
        if update["kind"] == "quote":
            if confirmation_rules.quote_terms(current) != confirmation_rules.quote_terms(update["data"]["previous_quote"]):
                raise ValueError("The quote changed since this correction; recheck it again")
            candidate = proposed
        else:
            if current["price_amount"] != update["data"]["previous_price"]:
                raise ValueError("The price changed since this revision; recheck it again")
            # Price approval must not launder stale/unverified transport, extras or advance.
            fields = {**current.get("amount_checks", {}).get("fields", {}),
                      "price_amount": proposed.get("amount_checks", {}).get("fields", {}).get("price_amount", {})}
            candidate = {**current, "price_amount": proposed["price_amount"], "price_change_from": update["data"]["previous_price"],
                         "unverified": [f for f in current.get("unverified", []) if f != "price_amount"],
                         "amount_checks": {"version": price_safety.VERSION, "category": vendor["category"], "fields": fields,
                                           "gst_statement": current.get("amount_checks", {}).get("gst_statement")}}
        _, brief = store.event(event_id)
        if any(flag["level"] == "block" for flag in rules.quote_flags(candidate, vendor, brief)):
            raise ValueError("Price safeguards are not satisfied. Reconfirm all flagged charges before approval.")
        store.save_quote(event_id, vendor_id, None, candidate)
    store.resolve_update(update_id, "approved" if approve else "rejected")
    store.add_alert(event_id, "info", f"{'Approved' if approve else 'Rejected'} revised price from {vendor['name']}.")
    confirmation_id = confirmations.resume_after_review(event_id, vendor_id)
    return {'confirmation_id': confirmation_id}


def _supersede_prices(event_id: int, vendor_id: str) -> None:
    for old in store.updates(event_id):
        if old["vendor_id"] == vendor_id and old["kind"] in ("price", "quote") and old["status"] == "pending":
            store.resolve_update(old["id"], "superseded")


def _price_terms(quote: dict[str, Any]) -> dict[str, Any]:
    return {field: quote.get(field) for field in (*price_safety.FIELDS, "price_unit", "gst_included")}


def record_quote(event_id: int, vendor_id: str, call_id: str, quote: dict[str, Any]) -> dict[str, Any]:
    """Rechecking an already booked quote never silently replaces agreed charges."""
    _, brief = store.event(event_id)
    vendor = store.vendor(vendor_id)
    committed = any(b["vendor_id"] == vendor_id and b["status"] in (*confirmation_rules.RESERVED, *confirmation_rules.BACKUP_RESERVED) for b in store.bookings(event_id))
    current = store.latest_quotes(event_id).get(vendor_id)
    if committed and quote.get("available") is False:
        update_id = record_vendor_update(event_id, vendor_id, {"kind": "cancelled", "notes": quote.get("notes") or "Vendor is no longer available."}, [], call_id=call_id)
        return {"update_id": update_id}
    if committed and current and confirmation_rules.quote_terms(current) != confirmation_rules.quote_terms(quote):
        _supersede_prices(event_id, vendor_id)
        update_id = store.save_update(event_id, vendor_id, "quote", {
            "quote": quote, "previous_quote": current, "previous_price": current.get("price_amount"),
            "notes": "Vendor corrected the quote. Review all charges before accepting.",
            "flags": rules.quote_flags(quote, vendor, brief),
        }, "pending")
        store.add_alert(event_id, "warn", f"{vendor['name']}: corrected charges need your approval; existing terms are unchanged.")
        confirmations.hold(event_id, vendor_id, 'Vendor corrected agreed quote terms; family review is required')
        return {"update_id": update_id}
    if committed and current:
        # Only verification metadata changes. Keep the booked vendor's operational terms intact.
        quote = {**current, **{k: quote[k] for k in ("amount_checks", "unverified", "initial_price_amount", "initial_price_unit", "initial_price_verified", "opening_quote") if k in quote}}
        if not any(f["level"] == "block" for f in rules.quote_flags(quote, vendor, brief)):
            _supersede_prices(event_id, vendor_id)
        else:
            confirmations.hold(event_id, vendor_id, 'Price verification failed; recheck the charges before booking confirmation')
    store.save_quote(event_id, vendor_id, call_id, quote)
    return {"saved": True}
