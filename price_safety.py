"""Pure price checks. Only vendor speech, in the correct charge context, is evidence.

Unknown wording is deliberately unverified, never a reason to guess. Prompts can ask a clearer
question, but they cannot turn a number, a 'yes', or a model-generated read-back into proof.
"""

import os
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
_NON_MONEY_UNIT = re.compile(r"^\s*(?:%|(?:guests?|people|persons?|plates?|chairs?|hours?|minutes?|days?|kg|kilos?|kilograms?|am|pm)\b|मेहमान|गेस्ट|लोग|घंट|किलो|बजे|நபர்கள்|பேர்|மணிக்கு|ಗಂಟೆ|ಜನ|మంది|గంటల)", re.I)
_MONTH = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|date|muhurat|guests?|people|hours?|minutes?)\b|नवंबर|नवम्बर|अक्टूबर|तारीख|मेहमान|गेस्ट|ಲೋಗ|ದಿನಾಂಕ|నవంబర్|తేదీ|தேதி", re.I)
_DATE_CONTEXT = re.compile(r"\b(?:date|time|muhurat|arrive|arrival|at)\b|तारीख|बजे|ದಿನಾಂಕ|తేదీ|தேதி", re.I)
_GST = re.compile(r"\bgst\b|जीएसटी|ಜಿಎಸ್.?ಟಿ|జీఎస్టీ|ஜிஎஸ்டி", re.I)
# "GST अलग से लगेगा, 5%": the vendor's own rate. Code adds it to the total; the model never computes tax.
GST_RATES = (5, 12, 18, 28)
_GST_PERCENT = re.compile(r"(?<!\d)(\d{1,2})\s*(?:%|percent|per cent|प्रतिशत|परसेंट|पर्सेंट|पर्सेन्ट)", re.I)
_INCLUDED = re.compile(r"\b(?:included|including|inclusive|includes)\b|शामिल|इनक्लूडेड|इंक्लूडेड|सहित|समेत|ಸೇರಿದೆ|సహా|కలిపి|చేర్చ|சேர்ந்த|உட்பட", re.I)
_YES = re.compile(r"\s*(?:yes(?: sir)?|yes included|haan|हाँ(?: जी)?|जी हाँ|ಹೌದು(?: ಸರ್)?|అవును|ஆம்|ஆமாம்|included|शामिल है)[.!।]?\s*", re.I)

_ALIASES = {
    "price_amount": r"\b(?:base price|price|rate|dakshina|fee|cost|total|per plate)\b|दक्षिणा|कीमत|दाम|रेट|कुल|टोटल|प्रति प्लेट|पर प्लेट|ಬೆಲೆ|ದರ|ದಕ್ಷಿಣೆ|ಒಟ್ಟು|ಪ್ರತಿ ಪ್ಲೇಟ್|ಪ್ಲೇಟ್‌?ಗೆ|ಪ್ರತಿ ತಟ್ಟೆ|ತಟ್ಟೆಗೆ|ధర|రేటు|దక్షిణ|మొత్తం|ప్లేటుకు|விலை|தட்சணை|மொத்தம்|ஒரு தட்டு|দাম|দক্ষিণা|মোট|કિંમત|દક્ષિણા|કુલ|ਕੀਮਤ|ਕੁੱਲ|വില|ദക്ഷിണ|മൊത്തം",
    "transport_charge": r"\b(?:transport|travel|delivery|transportation)\b|ट्रांसपोर्ट|परिवहन|डिलीवरी|यात्रा|आने जाने|ಸಾರಿಗೆ|ಟ್ರಾನ್ಸ್.?ಪೋರ್ಟ್|ಡೆಲಿವರಿ|ಪ್ರಯಾಣ|రవాణా|ట్రాన్స్.?పోర్ట్|డెలివరీ|ప్రయాణ|போக்குவரத்து|டெலிவரி|பயண|পরিবহন|ডেলিভারি|ડિલિવરી|પરિવહન|ਡਿਲਿਵਰੀ|യാത്ര|ഡെലിവറി",
    "extra_charges_amount": r"\b(?:extras?|other charges|additional charges|samagri|materials?|staff)\b|अतिरिक्त|सामग्री|एक्स्ट्रा|स्टाफ|ಸಾಮಗ್ರಿ|ಹೆಚ್ಚುವರಿ|ಎಕ್ಸ್ಟ್ರಾ|ಸಿಬ್ಬಂದಿ|సామాగ్రి|సామగ్రి|అదనపు|ఎక్స్.?ట్రా|సిబ్బంది|சாமக்ரி|கூடுதல்|பொருட்கள்|ஊழியர்|অতিরিক্ত|সামগ্রী|વધારાના|સામગ્રી|ਵਾਧੂ|ਸਮੱਗਰੀ|അധിക|സാമഗ്രി",
    "advance_amount": r"\b(?:advance|deposit|upfront|up front|prepayment)\b|एडवांस|अग्रिम|अडवांस|ಮುಂಗಡ|ಅಡ್ವಾನ್ಸ್|ಅಡ್ವಾನ್ಸ|అడ్వాన్స్|ముందస్తు|அட்வான்ஸ்|முன்பணம்|অগ্রিম|আগাম|એડવાન્સ|અગાઉથી|ਪੇਸ਼ਗੀ|ਅਡਵਾਂਸ|അഡ്വാൻസ്|മുൻകൂർ",
}
_FIELD_RE = {field: re.compile(pattern, re.I) for field, pattern in _ALIASES.items()}
_EXTRA_MODIFIER = re.compile(r"extra|additional|अतिरिक्त|एक्स्ट्रा|ಹೆಚ್ಚುವರಿ|ಎಕ್ಸ್ಟ್ರಾ|అదనపు|ఎక్స్.?ట్రా|கூடுதல்|অতিরিক্ত|વધારાના|ਵਾਧੂ|അധിക", re.I)
_PER_PLATE = re.compile(r"\b(?:per|a|each|one) plate\b|प्रति प्लेट|पर प्लेट|एक प्लेट|प्लेट की|प्लेट का|प्लेट के|प्रति थाली|एक थाली|ಪ್ರತಿ ಪ್ಲೇಟ್|ಪ್ಲೇಟಿಗೆ|ಪ್ಲೇಟ್‌?ಗೆ|ಪ್ರತಿ ತಟ್ಟೆ|ತಟ್ಟೆಗೆ|ప్లేటుకు|ఒక్కో ప్లేట్|ஒரு தட்டு|தட்டுக்கு", re.I)
_TOTAL = re.compile(r"\b(?:in total|total|overall|all in)\b|कुल|टोटल|ಒಟ್ಟು|మొత్తం|மொத்தம்|মোট|કુલ|ਕੁੱਲ|മൊത്തം", re.I)
_ZERO = re.compile(r"\b(?:none|nothing|free|included|no (?:extra )?(?:charge|charges|advance|deposit)|no transport|no delivery)\b|कोई.*नहीं|कुछ नहीं|शामिल|मुफ्त|फ्री|ज़ीरो|जीरो|\bzero\b|एडवांस नहीं|ಮುಂಗಡ ಬೇಡ|ಸೇರಿದೆ|ಉಚಿತ|అవసరం లేదు|ఉచితం|చార్జి లేదు|சேர்ந்துள்ளது|இலவசம்|முன்பணம் இல்லை", re.I)
_DENIAL = re.compile(r"\b(?:not|wrong|incorrect|instead|isn't|is not)\b|गलत|नहीं|ತಪ್ಪು|కాదు|தவறு|இல்லை", re.I)
_UNCERTAIN = re.compile(r"\b(?:about|around|approximately|roughly|maybe|perhaps|might|if|unless)\b|लगभग|शायद|ಸುಮಾರು|ಬಹುಶಃ|సుమారు|ஒருவேளை", re.I)
_OPEN_QUESTION = re.compile(r"how much|what.*(?:charge|advance|deposit|price)|कितन|ಎಷ್ಟು|ఎంత|எவ்வளவு", re.I)
_BARE_NO = re.compile(r"\s*(?:no|none|nothing|नहीं|नही|नई|ना|ಇಲ್ಲ|లేదు|இல்லை)(?: जी| ji| sir| सर)?(?:,? (?:नहीं है|नही है|कुछ नहीं))?[.!।]?\s*", re.I)
# "Is there any transport charge?" asks whether a charge exists, so a bare "no" means zero.
# A read-back ("so 500 for transport, right?") contains an amount, and there "no" means "wrong".
_EXISTENCE_QUESTION = re.compile(r"\bany\b|कोई|ಏನಾದರೂ|ಯಾವುದಾದರೂ|ఏదైనా|ஏதாவது", re.I)
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
    # 'extra transport charge' / 'यात्रा के 500 रुपये अतिरिक्त' is one transport charge, not two charges:
    # with a single amount in the clause, a bare "extra" can only describe the transport.
    if "transport_charge" in labels and "extra_charges_amount" in labels:
        single_amount = len(amount_mentions(text)) <= 1
        labels["extra_charges_amount"] = [span for span in labels["extra_charges_amount"] if not (
            _EXTRA_MODIFIER.fullmatch(text[span[0]:span[1]])
            and (single_amount or any(_distance(span, t) <= 2 for t in labels["transport_charge"])))]
        if not labels["extra_charges_amount"]:
            del labels["extra_charges_amount"]
    return labels


def _distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(a[0] - b[1], b[0] - a[1], 0)


def _unit(text: str, category: str) -> str | None:
    if _PER_PLATE.search(text):
        return "per_plate"
    if _TOTAL.search(text) or category != "caterer":
        return "total"
    return None


# "सब 13000 में" / "उसी में" / "all included" to "any transport or extras, or is everything in 13000?"
_ALL_IN = re.compile(r"(?:में|मे)(?:\s*(?:ही|सब|शामिल))?\s*[।.!]?\s*$|शामिल|उसी में|इसी में|सब कुछ|\ball (?:in|included)\b|\bincluded\b", re.I)
_EXTRAS = ("transport_charge", "extra_charges_amount")


def _all_included(text: str, question: str) -> list[str]:
    """Charges the vendor just said are included in the price (so 0 extra), when we asked exactly that."""
    asked = [field for field in _labels(question) if field in _EXTRAS]
    if "extra_charges_amount" not in asked and re.search(r"एक्स्ट्रा|\bextra\b|अतिरिक्त|स्टाफ|\bstaff\b", question, re.I):
        asked.append("extra_charges_amount")  # "transport या कोई एक्स्ट्रा चार्ज" labels only transport
    if not asked or not _INCLUDED.search(question) or _EXCLUDED.search(text) or _UNCERTAIN.search(text):
        return []
    if len({amount for _, _, amount in amount_mentions(text)} - {amount for _, _, amount in amount_mentions(question)}) > 0:
        return []  # a new number is a charge, not "it's all in"
    return asked if _ALL_IN.search(text) else []


# Costs billed later at actual cost ("सामग्री का bill बाद में add कर देंगे"): no fixed amount, and that's the answer.
_AT_ACTUALS = re.compile(r"बाद में.{0,25}(?:bill|बिल|add|ऐड|जोड़)|(?:bill|बिल).{0,25}(?:बाद में|add|ऐड|दे देंगे)|"
                         r"\bat actuals?\b|\bactual (?:cost|bill)\b|एक्चुअल|जितना (?:लगेगा|खर्च)|जो (?:भी )?(?:लगेगा|खर्च)|"
                         r"हिसाब से (?:लगेगा|होगा|देंगे|charge)|\bdepends?\b|डिपेंड", re.I)


# "हाँ हाँ, सही है" to our read-back. Short, agreeing, no new number, no "नहीं".
_AGREE = re.compile(r"हाँ|हां|सही|ठीक|बिल्कुल|\byes\b|\bhaan\b|\bcorrect\b|\bright\b|ಹೌದು|అవును|ஆம்", re.I)


_AGREES_TO_OFFER = re.compile(r"हो जाएगा|हो जायेगा|कर देंगे|चलेगा|ठीक है|डन|\bdone\b|\bok(?:ay)?\b|ओके|\bfine\b", re.I)


def _confirms_readback(text: str) -> bool:
    return (len(text.split()) <= 6 and bool(_AGREE.search(text)) and not _DENIAL.search(text)
            and not amount_mentions(text) and not _UNCERTAIN.search(text))


def _keep_statements(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last two, but never drop the vendor's own statement in favour of read-back confirmations."""
    last = statements[-2:]
    if any(s.get("scope") != "confirmed_readback" for s in last):
        return last
    own = [s for s in statements if s.get("scope") != "confirmed_readback"]
    return ([own[-1]] if own else []) + statements[-1:]


def _asks_if_charge_exists(question: str) -> bool:
    return bool(_EXISTENCE_QUESTION.search(question)) and not amount_mentions(question)


# "नहीं बोला तो नहीं है" / "no, nothing like that" to "is there any extra charge?". Not "नहीं पता" (don't know).
_LEADING_NO = re.compile(r"\s*(?:no|nope|नहीं|नही|नई|ना)(?=[\s,.।!?]|$)", re.I)  # \b fails after Devanagari marks
_DONT_KNOW = re.compile(r"पता|मालूम|\b(?:know|sure)\b", re.I)


def _no_to_existence(text: str, question: str) -> bool:
    return (_asks_if_charge_exists(question) and bool(_LEADING_NO.match(text)) and not amount_mentions(text)
            and not _DONT_KNOW.search(text) and not _EXCLUDED.search(text) and not _UNCERTAIN.search(text))


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
    money = " ".join(clause for clause in _CLAUSE.split(text) if amount_mentions(clause))  # "बोले तो 1200। हाँ, 1 minute।"
    if not found and not _labels(text) and not _MONTH.search(money):
        question_labels = _labels(question)
        if _GST.search(question) or _GST.search(text):
            question_labels.pop("price_amount", None)  # "GST आपके रेट में शामिल है?" asks about GST, not the rate
        mentions = amount_mentions(text)
        says_zero = (bool(_ZERO.search(text)) and not mentions) or _no_to_existence(text, question)  # "नहीं है, कोई charge नहीं है"
        if len(question_labels) == 1 and (not _DENIAL.search(text) or _BARE_NO.fullmatch(text) or says_zero) and not _EXCLUDED.search(text):
            field = next(iter(question_labels))
            values = {amount for _, _, amount in mentions}
            if len(values) == 1:
                found[field] = (values.pop(), _unit(question, category) if field == "price_amount" else None)
            elif field != "price_amount" and not mentions and (_ZERO.search(text) or _no_to_existence(text, question)
                                   or (_BARE_NO.fullmatch(text) and (_OPEN_QUESTION.search(question) or _asks_if_charge_exists(question)))):
                found[field] = (0, _unit(question, category) if field == "price_amount" else None)
    return {field: value for field, value in found.items() if field not in conflicts}


def stated(dialogue: list[dict[str, str]], category: str) -> dict[str, tuple[int, str | None]]:
    """The last amount (and unit) the vendor clearly stated for each charge, read the same way audit reads it.

    Used only to fill a charge the model left empty; audit still has to verify whatever is saved.
    """
    found: dict[str, tuple[int, str | None]] = {}
    question = ""
    for message in dialogue:
        if message["role"] in ("assistant", "bandobast"):
            question = message["text"] if "?" in message["text"] or "？" in message["text"] else ""
            continue
        if message["role"] not in ("user", "vendor_or_family"):
            continue
        found.update(_assertions(message["text"], question, category))
        for field in _all_included(message["text"], question):
            found[field] = (0, None)
        question = ""
    return found


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
    their_numbers: set[int] = set()  # every amount the vendor has said so far, in any words
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
        percent = _GST_PERCENT.search(message["text"])
        if (_GST.search(message["text"]) or _GST.search(question)) and percent and int(percent.group(1)) in GST_RATES \
                and not _UNCERTAIN.search(message["text"]) and not (_INCLUDED.search(message["text"]) and not _EXCLUDED.search(message["text"])
                                                                 and not _DENIAL.search(message["text"])):  # "नहीं शामिल, 28% अलग"
            gst_statement = {"id": f"{call_id}:{turn}", "text": message["text"][:600], "percent": int(percent.group(1))}
        elif _GST.search(message["text"]) or _GST.search(question):
            if _EXCLUDED.search(message["text"]) or _BARE_NO.fullmatch(message["text"]) or _DENIAL.search(message["text"]) or _UNCERTAIN.search(message["text"]):
                gst_statement = None
            elif (_GST.search(message["text"]) and _INCLUDED.search(message["text"])) or (_GST.search(question) and _INCLUDED.search(question) and _YES.fullmatch(message["text"])):
                gst_statement = {"id": f"{call_id}:{turn}", "text": message["text"][:600]}
        # Rejecting a read-back, or explicitly contradicting a charge, invalidates its old proof.
        rejected = _BARE_NO.fullmatch(message["text"]) and not _OPEN_QUESTION.search(question) and not _asks_if_charge_exists(question)
        denied = _DENIAL.search(message["text"]) and not _ZERO.search(message["text"]) and not _no_to_existence(message["text"], question)
        if (rejected or _UNCERTAIN.search(message["text"]) or denied) and not (_GST.search(message["text"]) or _GST.search(question)):
            # "नहीं" to "GST आपके रेट में शामिल है?" is about GST, not the rate. A no cancels the charges it names,
            # or those our read-back stated; a question with no amount in it has nothing to cancel.
            affected = _labels(message["text"]) or (_labels(question) if amount_mentions(question) else {})
            for field in affected:
                evidence[field]["statements"] = []
        assertions = _assertions(message["text"], question, category)
        for field in _all_included(message["text"], question):
            assertions[field] = (0, None)  # "13000 में": the number is the price it's included in, not this charge
        offered = {amount for _, _, amount in amount_mentions(question)}
        if (len(offered) == 1 and "price_amount" in _labels(question) and _confirms_readback(message["text"])
                and _AGREES_TO_OFFER.search(message["text"])):
            # "हाँ, हो जाएगा" to our single counter-offer "700 रुपये पर प्लेट कर दीजिए": they agreed to that rate.
            assertions.setdefault("price_amount", (next(iter(offered)), _unit(question, category)))
        elif amount_mentions(question) and _confirms_readback(message["text"]):
            # The vendor said yes to our read-back: every amount in it is confirmed, and so is GST if it was read
            # out with its percent. Nothing in it is asked again (owner's call for the demo: a yes to the full
            # read-back is the vendor's confirmation). A "no" to a read-back still cancels it (above).
            for field, (amount, unit) in _assertions(question, "", category).items():
                proof = evidence[field]
                if type(quote.get(field)) is int and amount == quote[field] and unit == proof["unit"]:
                    proof["statements"] = _keep_statements(proof["statements"] + [{
                        "id": f"{call_id}:{turn}", "text": message["text"][:600], "question": question[:600],
                        "scope": "readback_confirmed"}])
            percent = _GST_PERCENT.search(question)
            if _GST.search(question) and percent and int(percent.group(1)) in GST_RATES:
                gst_statement = {"id": f"{call_id}:{turn}", "text": message["text"][:600], "percent": int(percent.group(1))}
            elif _GST.search(question) and _INCLUDED.search(question) and not _DENIAL.search(question):
                gst_statement = {"id": f"{call_id}:{turn}", "text": message["text"][:600]}
        their_numbers.update(amount for _, _, amount in amount_mentions(message["text"]))
        for field, (amount, unit) in assertions.items():
            proof = evidence[field]
            if type(quote.get(field)) is int and amount == quote[field] and unit == proof["unit"]:
                statement_id = f"{call_id}:{turn}"
                if not any(s["id"] == statement_id for s in proof["statements"]):
                    proof["statements"].append({"id": statement_id, "text": message["text"][:600],
                        "question": question[:600], "scope": "vendor_named_charge" if field in _labels(message["text"]) else "single_charge_answer"})
                    proof["statements"] = _keep_statements(proof["statements"])
            elif field in evidence:
                # A later correction invalidates earlier agreement with this proposed amount.
                proof["statements"] = []
        question = ""  # a response cannot be scoped to an old unanswered question twice
    return {"version": VERSION, "category": category, "fields": evidence, "gst_statement": gst_statement,
            "vendor_numbers": sorted(their_numbers)}


# relaxed (default, the demo): an amount the vendor said anywhere counts. strict: each amount must be bound to
# the question it answered (the original guards; their tests run in strict mode).
RELAXED = os.environ.get("PRICE_CHECKS", "relaxed") != "strict"


def grounded(quote: dict[str, Any], checks: dict[str, Any], field: str) -> bool:
    """The amount is a number the vendor said themselves somewhere in the call (in any words, to any question).

    Owner's call for the demo (2026-11-09): binding each number to the exact question kept rejecting correct
    quotes over phrasing ("नहीं, 14000" to a counter-offer read as a rejection). The model still can't invent a
    number: it must be one the vendor actually said. Zero still needs the vendor's own "no"/"included".
    """
    amount = quote.get(field)
    if RELAXED and field == "extra_charges_amount" and amount == 0 and quote.get("extras_at_actuals") is True:
        return True  # "extra setup का जो लगेगा, वहीं decide होगा": no fixed extra, billed later; the note says what
    return RELAXED and type(amount) is int and amount > 0 and amount in set(checks.get("vendor_numbers") or [])


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
    if quote.get("gst_percent") is not None and quote.get("gst_percent") not in GST_RATES:
        problems.append("gst_percent: one of 5, 12, 18 or 28, as the vendor said, or null if GST is included")
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


def gst_settled(quote: dict[str, Any], statement: dict[str, Any] | None) -> bool:
    """GST included (said so), or extra at the percent the vendor said. An unknown tax is never zero."""
    if not statement:
        return False
    if quote.get("gst_included") is True:
        return "percent" not in statement  # they said GST is extra at some percent: not included
    return (quote.get("gst_included") is False and statement.get("percent") in GST_RATES
            and quote.get("gst_percent") == statement["percent"])


def _within_hard_limits(field: str, quote: dict[str, Any], brief: dict[str, Any], category: str) -> bool:
    amount = quote.get(field) or 0
    if field == "price_amount":
        limit = 1500 if category == "caterer" and quote.get("price_unit") == "per_plate" else BASE_RECHECK[category]
        return amount <= limit
    return amount <= {"transport_charge": 10_000, "extra_charges_amount": 20_000}.get(field, MAX_AMOUNT)


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
        if grounded(quote, checks, field) if checks.get("version") == VERSION and checks.get("category") == category else False:
            continue  # a number they said themselves: settled
        if amount is not None and field not in quote.get("unverified", []) and any(
                isinstance(s, dict) and s.get("scope") == "readback_confirmed" for s in statements):
            continue  # they said yes to our read-back of this amount: settled, never asked again
        own = {s.get("id") for s in statements if isinstance(s, dict) and s.get("id") and s.get("text") and s.get("scope") != "confirmed_readback"}
        confirmed = {s.get("id") for s in statements if isinstance(s, dict) and s.get("id") and s.get("scope") == "confirmed_readback"}
        confirmed = {f"{i}:confirmed" for i in confirmed}
        # A yes to our read-back never proves an amount on its own, and only settles amounts that are unusual
        # relative to the rest of the quote, not ones over the hard limits (those need the vendor to repeat them).
        count = len(own) + (len(confirmed) if own and _within_hard_limits(field, quote, brief, category) else 0)
        if amount is None or count == 0 or field in quote.get("unverified", []):
            issues.append({"field": field, "reason": f"{LABELS[field]} not verified against the vendor's own answer for this charge"})
        elif field in unusual and count < 2:
            issues.append({"field": field, "reason": unusual[field]})
    if not value_problems(quote, category) and type(quote.get("price_amount")) is int:
        base = quote["price_amount"] * brief.get("guests", 0) if quote.get("price_unit") == "per_plate" else quote["price_amount"]
        total = base + (quote.get("transport_charge") or 0) + (quote.get("extra_charges_amount") or 0)
        if (quote.get("advance_amount") or 0) > total:
            issues.append({"field": "advance_amount", "reason": "advance cannot exceed the entire total; ask for a correction"})
    if category == "caterer" and not gst_settled(quote, checks.get("gst_statement")):
        issues.append({"field": "gst_included", "reason": "ask whether GST is included, and if it is extra, what GST percent they charge"})
    return issues
