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


def comparison(event_id: int, brief: dict[str, Any]) -> dict[str, Any]:
    """Ranked quotes per category, with the rules' top pick and backup."""
    quotes = store.latest_quotes(event_id)
    by_id = {v["id"]: v for v in store.vendors()}
    out = {}
    for category in rules.CATEGORIES:
        ranked = rules.rank_category(category, quotes, by_id, brief)
        top, backup = rules.pick(ranked)
        out[category] = {"ranked": ranked, "top": top, "backup": backup}
    return out


def book(category: str) -> dict[str, Any]:
    """Family approved: book the top pick, keep the runner-up on standby, schedule check-ins."""
    event_id, brief = require_event()
    result = comparison(event_id, brief)[category]
    if not result["top"]:
        raise ValueError(f"No bookable {category} quote yet")
    store.set_booking(event_id, category, "primary", result["top"], "active")
    if result["backup"]:
        store.set_booking(event_id, category, "backup", result["backup"], "standby")
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


def record_checkin(checkin_id: int, status: str, eta: str | None, notes: str | None) -> dict[str, str] | None:
    """Store a check-in result, then let the rules decide whether to switch to the backup."""
    checkin = store.checkin(checkin_id)
    store.finish_checkin(checkin_id, status, eta, notes)
    event_id, brief = require_event()
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
        "confirmation call scheduled.",
    )


def mark_unreachable(checkin_id: int) -> dict[str, str] | None:
    """The phone wasn't answered. In browser testing you trigger this from the dashboard."""
    return record_checkin(checkin_id, "unreachable", None, "No answer")
