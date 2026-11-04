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
    wanted = f" (the family wants: {wish})" if wish else ""
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
            "transport, serving staff or any other extra charges",
            "whether GST is included",
            "how much advance they need",
        ],
        "decorator": [
            "whether they are available on the date, setting up at least three hours before the muhurat",
            f"what their {label} decoration package includes ({function['decoration']}){wanted}",
            "the package price",
            "transport or any other extra charges",
            "how much advance they need",
        ],
        "cake": [
            "whether they can deliver on the date, at least two hours before the muhurat",
            f"whether they can make the cake the family wants{wanted}",
            "the price of the cake",
            "the delivery charge",
            "how much advance they need",
        ],
        "photographer": [
            "whether they are free on the date, from half an hour before the muhurat",
            f"how many hours they cover, and whether that includes photos and video{wanted}",
            "when the photos and video will be delivered",
            "their price",
            "any travel charge",
            "how much advance they need",
        ],
        "tent_sound": [
            "whether they are free on the date, setting up at least four hours before the muhurat",
            f"what they would set up for the number of guests: tent size, chairs and sound system{wanted}",
            "whether setup and removal afterwards are included",
            "their price",
            "the transport charge",
            "how much advance they need",
        ],
    }[category]


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
