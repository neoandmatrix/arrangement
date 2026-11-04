"""Pure price checks. Only vendor speech, in the correct charge context, is evidence.

Unknown wording is deliberately unverified, never a reason to guess. Prompts can ask a clearer
question, but they cannot turn a number, a 'yes', or a model-generated read-back into proof.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

VERSION = 1
FIELDS = ("price_amount", "transport_charge", "extra_charges_amount", "advance_amount")
LABELS = {
    "price_amount": "base price",
    "transport_charge": "transport / delivery charge",
    "extra_charges_amount": "other extra charges",
    "advance_amount": "advance",
}
# Not hard price caps: above these, the vendor must repeat the charge and amount separately.
BASE_RECHECK = {"priest": 50_000, "caterer": 200_000, "decorator": 75_000,
                "cake": 15_000, "photographer": 100_000, "tent_sound": 150_000}
MAX_AMOUNT = 1_000_000_000  # bounds corrupt numeric payloads, not a typical-event price estimate

_SCALES = {
    "thousand": 1000, "thousands": 1000, "hazar": 1000, "hazaar": 1000, "हजार": 1000, "हज़ार": 1000,
    "ಸಾವಿರ": 1000, "వేలు": 1000, "వేల": 1000, "ஆயிரம்": 1000, "হাজার": 1000, "હજાર": 1000,
    "ਹਜ਼ਾਰ": 1000, "ആയിരം": 1000, "हजारों": 1000,
    "lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000, "लाख": 100000,
    "ಲಕ್ಷ": 100000, "లక్షలు": 100000, "లక్ష": 100000, "லட்சம்": 100000, "লাখ": 100000,
    "લાખ": 100000, "ਲੱਖ": 100000, "ലക്ഷം": 100000,
    "crore": 10000000, "crores": 10000000, "करोड़": 10000000, "करोड": 10000000,
    "ಕೋಟಿ": 10000000, "కోట్లు": 10000000, "కోటి": 10000000, "கோடி": 10000000,
    "কোটি": 10000000, "કરોડ": 10000000, "ਕਰੋੜ": 10000000, "കോടി": 10000000,
}
_SCALE_RE = "|".join(re.escape(k) for k in sorted(_SCALES, key=len, reverse=True))
_NUMBER = re.compile(r"(?<![\w.:/+-])(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<scale>" + _SCALE_RE + r")?(?!\d)", re.I)
_NON_MONEY_UNIT = re.compile(r"^\s*(?:%|(?:guests?|people|persons?|plates?|chairs?|hours?|minutes?|days?|kg|kilos?|kilograms?|am|pm)\b|मेहमान|लोग|घंट|किलो|बजे|நபர்கள்|பேர்|மணிக்கு|ಗಂಟೆ|ಜನ|మంది|గంటల)", re.I)
_MONTH = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|date|muhurat|guests?|people|hours?|minutes?)\b|नवंबर|नवम्बर|अक्टूबर|तारीख|मेहमान|ಲೋಗ|ದಿನಾಂಕ|నవంబర్|తేదీ|தேதி", re.I)
_DATE_CONTEXT = re.compile(r"\b(?:date|time|muhurat|arrive|arrival|at)\b|तारीख|बजे|ದಿನಾಂಕ|తేదీ|தேதி", re.I)
_GST = re.compile(r"\bgst\b|जीएसटी|ಜಿಎಸ್.?ಟಿ|జీఎస్టీ|ஜிஎஸ்டி", re.I)
_INCLUDED = re.compile(r"\b(?:included|including|inclusive|includes)\b|शामिल|ಸೇರಿದೆ|సహా|కలిపి|చేర్చ|சேர்ந்த|உட்பட", re.I)
_YES = re.compile(r"\s*(?:yes(?: sir)?|yes included|haan|हाँ(?: जी)?|जी हाँ|ಹೌದು(?: ಸರ್)?|అవును|ஆம்|ஆமாம்|included|शामिल है)[.!।]?\s*", re.I)

_ALIASES = {
    "price_amount": r"\b(?:base price|price|rate|dakshina|fee|cost|total|per plate)\b|दक्षिणा|कीमत|दाम|रेट|कुल|प्रति प्लेट|ಬೆಲೆ|ದರ|ದಕ್ಷಿಣೆ|ಒಟ್ಟು|ಪ್ರತಿ ಪ್ಲೇಟ್|ధర|రేటు|దక్షిణ|మొత్తం|ప్లేటుకు|விலை|தட்சணை|மொத்தம்|ஒரு தட்டு|দাম|দক্ষিণা|মোট|કિંમત|દક્ષિણા|કુલ|ਕੀਮਤ|ਕੁੱਲ|വില|ദക്ഷിണ|മൊത്തം",
    "transport_charge": r"\b(?:transport|travel|delivery|transportation)\b|ट्रांसपोर्ट|परिवहन|डिलीवरी|यात्रा|आने जाने|ಸಾರಿಗೆ|ಟ್ರಾನ್ಸ್.?ಪೋರ್ಟ್|ಡೆಲಿವರಿ|ಪ್ರಯಾಣ|రవాణా|ట్రాన్స్.?పోర్ట్|డెలివరీ|ప్రయాణ|போக்குவரத்து|டெலிவரி|பயண|পরিবহন|ডেলিভারি|ડિલિવરી|પરિવહન|ਡਿਲਿਵਰੀ|യാത്ര|ഡെലിവറി",
    "extra_charges_amount": r"\b(?:extras?|other charges|additional charges|samagri|materials?|staff)\b|अतिरिक्त|सामग्री|एक्स्ट्रा|स्टाफ|ಸಾಮಗ್ರಿ|ಹೆಚ್ಚುವರಿ|ಎಕ್ಸ್ಟ್ರಾ|ಸಿಬ್ಬಂದಿ|సామాగ్రి|సామగ్రి|అదనపు|ఎక్స్.?ట్రా|సిబ్బంది|சாமக்ரி|கூடுதல்|பொருட்கள்|ஊழியர்|অতিরিক্ত|সামগ্রী|વધારાના|સામગ્રી|ਵਾਧੂ|ਸਮੱਗਰੀ|അധിക|സാമഗ്രി",
    "advance_amount": r"\b(?:advance|deposit|upfront|up front|prepayment)\b|एडवांस|अग्रिम|अडवांस|ಮುಂಗಡ|ಅಡ್ವಾನ್ಸ್|ಅಡ್ವಾನ್ಸ|అడ్వాన్స్|ముందస్తు|அட்வான்ஸ்|முன்பணம்|অগ্রিম|আগাম|એડવાન્સ|અગાઉથી|ਪੇਸ਼ਗੀ|ਅਡਵਾਂਸ|അഡ്വാൻസ്|മുൻകൂർ",
}
_FIELD_RE = {field: re.compile(pattern, re.I) for field, pattern in _ALIASES.items()}
_PER_PLATE = re.compile(r"\bper plate\b|प्रति प्लेट|ಪ್ರತಿ ಪ್ಲೇಟ್|ಪ್ಲೇಟಿಗೆ|ప్లేటుకు|ఒక్కో ప్లేట్|ஒரு தட்டு|தட்டுக்கு", re.I)
_TOTAL = re.compile(r"\b(?:in total|total|overall|all in)\b|कुल|ಒಟ್ಟು|మొత్తం|மொத்தம்|মোট|કુલ|ਕੁੱਲ|മൊത്തം", re.I)
_ZERO = re.compile(r"\b(?:none|nothing|free|included|no (?:extra )?(?:charge|charges|advance|deposit)|no transport|no delivery)\b|कोई.*नहीं|शामिल|मुफ्त|एडवांस नहीं|ಮುಂಗಡ ಬೇಡ|ಸೇರಿದೆ|ಉಚಿತ|అవసరం లేదు|ఉచితం|చార్జి లేదు|சேர்ந்துள்ளது|இலவசம்|முன்பணம் இல்லை", re.I)
_DENIAL = re.compile(r"\b(?:not|wrong|incorrect|instead|isn't|is not)\b|गलत|नहीं|ತಪ್ಪು|కాదు|தவறு|இல்லை", re.I)
_UNCERTAIN = re.compile(r"\b(?:about|around|approximately|roughly|maybe|perhaps|might|if|unless)\b|लगभग|शायद|ಸುಮಾರು|ಬಹುಶಃ|సుమారు|ஒருவேளை", re.I)
_OPEN_QUESTION = re.compile(r"how much|what.*(?:charge|advance|deposit|price)|कितन|ಎಷ್ಟು|ఎంత|எவ்வளவு", re.I)
_BARE_NO = re.compile(r"\s*(?:no|none|nothing|नहीं|नही|ಇಲ್ಲ|లేదు|இல்லை)[.!।]?\s*", re.I)
_EXCLUDED = re.compile(r"\b(?:not included|excluded|not free)\b|शामिल नहीं|சேர்க்கப்படவில்லை", re.I)
_CLAUSE = re.compile(r"[;।\n]|(?<!\d)[,.]|[,.](?!\d)|\s+(?:and|और|ಹಾಗೂ|ಮತ್ತು|మరియు|அடுத்து|மற்றும்)\s+", re.I)


def amount_mentions(text: str) -> list[tuple[int, int, int]]:
    """Rupee digits, including Indian grouping and scaled digits; never harvest time/weight/counts."""
    text = unicodedata.normalize("NFKC", text)
    mentions = []
    for match in _NUMBER.finditer(text):
        end = match.end()
        if text[end:end + 1] in (":", "/", "%", "-") or _NON_MONEY_UNIT.match(text[end:]) or _MONTH.match(text[end:].lstrip()):
            continue
        # The regex's whitespace may consume the gap before a non-money unit.
        if _NON_MONEY_UNIT.match(text[match.end("number"):]) and not match.group("scale"):
            continue
        try:
            grouped = match.group("number").rstrip(",")
            groups = grouped.split(",")
            if len(groups) > 1 and not (1 <= len(groups[0]) <= 3 and len(groups[-1]) == 3
                    and (all(len(g) == 3 for g in groups[1:]) or all(len(g) == 2 for g in groups[1:-1]))):
                continue
            raw = grouped.replace(",", "")
            raw = "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in raw)
            value = Decimal(raw) * _SCALES.get((match.group("scale") or "").lower(), 1)
            if value == value.to_integral_value() and 0 <= value <= MAX_AMOUNT:
                numeric_end = match.end("scale") if match.group("scale") else match.end("number")
                mentions.append((match.start(), numeric_end, int(value)))
        except (ValueError, InvalidOperation):
            continue
    return mentions


def numbers_heard(text: str) -> set[int]:
    return {amount for _, _, amount in amount_mentions(text)}


def _labels(text: str) -> dict[str, list[tuple[int, int]]]:
    labels = {field: [(m.start(), m.end()) for m in regex.finditer(text)] for field, regex in _FIELD_RE.items()}
    labels = {field: spans for field, spans in labels.items() if spans}
    # 'transport cost' is transport, not an independent base-price assertion.
    if "price_amount" in labels and len(labels) > 1:
        specific = [span for field, spans in labels.items() if field != "price_amount" for span in spans]
        labels["price_amount"] = [span for span in labels["price_amount"] if all(
            _distance(span, s) > 12 or re.search(r"\d", text[min(span[1], s[1]):max(span[0], s[0])])
            for s in specific)]
        if not labels["price_amount"]:
            del labels["price_amount"]
    return labels


def _distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(a[0] - b[1], b[0] - a[1], 0)


def _unit(text: str, category: str) -> str | None:
    if _PER_PLATE.search(text):
        return "per_plate"
    if _TOTAL.search(text) or category != "caterer":
        return "total"
    return None


def _assertions(text: str, question: str, category: str) -> dict[str, tuple[int, str | None]]:
    """One unambiguous nearest amount per labelled clause, or an answer to a single-charge question."""
    found: dict[str, tuple[int, str | None]] = {}
    if _UNCERTAIN.search(text):
        return found
    conflicts = set()
    for clause in _CLAUSE.split(text):
        labels = _labels(clause)
        mentions = amount_mentions(clause)
        if len(labels) != 1:
            # A multi-charge clause without clear separators is not a licence to guess
            # which nearby number belongs to which charge. Ask one charge at a time.
            continue
        if not mentions and _ZERO.search(clause) and not _EXCLUDED.search(clause):
            # Zero has to be explicitly stated too; it is not the default for omitted extras.
            if len(labels) == 1:
                field = next(iter(labels))
                found[field] = (0, _unit(clause, category) if field == "price_amount" else None)
            continue
        if _DENIAL.search(clause):
            continue  # corrections containing multiple contradictory numbers need a clearer repeat
        for field, spans in labels.items():
            if not mentions:
                continue
            distances = []
            for start, end, amount in mentions:
                scoped = [span for span in spans if not _DATE_CONTEXT.search(clause[min(span[1], end):max(span[0], start)])]
                if scoped:
                    distances.append((min(_distance(span, (start, end)) for span in scoped), amount))
            if not distances:
                continue
            nearest = min(d for d, _ in distances)
            amounts = {amount for distance, amount in distances if distance == nearest}
            if len(amounts) != 1:
                conflicts.add(field)
                continue
            amount = amounts.pop()
            value = (amount, (_unit(clause, category) or _unit(question, category)) if field == "price_amount" else None)
            if field in found and found[field] != value:
                conflicts.add(field)
            found[field] = value
    if not found and not _labels(text) and not _MONTH.search(text):
        question_labels = _labels(question)
        mentions = amount_mentions(text)
        if len(question_labels) == 1 and (not _DENIAL.search(text) or _BARE_NO.fullmatch(text)) and not _EXCLUDED.search(text):
            field = next(iter(question_labels))
            values = {amount for _, _, amount in mentions}
            if len(values) == 1:
                found[field] = (values.pop(), _unit(question, category) if field == "price_amount" else None)
            elif not mentions and (_ZERO.search(text) or (_BARE_NO.fullmatch(text) and _OPEN_QUESTION.search(question))):
                found[field] = (0, _unit(question, category) if field == "price_amount" else None)
    return {field: value for field, value in found.items() if field not in conflicts}


def audit(quote: dict[str, Any], dialogue: list[dict[str, str]], category: str, *, call_id: str,
          previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist field-bound proof generated by code. Changed amounts/units cannot inherit old proof."""
    evidence: dict[str, dict[str, Any]] = {}
    old = (previous or {}).get("amount_checks", {})
    gst_statement = old.get("gst_statement") if old.get("version") == VERSION and old.get("category") == category and (previous or {}).get("gst_included") is True else None
    for field in FIELDS:
        amount = quote.get(field)
        unit = quote.get("price_unit") if field == "price_amount" else None
        proof = old.get("fields", {}).get(field, {}) if old.get("version") == VERSION and old.get("category") == category else {}
        inherited = proof.get("statements", []) if proof.get("amount") == amount and proof.get("unit") == unit and field not in (previous or {}).get("unverified", []) else []
        evidence[field] = {"amount": amount, "unit": unit, "statements": list(inherited[-2:])}
    question, turn = "", 0
    for message in dialogue:
        if message["role"] in ("assistant", "bandobast"):
            question = message["text"] if "?" in message["text"] or "？" in message["text"] else ""
            continue
        if message["role"] not in ("user", "vendor_or_family"):
            continue
        turn += 1
        for clause in _CLAUSE.split(message["text"]):
            ambiguous = _labels(clause)
            if len(ambiguous) > 1:
                for field in ambiguous:
                    evidence[field]["statements"] = []
        if _GST.search(message["text"]) or _GST.search(question):
            if _EXCLUDED.search(message["text"]) or _BARE_NO.fullmatch(message["text"]) or _DENIAL.search(message["text"]) or _UNCERTAIN.search(message["text"]):
                gst_statement = None
            elif (_GST.search(message["text"]) and _INCLUDED.search(message["text"])) or (_GST.search(question) and _INCLUDED.search(question) and _YES.fullmatch(message["text"])):
                gst_statement = {"id": f"{call_id}:{turn}", "text": message["text"][:600]}
        # Rejecting a read-back, or explicitly contradicting a charge, invalidates its old proof.
        rejected = _BARE_NO.fullmatch(message["text"]) and not _OPEN_QUESTION.search(question)
        if rejected or _UNCERTAIN.search(message["text"]) or (_DENIAL.search(message["text"]) and not _ZERO.search(message["text"])):
            affected = _labels(message["text"]) or _labels(question)
            for field in affected:
                evidence[field]["statements"] = []
        assertions = _assertions(message["text"], question, category)
        for field, (amount, unit) in assertions.items():
            proof = evidence[field]
            if type(quote.get(field)) is int and amount == quote[field] and unit == proof["unit"]:
                statement_id = f"{call_id}:{turn}"
                if not any(s["id"] == statement_id for s in proof["statements"]):
                    proof["statements"].append({"id": statement_id, "text": message["text"][:600],
                        "question": question[:600], "scope": "vendor_named_charge" if field in _labels(message["text"]) else "single_charge_answer"})
                    proof["statements"] = proof["statements"][-2:]
            elif field in evidence:
                # A later correction invalidates earlier agreement with this proposed amount.
                proof["statements"] = []
        question = ""  # a response cannot be scoped to an old unanswered question twice
    return {"version": VERSION, "category": category, "fields": evidence, "gst_statement": gst_statement}


def value_problems(quote: dict[str, Any], category: str) -> list[str]:
    problems = []
    for field in (*FIELDS, "initial_price_amount"):
        value = quote.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_AMOUNT):
            problems.append(f"{field}: use a non-negative whole rupee amount, at most {MAX_AMOUNT}")
    unit = quote.get("price_unit")
    if unit not in ("per_plate", "total") or (unit == "per_plate" and category != "caterer"):
        problems.append("price_unit: per plate is only valid for catering; otherwise quote a total")
    if type(quote.get("available")) is not bool:
        problems.append("availability must be explicitly true or false")
    if quote.get("gst_included") is not None and type(quote["gst_included"]) is not bool:
        problems.append("GST inclusion must be true or false")
    return problems


def rechecks(quote: dict[str, Any], brief: dict[str, Any], category: str) -> dict[str, str]:
    """Suspicious charges need a second vendor statement; price caps are not automatic rejection."""
    if value_problems(quote, category) or type(quote.get("price_amount")) is not int:
        return {}
    base = quote["price_amount"] * brief.get("guests", 0) if quote.get("price_unit") == "per_plate" else quote["price_amount"]
    total = base + (quote.get("transport_charge") or 0) + (quote.get("extra_charges_amount") or 0)
    unusual = {}
    limit = 1500 if category == "caterer" and quote.get("price_unit") == "per_plate" else BASE_RECHECK[category]
    budget = (brief.get("budget") or {}).get(category)
    if quote["price_amount"] > limit or (budget and base > budget * 2):
        unusual["price_amount"] = "unusually high base price; repeat the amount and pricing unit"
    previous_price = quote.get("price_change_from")
    if type(previous_price) is int and previous_price > 0 and (quote["price_amount"] > previous_price * 2 or quote["price_amount"] < previous_price * 0.2):
        unusual["price_amount"] = "large price change; repeat the new amount and pricing unit separately"
    transport = quote.get("transport_charge") or 0
    if transport > 10_000 or (transport > 0 and transport > base * 0.5):
        unusual["transport_charge"] = "unusually high transport charge; repeat it separately"
    extras = quote.get("extra_charges_amount") or 0
    if extras > 20_000 or (extras > 0 and extras > base * 0.5):
        unusual["extra_charges_amount"] = "unusually high extras; repeat the charge and amount separately"
    if (quote.get("advance_amount") or 0) > total * 0.5:
        unusual["advance_amount"] = "advance exceeds half the total; repeat the amount separately"
    return unusual


def issues(quote: dict[str, Any], brief: dict[str, Any], category: str) -> list[dict[str, str]]:
    """The same checks guard live saving, ranking, bookings and revised-price approval."""
    if quote.get("available") is False:
        return []
    if category not in BASE_RECHECK:
        return [{"field": "quote", "reason": "unknown vendor category; prices cannot be verified"}]
    issues = [{"field": "quote", "reason": p} for p in value_problems(quote, category)]
    checks = quote.get("amount_checks", {})
    fields = checks.get("fields", {}) if checks.get("version") == VERSION and checks.get("category") == category else {}
    unusual = rechecks(quote, brief, category)
    for field in FIELDS:
        amount = quote.get(field)
        proof = fields.get(field, {})
        statements = proof.get("statements", []) if proof.get("amount") == amount and proof.get("unit") == (quote.get("price_unit") if field == "price_amount" else None) else []
        count = len({s.get("id") for s in statements if isinstance(s, dict) and s.get("id") and s.get("text")})
        if amount is None or count == 0 or field in quote.get("unverified", []):
            issues.append({"field": field, "reason": f"{LABELS[field]} not verified against the vendor's own answer for this charge"})
        elif field in unusual and count < 2:
            issues.append({"field": field, "reason": unusual[field]})
    if not value_problems(quote, category) and type(quote.get("price_amount")) is int:
        base = quote["price_amount"] * brief.get("guests", 0) if quote.get("price_unit") == "per_plate" else quote["price_amount"]
        total = base + (quote.get("transport_charge") or 0) + (quote.get("extra_charges_amount") or 0)
        if (quote.get("advance_amount") or 0) > total:
            issues.append({"field": "advance_amount", "reason": "advance cannot exceed the entire total; ask for a correction"})
    if category == "caterer" and (quote.get("gst_included") is not True or not checks.get("gst_statement")):
        issues.append({"field": "gst_included", "reason": "ask for the actual GST-inclusive base price; an unknown tax must not be treated as zero"})
    return issues
