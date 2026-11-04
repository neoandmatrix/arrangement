"""Pure booking terms and read-backs. Code, not the model, chooses what must be confirmed."""

import re
from datetime import datetime
from typing import Any

import catalog
import rules

RESERVED = ("active", "awaiting_confirmation", "confirming", "confirmation_failed", "awaiting_changes")
RETRYABLE = ("pending", "no_answer", "failed", "interrupted", "callback")
_YES = re.compile(r"\s*(?:yes(?: sir)?|yes confirmed|yes (?:that is|that's) (?:correct|right)|yes of course|yes okay|yes sure|agreed|okay|ok|sure|haan|haudu|avunu|sare|aama|seri|हाँ(?: जी)?|हां(?: जी)?|जी|जी हाँ|जी हां|हाँ सही है|हां सही है|हाँ ठीक है|हाँ बिल्कुल|हाँ बिल्कुल सही है|ठीक है|बिल्कुल|ಹೌದು(?: ಸರ್)?|ಹೌದು ಸರಿ|ಹೌದು ಸರಿಯಾಗಿದೆ|ಸರಿ|ಒಪ್ಪಿಗೆ|అవును|అవును సరే|సరే|ఒప్పుకుంటున్నాను|ஆம்|ஆமாம்|ஆமாம் சரி|சரி)[.!।]?\s*", re.I)

DISCLOSURE = {'en-IN': 'I am Bandobast, an AI assistant helping the family.',
    'hi-IN': 'मैं Bandobast का एआई सहायक हूँ, परिवार की मदद कर रहा हूँ।',
    'kn-IN': 'ನಾನು ಕುಟುಂಬಕ್ಕೆ ಸಹಾಯ ಮಾಡುವ Bandobast ಎಐ ಸಹಾಯಕ.',
    'te-IN': 'నేను కుటుంబానికి సహాయం చేసే Bandobast ఏఐ సహాయకుడిని.',
    'ta-IN': 'நான் குடும்பத்திற்கு உதவும் Bandobast ஏஐ உதவியாளர்.'}


def agreed(text: str) -> bool:
    """Only a clear complete affirmative counts, never 'yes, but...', dates, prices or model claims."""
    return bool(_YES.fullmatch(text))


def terms(brief: dict[str, Any], quote: dict[str, Any], category: str) -> dict[str, Any]:
    arrive_by = rules.event_start(brief) - rules.ARRIVAL_LEAD[category]
    return {
        "function": catalog.function_label(brief), "family": brief["family_name"],
        "date": brief["date"], "start_time": brief["start_time"], "area": brief["area"], "city": brief["city"],
        "location": brief.get("location"), "guests": brief["guests"], "category": category,
        "arrive_by": arrive_by.isoformat(timespec="minutes"),
        "cuisine": brief.get("cuisine", "") if category == "caterer" else "",
        "rituals": brief.get("rituals", "") if category == "priest" else "",
        "wishes": (brief.get("details") or {}).get(category, ""),
        "quote": {k: quote.get(k) for k in ("available", "price_amount", "price_unit", "transport_charge",
                    "extra_charges_amount", "extra_charges_notes", "gst_included", "advance_amount", "includes_notes")},
        "total": rules.landed_cost(quote, brief),
    }


def quote_terms(quote: dict[str, Any]) -> dict[str, Any]:
    return {k: quote.get(k) for k in ("price_amount", "price_unit", "transport_charge", "extra_charges_amount",
            "extra_charges_notes", "gst_included", "advance_amount", "includes_notes")}


_TEXT = {
    "en-IN": ("Date {date}, muhurat {time}, {area}. Is that correct?", "Arrival or setup by {arrival}. Can you confirm?",
              "For {guests} guests. Can you confirm?", "Service detail: {detail}. Is that included?",
              "Base price {price} rupees {unit}. Is that correct?", "Transport {transport} rupees. Is that correct?",
              "Extras {extras} rupees. Is that correct?", "Total {total} rupees, advance {advance} rupees. Do you confirm the booking?"),
    "hi-IN": ("तारीख {date}, मुहूर्त {time}, जगह {area}। क्या यह सही है?", "आने या सेटअप का समय {arrival} है। क्या आप पुष्टि करते हैं?",
              "{guests} मेहमान हैं। क्या यह सही है?", "सेवा का विवरण: {detail}। क्या यह शामिल है?",
              "मूल कीमत {price} रुपये {unit} है। क्या यह सही है?", "ट्रांसपोर्ट {transport} रुपये है। क्या यह सही है?",
              "अतिरिक्त खर्च {extras} रुपये है। क्या यह सही है?", "कुल {total} रुपये, एडवांस {advance} रुपये। क्या आप बुकिंग की पुष्टि करते हैं?"),
    "kn-IN": ("ದಿನಾಂಕ {date}, ಮುಹೂರ್ತ {time}, ಸ್ಥಳ {area}. ಇದು ಸರಿಯೇ?", "ಆಗಮನ ಅಥವಾ ಸಿದ್ಧತೆ {arrival} ಒಳಗೆ. ದೃಢಪಡಿಸುತ್ತೀರಾ?",
              "{guests} ಅತಿಥಿಗಳು. ಇದು ಸರಿಯೇ?", "ಸೇವೆಯ ವಿವರ: {detail}. ಇದು ಒಳಗೊಂಡಿದೆಯೇ?",
              "ಮೂಲ ಬೆಲೆ {price} ರೂಪಾಯಿ {unit}. ಇದು ಸರಿಯೇ?", "ಸಾರಿಗೆ {transport} ರೂಪಾಯಿ. ಇದು ಸರಿಯೇ?",
              "ಹೆಚ್ಚುವರಿ ವೆಚ್ಚ {extras} ರೂಪಾಯಿ. ಇದು ಸರಿಯೇ?", "ಒಟ್ಟು {total} ರೂಪಾಯಿ, ಮುಂಗಡ {advance} ರೂಪಾಯಿ. ಬುಕಿಂಗ್ ದೃಢಪಡಿಸುತ್ತೀರಾ?"),
    "te-IN": ("తేదీ {date}, ముహూర్తం {time}, స్థలం {area}. ఇది సరైనదేనా?", "రాక లేదా ఏర్పాట్లు {arrival} లోపు. నిర్ధారిస్తారా?",
              "{guests} మంది అతిథులు. ఇది సరైనదేనా?", "సేవ వివరాలు: {detail}. ఇది చేర్చబడిందా?",
              "ప్రాథమిక ధర {price} రూపాయలు {unit}. ఇది సరైనదేనా?", "రవాణా {transport} రూపాయలు. ఇది సరైనదేనా?",
              "అదనపు ఖర్చులు {extras} రూపాయలు. ఇది సరైనదేనా?", "మొత్తం {total} రూపాయలు, అడ్వాన్స్ {advance} రూపాయలు. బుకింగ్ నిర్ధారిస్తారా?"),
    "ta-IN": ("தேதி {date}, முகூர்த்தம் {time}, இடம் {area}. இது சரியா?", "வருகை அல்லது ஏற்பாடுகள் {arrival}க்குள். உறுதி செய்கிறீர்களா?",
              "{guests} விருந்தினர்கள். இது சரியா?", "சேவை விவரம்: {detail}. இது சேர்க்கப்பட்டுள்ளதா?",
              "அடிப்படை விலை {price} ரூபாய் {unit}. இது சரியா?", "போக்குவரத்து {transport} ரூபாய். இது சரியா?",
              "கூடுதல் செலவு {extras} ரூபாய். இது சரியா?", "மொத்தம் {total} ரூபாய், முன்பணம் {advance} ரூபாய். முன்பதிவை உறுதி செய்கிறீர்களா?"),
}
_UNITS = {"en-IN": ("per plate", "in total"), "hi-IN": ("प्रति प्लेट", "कुल"), "kn-IN": ("ಪ್ರತಿ ಪ್ಲೇಟ್", "ಒಟ್ಟು"),
          "te-IN": ("ప్రతి ప్లేట్", "మొత్తం"), "ta-IN": ("ஒரு தட்டுக்கு", "மொத்தம்")}


def questions(snapshot: dict[str, Any], language: str) -> list[dict[str, str]]:
    """Protected numbers come from the approved snapshot; service notes are read in short chunks."""
    templates = _TEXT.get(language, _TEXT["en-IN"])
    quote = snapshot["quote"]
    values = {**snapshot, 'date': datetime.fromisoformat(snapshot['date']).strftime('%d %B %Y'),
              'area': f"{snapshot['area']}, {snapshot['city']}", "time": snapshot["start_time"],
              "arrival": datetime.fromisoformat(snapshot['arrive_by']).strftime('%d %B %Y %H:%M'),
              "price": quote["price_amount"], "transport": quote["transport_charge"], "extras": quote["extra_charges_amount"],
              "advance": quote["advance_amount"], "unit": _UNITS.get(language, _UNITS["en-IN"])[0 if quote["price_unit"] == "per_plate" else 1]}
    result = [{"key": key, "text": templates[index].format(**values)} for index, key in enumerate(("event", "arrival", "guests"))]
    details = [snapshot["function"], snapshot.get("cuisine"), snapshot.get("rituals"), snapshot.get("wishes"), quote.get("includes_notes"), quote.get("extra_charges_notes")]
    if quote.get("gst_included") is True:
        details.append({"hi-IN": "जीएसटी शामिल है", "kn-IN": "ಜಿಎಸ್‌ಟಿ ಸೇರಿದೆ", "te-IN": "జీఎస్టీ కలిపి", "ta-IN": "ஜிஎஸ்டி உட்பட"}.get(language, "GST included"))
    if snapshot.get("location"):
        details.append(snapshot["location"].get("label"))
        details.append(f"Venue pin: {snapshot['location'].get('lat')}, {snapshot['location'].get('lon')}")
    seen = set()
    for detail in details:
        if not detail or str(detail) in seen:
            continue
        seen.add(str(detail))
        # Never send tool markup to the voice. Short pieces also keep long coverage notes audible.
        clean = re.sub(r"[\[\]{}()?]", " ", str(detail))
        words = clean.split()
        for start in range(0, len(words), 12):
            result.append({"key": f"service-{len(result)}", "text": templates[3].format(detail=" ".join(words[start:start + 12]))})
    for index, key in ((4, "base_price"), (5, "transport"), (6, "extras"), (7, "booking")):
        result.append({"key": key, "text": templates[index].format(**values)})
    return [{**item, 'text': ' '.join(re.sub(r'[\[\]{}()_*`<>]', ' ', item['text']).split())} for item in result]
