"""Deterministic decisions: the LLM talks and extracts, these rules decide.

- landed cost and red flags for each quote
- ranking per category, with a top pick and a backup
- the check-in schedule before the function
- when a booked vendor is at risk and the backup should take over
- what to counter-offer when negotiating, and when to stop
- which vendors to call: the shortlist near the venue
"""

import math
import re
from datetime import datetime, timedelta
from typing import Any

CATEGORIES = ("priest", "caterer", "decorator", "cake", "photographer", "tent_sound")

# How early each vendor must be at the venue (or deliver), relative to the muhurat.
ARRIVAL_LEAD = {
    "priest": timedelta(minutes=30),
    "caterer": timedelta(minutes=60),
    "decorator": timedelta(hours=3),
    "cake": timedelta(hours=2),
    "photographer": timedelta(minutes=30),
    "tent_sound": timedelta(hours=4),
}

# Review phrases that predict the last-minute failure we are guarding against.
RELIABILITY_WARNINGS = (
    "late",
    "did not show",
    "didn't show",
    "not show up",
    "cancelled",
    "canceled",
    "did not come",
    "changed on the day",
    "raised after",
    "extra and not told",
    "without telling",
    "full payment in advance",
)

ADVANCE_LIMIT_SHARE = 0.5  # asking for more than half up front is a red flag

AMOUNT_FIELDS = (
    "price_amount",
    "initial_price_amount",
    "transport_charge",
    "extra_charges_amount",
    "advance_amount",
)

# Rounds of negotiation per vendor type. Haggling over a priest's dakshina comes across as
# disrespectful, so we don't; a cake is a small order, so one gentle ask at most.
NEGOTIATION_ROUNDS = {"caterer": 2, "decorator": 2, "photographer": 2, "tent_sound": 2, "cake": 1}
NEGOTIABLE = tuple(NEGOTIATION_ROUNDS)
FIRST_ASK_SHARE = 0.85  # never open lower than 15% off the vendor's total
GENTLE_ASK_SHARE = 0.95  # under budget with no cheaper quote: ask for about 5% off, once
_NUMBER_RE = re.compile(r"\d[\d,]*")  # \d matches Devanagari, Kannada etc. digits too


def numbers_heard(text: str) -> set[int]:
    """Every number in what the vendor said. Prisma's 'transcribe' format writes them as digits."""
    return {int(m.replace(",", "")) for m in _NUMBER_RE.findall(text) if m.replace(",", "")}


def unverified_amounts(quote: dict[str, Any], vendor_said: str) -> list[str]:
    """Amounts in a recorded quote that the vendor never actually said.

    The LLM extracts the quote, and it can invent or mishear numbers; a price that can't be
    traced to the vendor's own words must not drive a booking.
    """
    heard = numbers_heard(vendor_said)
    return [f for f in AMOUNT_FIELDS if quote.get(f) and quote[f] not in heard]


def event_start(brief: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(f"{brief['date']}T{brief['start_time']}")


def landed_cost(quote: dict[str, Any], brief: dict[str, Any]) -> int | None:
    """Total the family actually pays: base price (per plate x guests, or total) plus all extras."""
    price = quote.get("price_amount")
    if price is None:
        return None
    base = price * int(brief.get("guests") or 0) if quote.get("price_unit") == "per_plate" else price
    extras = (quote.get("transport_charge") or 0) + (quote.get("extra_charges_amount") or 0)
    return int(base + extras)


def best_competing_total(competitor_quotes: list[dict[str, Any]], brief: dict[str, Any]) -> int | None:
    """Lowest real, verified total among other vendors' quotes for the same service, if any.

    Only quotes we actually collected count, so the bot never invents a competing offer.
    """
    totals = [
        t
        for q in competitor_quotes
        if q.get("available") and not q.get("unverified") and (t := landed_cost(q, brief))
    ]
    return min(totals) if totals else None


def _round_down(amount: float, price_unit: str) -> int:
    step = 10 if price_unit == "per_plate" else 100
    return int(amount // step * step)


def counter_offer(
    category: str,
    offer: dict[str, Any],
    brief: dict[str, Any],
    *,
    round_no: int,
    competing_total: int | None,
    previous_ask_total: int | None = None,
) -> dict[str, Any]:
    """What to ask for in negotiation round `round_no` (1-based), given the vendor's current offer.

    Returns {"negotiate": False, ...} when we should accept, or a counter-offer with a truthful
    reason. The counter is expressed in the vendor's own unit (per plate or total).
    """
    stop = {"negotiate": False}
    if category not in NEGOTIABLE:
        return {**stop, "why": f"We don't negotiate with a {category}."}
    total = landed_cost(offer, brief)
    if total is None:
        return {**stop, "why": "No price yet."}
    if round_no > NEGOTIATION_ROUNDS[category]:
        return {**stop, "why": "Enough rounds; accept their final price politely."}

    budget = (brief.get("budget") or {}).get(category)
    targets = [t for t in (budget, competing_total) if t]
    target = min(targets) if targets else None
    unit = offer.get("price_unit") or "total"
    guests = int(brief.get("guests") or 1)
    extras = (offer.get("transport_charge") or 0) + (offer.get("extra_charges_amount") or 0)

    if target is None or total <= target:
        # Already fine: one gentle ask, then accept.
        if round_no > 1:
            return {**stop, "why": "The price is within budget; accept it."}
        if offer.get("transport_charge"):
            return {
                "negotiate": True,
                "ask": "waive_transport",
                "transport_charge": 0,
                "why": "A small courtesy: ask if they can include transport.",
            }
        ask_total = total * GENTLE_ASK_SHARE
        reason = "small_discount"
    else:
        floor = total * FIRST_ASK_SHARE
        if round_no == 1:
            ask_total = max(target, floor)
        else:
            # Meet them halfway between our last ask and their new offer.
            ask_total = max(target, ((previous_ask_total or floor) + total) / 2)
        reason = "competing_quote" if competing_total and competing_total == target else "budget"

    base = ask_total - extras
    price = _round_down(base / guests if unit == "per_plate" else base, unit)
    if price >= (offer.get("price_amount") or 0):
        return {**stop, "why": "No meaningful discount left to ask for."}
    asked_total = price * guests + extras if unit == "per_plate" else price + extras
    result = {
        "negotiate": True,
        "ask": "lower_price",
        "price_amount": price,
        "price_unit": unit,
        "asked_total": int(asked_total),
        "reason": reason,
    }
    if reason == "budget":
        result["why"] = f"The family's budget for this is about ₹{target}."
    elif reason == "competing_quote":
        result["why"] = f"We have another quote of about ₹{target} in total."
    else:
        result["why"] = "Ask politely if they can do a little better."
    return result


def savings(quote: dict[str, Any], brief: dict[str, Any]) -> int | None:
    """How much negotiation saved on the total, if the price came down."""
    first = quote.get("initial_price_amount")
    if not first or not quote.get("price_amount") or first <= quote["price_amount"]:
        return None
    before = landed_cost({**quote, "price_amount": first}, brief)
    after = landed_cost(quote, brief)
    return (before - after) if before and after else None


_WARNING_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in RELIABILITY_WARNINGS) + r")\b")


def review_warnings(vendor: dict[str, Any]) -> list[str]:
    # Whole words only, so "late" doesn't match "per plate".
    return [r for r in vendor.get("reviews", []) if _WARNING_RE.search(r.lower())]


def quote_flags(quote: dict[str, Any], vendor: dict[str, Any], brief: dict[str, Any]) -> list[dict[str, str]]:
    """Problems with a quote. level 'block' means it can't be booked as-is."""
    flags: list[dict[str, str]] = []

    def add(level: str, text: str) -> None:
        flags.append({"level": level, "text": text})

    if quote.get("available") is False:
        add("block", "Not available on the date")
    elif quote.get("available") is None:
        add("warn", "Availability not confirmed")

    if quote.get("price_amount") is None:
        add("block", "No price given")
    for field in quote.get("unverified", []):
        label = field.replace("_amount", "").replace("_", " ")
        text = f"{label.capitalize()} ₹{quote.get(field)} was not heard from the vendor; re-confirm"
        add("block" if field == "price_amount" else "warn", text)
    if quote.get("transport_charge") is None:
        add("warn", "Transport charge not discussed")
    if quote.get("gst_included") is None and vendor["category"] == "caterer":
        add("warn", "GST not clarified")
    if quote.get("extra_charges_amount"):
        add("info", f"Extra charges: ₹{quote['extra_charges_amount']} ({quote.get('extra_charges_notes') or 'unspecified'})")
    if saved := savings(quote, brief):
        add("info", f"Negotiated ₹{quote['initial_price_amount']} → ₹{quote['price_amount']}: saves ₹{saved}")

    total = landed_cost(quote, brief)
    advance = quote.get("advance_amount")
    if advance is None:
        add("warn", "Advance amount not discussed")
    elif total and advance > total * ADVANCE_LIMIT_SHARE:
        add("warn", f"Advance ₹{advance} is more than half of the total ₹{total}")

    budget = (brief.get("budget") or {}).get(vendor["category"])
    if budget and total and total > budget:
        add("warn", f"Over budget: ₹{total} vs ₹{budget}")

    for review in review_warnings(vendor):
        add("warn", f"Review: “{review}”")
    return flags


def rank_category(
    category: str,
    quotes: dict[str, dict[str, Any]],
    vendors_by_id: dict[str, dict[str, Any]],
    brief: dict[str, Any],
) -> list[dict[str, Any]]:
    """Bookable quotes first, then fewest warnings, then lowest landed cost, then rating."""
    rows = []
    for vendor_id, quote in quotes.items():
        vendor = vendors_by_id.get(vendor_id)
        if not vendor or vendor["category"] != category:
            continue
        flags = quote_flags(quote, vendor, brief)
        rows.append(
            {
                "vendor_id": vendor_id,
                "vendor_name": vendor["name"],
                "landed_cost": landed_cost(quote, brief),
                "flags": flags,
                "bookable": not any(f["level"] == "block" for f in flags),
                "warnings": sum(f["level"] == "warn" for f in flags),
                "rating": vendor.get("rating", 0),
                "quote": quote,
            }
        )
    rows.sort(
        key=lambda r: (
            not r["bookable"],
            r["warnings"],
            r["landed_cost"] if r["landed_cost"] is not None else float("inf"),
            -r["rating"],
        )
    )
    return rows


def pick(ranked: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Top bookable vendor and the next bookable one as backup."""
    bookable = [r["vendor_id"] for r in ranked if r["bookable"]]
    return (bookable[0] if bookable else None, bookable[1] if len(bookable) > 1 else None)


def checkin_schedule(brief: dict[str, Any], category: str) -> list[tuple[str, str]]:
    """(label, due_at) check-in calls before the function."""
    start = event_start(brief)
    arrive_by = start - ARRIVAL_LEAD[category]
    day = start.replace(hour=18, minute=0, second=0, microsecond=0)
    schedule = [
        ("D-3 confirmation", day - timedelta(days=3)),
        ("D-1 confirmation", day - timedelta(days=1)),
        ("Morning: on the way?", arrive_by - timedelta(hours=2)),
        ("Final: ETA check", arrive_by - timedelta(minutes=45)),
    ]
    return [(label, due.isoformat(timespec="minutes")) for label, due in schedule]


def assess_risk(
    checkins: list[dict[str, Any]], brief: dict[str, Any], category: str
) -> dict[str, str] | None:
    """Look at completed check-ins for a booked vendor. Returns an action when there's risk.

    action 'activate_backup' hands the job to the standby vendor; 'warn' tells the family.
    """
    done = [c for c in checkins if c["status"] != "pending"]
    if not done:
        return None
    last = done[-1]
    arrive_by = event_start(brief) - ARRIVAL_LEAD[category]

    if last["status"] == "cancelled":
        return {"action": "activate_backup", "reason": "Vendor cancelled"}

    unreachable_streak = 0
    for c in reversed(done):
        if c["status"] != "unreachable":
            break
        unreachable_streak += 1
    if unreachable_streak >= 2:
        return {"action": "activate_backup", "reason": "Unreachable on two check-ins in a row"}

    if last["status"] == "delayed" and last.get("eta"):
        eta = datetime.fromisoformat(f"{brief['date']}T{last['eta']}")
        if eta > arrive_by:
            return {
                "action": "activate_backup",
                "reason": f"ETA {last['eta']} is after the required arrival {arrive_by:%H:%M}",
            }
        return {"action": "warn", "reason": f"Running late but should make it (ETA {last['eta']})"}

    is_final = last["label"].startswith("Final")
    if last["status"] == "unsure" and is_final:
        return {"action": "activate_backup", "reason": "Not sure they can make it, on the final check"}
    if last["status"] in ("unsure", "unreachable"):
        return {"action": "warn", "reason": f"Check-in '{last['label']}': {last['status']}"}
    return None


SHORTLIST_SIZE = 3
MAX_DISTANCE_KM = 15  # across Bengaluru traffic, further than this risks a late arrival


def distance_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Straight-line distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def venue_location(brief: dict[str, Any]) -> tuple[float, float] | None:
    loc = brief.get("location") or {}
    if loc.get("lat") is None or loc.get("lon") is None:
        return None
    return float(loc["lat"]), float(loc["lon"])


def shortlist(
    category: str,
    vendors: list[dict[str, Any]],
    brief: dict[str, Any],
    *,
    exclude: set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Up to three vendors to call for one service: near enough, fewest review warnings, best rated.

    Returns rows {vendor_id, distance_km, warnings, rating}, in calling order.
    """
    venue = venue_location(brief)
    rows = []
    for v in vendors:
        if v["category"] != category or v["id"] in exclude:
            continue
        distance = None
        if venue and v.get("lat") is not None:
            distance = round(distance_km(*venue, v["lat"], v["lon"]), 1)
            if distance > MAX_DISTANCE_KM:
                continue
        rows.append(
            {
                "vendor_id": v["id"],
                "distance_km": distance,
                "warnings": len(review_warnings(v)),
                "rating": v.get("rating", 0),
            }
        )
    rows.sort(key=lambda r: (r["warnings"] > 0, -r["rating"], r["distance_km"] or 0))
    return rows[:SHORTLIST_SIZE]
