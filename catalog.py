"""What Bandobast arranges: the functions it covers and the vendor types it books.

One place for the names, call checklists and must-answer quote items, so prompts, tools,
validation and the dashboard all agree. Policy (arrival times, negotiation) lives in rules.py.
"""

from typing import Any

# Function types. Default needs are suggested at intake; the family can add or drop any vendor.
FUNCTIONS: dict[str, dict[str, Any]] = {
    "griha_pravesh": {
        "name": "Griha pravesh",
        "variants": {"griha_pravesh": "Griha pravesh", "vastu_shanti": "Vastu shanti and griha pravesh"},
        "default_needs": ["priest", "caterer", "decorator"],
        "optional": ["tent_sound", "photographer"],
        "decoration": "toran, marigold flowers at the entrance, and rangoli",
        "food_hint": "a meal for the guests after the puja",
    },
    "puja": {
        "name": "Puja",
        "variants": {
            "satyanarayan": "Satyanarayan puja and katha",
            "lakshmi": "Lakshmi puja",
            "ganesh": "Ganesh puja",
            "rudrabhishek": "Rudrabhishek",
        },
        "default_needs": ["priest", "caterer", "decorator"],
        "optional": ["photographer", "tent_sound"],
        "decoration": "flowers for the puja area and the mandap",
        "food_hint": "prasad only, or a full meal",
    },
    "baby_ceremony": {
        "name": "Baby ceremony",
        "variants": {"naamkaran": "Naamkaran (naming ceremony)", "annaprashan": "Annaprashan", "mundan": "Mundan"},
        "default_needs": ["priest", "caterer", "decorator"],
        "optional": ["cake", "photographer", "tent_sound"],
        "decoration": "cradle decoration, flowers and balloons",
        "food_hint": "a meal for the guests",
    },
}

# Suggest a tent and sound system above this many guests: most homes can't seat more.
TENT_SUGGESTED_ABOVE_GUESTS = 60

# Vendor types. "details" is what the family is asked at intake so the vendor can quote.
CATEGORIES: dict[str, dict[str, Any]] = {
    "priest": {"name": "Priest", "spoken": "priest (pandit ji)", "details": None},
    "caterer": {"name": "Caterer", "spoken": "caterer", "details": None},
    "decorator": {"name": "Decorator", "spoken": "decorator", "details": "any decoration wishes, like colours or flowers"},
    "cake": {"name": "Cake", "spoken": "cake shop", "details": "flavour, eggless or not, and weight in kilos"},
    "photographer": {"name": "Photographer", "spoken": "photographer", "details": "photos, video or both, and for how many hours"},
    "tent_sound": {"name": "Tent and sound", "spoken": "tent, chairs and sound supplier", "details": "tent or shamiana, chairs, and whether a sound system is needed"},
}

# Must be answered before a quote is saved (0 is a valid answer: "no charge").
QUOTE_REQUIRED: dict[str, dict[str, str]] = {
    "priest": {
        "price_amount": "dakshina",
        "extra_charges_amount": "samagri cost (0 if included)",
        "transport_charge": "travel charge",
        "advance_amount": "advance",
    },
    "caterer": {
        "price_amount": "price per plate",
        "transport_charge": "transport charge",
        "extra_charges_amount": "staff and other extra charges (0 if none)",
        "gst_included": "whether GST is included",
        "advance_amount": "advance",
    },
    "decorator": {
        "price_amount": "package price",
        "includes_notes": "what the package includes",
        "transport_charge": "transport charge",
        "extra_charges_amount": "other extra charges (0 if none)",
        "advance_amount": "advance",
    },
    "cake": {
        "price_amount": "cake price",
        "transport_charge": "delivery charge",
        "extra_charges_amount": "other extra charges (0 if none)",
        "advance_amount": "advance",
    },
    "photographer": {
        "price_amount": "price",
        "includes_notes": "hours covered, photos or video, and when they're delivered",
        "transport_charge": "travel charge",
        "extra_charges_amount": "other extra charges (0 if none)",
        "advance_amount": "advance",
    },
    "tent_sound": {
        "price_amount": "price",
        "includes_notes": "tent size, chairs and sound, and whether setup and removal are included",
        "transport_charge": "transport charge",
        "extra_charges_amount": "other extra charges (0 if none)",
        "advance_amount": "advance",
    },
}


def function_of(brief: dict[str, Any]) -> dict[str, Any] | None:
    return FUNCTIONS.get(brief.get("function_type", ""))


def function_label(brief: dict[str, Any]) -> str:
    """'Satyanarayan puja and katha', or whatever free text an older brief holds."""
    function = function_of(brief)
    if not function:
        return str(brief.get("function_type") or "function")
    return function["variants"].get(brief.get("function_variant") or "", function["name"])


def checklist(category: str, brief: dict[str, Any]) -> list[str]:
    """What to ask this vendor, one question per item, shaped by the function."""
    function = function_of(brief) or FUNCTIONS["griha_pravesh"]
    label = function_label(brief)
    wish = (brief.get("details") or {}).get(category)
    # The family's wish is its own plain yes/no question. Folded into another question it came out
    # as "with three hours of photos and video, how many hours will you cover?", which confused vendors.
    can_do = ([f"whether they can do what the family wants: {wish}. Say it plainly in one short sentence and ask "
               "if they can do that; don't turn it into a how-many question"] if wish else [])
    return {
        "priest": [
            "whether they are free on the date at the muhurat time",
            f"whether they can perform the {label} as the family wants, and in their language",
            "their dakshina",
            "whether samagri is included, and if not what it costs",
            "any travel charge",
            "how much advance they need, if any",
        ],
        "caterer": [
            "whether they are available on the date, and can serve before the muhurat ends",
            "whether they can make the food the family wants",
            "price per plate for the number of guests",
            "the transport charge",
            "any other extra charge, like serving staff",
            "whether GST is included",
            "how much advance they need",
        ],
        "decorator": [
            "whether they are available on the date, setting up at least three hours before the muhurat",
            *(can_do or [f"what their {label} decoration package includes (usually {function['decoration']})"]),
            "the package price",
            "the transport charge",
            "any other extra charge, like materials",
            "how much advance they need",
        ],
        "cake": [
            "whether they can deliver on the date, at least two hours before the muhurat",
            *(can_do or [f"what cake they would suggest for a {label}"]),
            "the price of the cake",
            "the delivery charge",
            "any other extra charge",
            "how much advance they need",
        ],
        "photographer": [
            "whether they are free on the date, from half an hour before the muhurat",
            *(can_do or ["whether they do both photos and video", "how many hours they cover"]),
            "when the photos and video will be delivered",
            "their price",
            "any travel charge",
            "any other extra charge, like extra hours or prints",
            "how much advance they need",
        ],
        "tent_sound": [
            "whether they are free on the date, setting up at least four hours before the muhurat",
            *(can_do or ["what they would set up for the number of guests: tent size, chairs and sound system"]),
            "whether setup and removal afterwards are included",
            "their price",
            "the transport charge",
            "any other extra charge",
            "how much advance they need",
        ],
    }[category]


# What a vendor asks before quoting ("कितना बड़ा function है, कहाँ है, क्या सजाना है?"), answered by a fixed
# line built from the brief, so the model never has to make it up and never dodges with "what's your rate?".
_VENUE = {"griha_pravesh": ("नए घर पर", "at the new house"), "puja": ("घर पर", "at home"),
          "baby_ceremony": ("घर पर", "at home")}
_PUJA_HI = {"griha_pravesh": "गृह प्रवेश की पूजा", "puja": "घर पर पूजा", "baby_ceremony": "बच्चे के संस्कार की पूजा"}
_DECORATION_HI = {"griha_pravesh": "मेन डोर पर तोरण, गेंदे के फूल और रंगोली",
                  "puja": "पूजा की जगह और मंडप पर फूलों की सजावट",
                  "baby_ceremony": "पालने की सजावट, फूल और बैलून"}


def _size(guests: int) -> tuple[str, str]:
    if guests <= 25:
        return "छोटा फंक्शन", "a small function"
    if guests <= 100:
        return "मीडियम साइज़ का फंक्शन", "a medium-sized function"
    return "बड़ा फंक्शन", "a big function"


def scope_line(category: str, brief: dict[str, Any]) -> tuple[str, str]:
    """(Hinglish line, English meaning) describing the job: size, guests, place, what's needed."""
    key = brief.get("function_type") if brief.get("function_type") in FUNCTIONS else "griha_pravesh"
    guests = int(brief.get("guests") or 0)
    size, size_en = _size(guests)
    venue, venue_en = _VENUE[key]
    morning = str(brief.get("start_time") or "08:00") < "12:00"
    wish = ((brief.get("details") or {}).get(category) or "").strip()
    head, head_en = f"{size} है, लगभग {guests} गेस्ट, {venue}", f"It's {size_en}, about {guests} guests, {venue_en}"
    if category == "decorator":
        what, what_en = _DECORATION_HI[key], FUNCTIONS[key]["decoration"]
        return f"{head}। {what} चाहिए", f"{head_en}. We need {wish or what_en}"
    if category == "tent_sound":
        place, place_en = ("टेरेस पर", "on the terrace") if "terrace" in wish.lower() else ("घर के पास खुले मैदान में", "in an open ground near the house")
        return (f"{head}। {place} शामियाना, {guests} कुर्सियाँ और छोटा साउंड सिस्टम चाहिए",
                f"{head_en}. We need a shamiana {place_en}, {guests} chairs and a small sound system")
    if category == "caterer":
        meal, meal_en = ("सुबह पूजा के बाद खाना", "a meal after the morning puja") if morning else ("पूजा के बाद खाना", "a meal after the puja")
        return f"{head}, {meal}", f"{head_en}, {meal_en}"
    if category == "photographer":
        return f"{head}, पूजा और फंक्शन की फोटो और वीडियो चाहिए", f"{head_en}; we need photos and video of the puja and function"
    if category == "priest":
        return f"{_PUJA_HI[key]} है, {venue}, लगभग {guests} गेस्ट", f"The puja is {function_label(brief)}, {venue_en}, about {guests} guests"
    return head, head_en


def functions_for_prompt() -> str:
    """The function list as the intake prompt shows it."""
    lines = []
    for key, f in FUNCTIONS.items():
        variants = ", ".join(f"{v} ({k})" for k, v in f["variants"].items())
        defaults = ", ".join(f["default_needs"])
        optional = ", ".join(f["optional"])
        lines.append(
            f"- {key}: {f['name']}. Variants: {variants}. Usually needs {defaults}; "
            f"may also want {optional}. Food is usually {f['food_hint']}."
        )
    return "\n".join(lines)
