"""Synthetic vendor statements for fixtures, not a bypass in production code."""

import price_safety


def checked_quote(data, category):
    quote = dict(data)
    quote.setdefault("extra_charges_amount", 0)
    quote.setdefault("advance_amount", 0)
    if category == "caterer":
        quote.setdefault("gst_included", True)
    unit = "per plate" if quote.get("price_unit") == "per_plate" else "in total"
    dialogue = [{"role": "user", "text": f"{price_safety.LABELS[field]} {quote.get(field)} rupees {unit if field == 'price_amount' else ''}"}
                for field in price_safety.FIELDS]
    if category == "caterer" and quote.get("gst_included") is True:
        dialogue.append({"role": "user", "text": "GST is included"})
    quote["amount_checks"] = price_safety.audit(quote, dialogue, category, call_id="synthetic-fixture")
    if quote.get("initial_price_amount") is not None:
        quote["initial_price_verified"] = True
        opening = {k: v for k, v in quote.items() if k not in ("opening_quote", "initial_price_amount", "initial_price_verified", "amount_checks")}
        opening["price_amount"] = quote["initial_price_amount"]
        opening["price_unit"] = quote.get("initial_price_unit", quote["price_unit"])
        opening_unit = "per plate" if opening["price_unit"] == "per_plate" else "in total"
        opening_speech = [{"role": "user", "text": f"{price_safety.LABELS[field]} {opening.get(field)} rupees {opening_unit if field == 'price_amount' else ''}"}
                         for field in price_safety.FIELDS]
        if category == "caterer":
            opening_speech.append({"role": "user", "text": "GST included"})
        opening["amount_checks"] = price_safety.audit(opening, opening_speech, category, call_id="synthetic-opening")
        quote["opening_quote"] = opening
    return quote
