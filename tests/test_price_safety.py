"""Synthetic speech and DB tests for charge binding, anomalous prices and fail-closed approvals."""

import tempfile
import unittest
from pathlib import Path

import price_safety
import prompts
import rules
import store
import workflow
from conversation import SessionConfig, _plan, checked_quote, quote_problems
from price_fixture import checked_quote as fixture_quote
from confirmation_fixture import book_confirmed

BRIEF = prompts.SAMPLE_BRIEF
BASE = {"available": True, "price_amount": 450, "price_unit": "per_plate", "transport_charge": 1500,
        "extra_charges_amount": 0, "advance_amount": 5000, "gst_included": True}


def dialogue(quote, repeats=1):
    unit = "per plate" if quote["price_unit"] == "per_plate" else "in total"
    return [{"role": "user", "text": f"{price_safety.LABELS[field]} {quote[field]} rupees {unit if field == 'price_amount' else ''}"}
            for _ in range(repeats) for field in price_safety.FIELDS] + [{"role": "user", "text": "GST included"}]


def checked(quote=BASE, speech=None, category="caterer", previous=None):
    return checked_quote(quote, dialogue(quote) if speech is None else speech, BRIEF, category, "synthetic-call", previous)


class PriceSafetyTest(unittest.TestCase):
    def test_digits_grouping_and_indian_scales(self):
        self.assertEqual(price_safety.numbers_heard("₹2,50,000, 1.5 lakh, ५ हजार, ೫ ಲಕ್ಷ, ౫ వేలు"), {250000, 150000, 5000, 500000})

    def test_dates_times_counts_percentages_and_bad_grouping_are_not_money(self):
        for text in ("14:30", "2026-11-05", "05/11/2026", "40 guests", "2 kg", "5%", "1,00", "1000,2000", "5 pm", "15 November", "5 बजे"):
            self.assertEqual(price_safety.numbers_heard(text), set(), text)

    def test_normal_quote_has_field_bound_proof(self):
        quote = checked()
        self.assertEqual(price_safety.issues(quote, BRIEF, "caterer"), [])
        self.assertEqual(quote["amount_checks"]["fields"]["transport_charge"]["amount"], 1500)
        self.assertIn("1500", quote["amount_checks"]["fields"]["transport_charge"]["statements"][0]["text"])

    def test_swapping_advance_and_transport_fails_even_when_both_numbers_were_said(self):
        quote = checked({**BASE, "transport_charge": 5000, "advance_amount": 1500}, dialogue(BASE))
        self.assertIn("transport_charge", quote["unverified"])
        self.assertIn("advance_amount", quote["unverified"])

    def test_ambiguous_multi_charge_sentence_must_not_guess_by_nearest_number(self):
        quote = checked({**BASE, "transport_charge": 450}, [{"role": "user", "text": "Price 450 per plate transport 1500 advance 5000"}])
        self.assertIn("transport_charge", quote["unverified"])
        self.assertIn("price_amount", quote["unverified"])

    def test_guest_count_cannot_become_base_price(self):
        speech = [{"role": "assistant", "text": "What is your price per plate?"}, {"role": "user", "text": "40 guests"}]
        self.assertIn("price_amount", checked({**BASE, "price_amount": 40}, speech)["unverified"])

    def test_delivery_date_cannot_become_transport_charge(self):
        quote = checked({**BASE, "transport_charge": 15}, [{"role": "user", "text": "Delivery date 15"}])
        self.assertIn("transport_charge", quote["unverified"])

    def test_single_charge_question_scopes_numeric_answer(self):
        speech = [{"role": "assistant", "text": "How much is transport?"}, {"role": "user", "text": "1500 rupees"}]
        quote = checked(speech=speech)
        self.assertNotIn("transport_charge", quote["unverified"])
        self.assertIn("price_amount", quote["unverified"])

    def test_multiple_charge_readback_and_yes_do_not_verify_invented_amounts(self):
        speech = [{"role": "assistant", "text": "Price 450 per plate, transport 1500 and advance 5000, right?"}, {"role": "user", "text": "Yes"}]
        self.assertEqual(set(checked(speech=speech)["unverified"]), set(price_safety.FIELDS))

    def test_omitted_zero_is_not_verified_but_explicit_no_charge_is(self):
        speech = [{"role": "user", "text": "Transport included, no advance, no extra charges"}]
        quote = checked({**BASE, "transport_charge": 0, "advance_amount": 0}, speech)
        self.assertNotIn("transport_charge", quote["unverified"])
        self.assertNotIn("advance_amount", quote["unverified"])
        self.assertNotIn("extra_charges_amount", quote["unverified"])
        self.assertIn("extra_charges_amount", checked(speech=[])["unverified"])

    def test_not_included_must_not_become_zero(self):
        quote = checked({**BASE, "transport_charge": 0}, [{"role": "user", "text": "Transport is not included"}])
        self.assertIn("transport_charge", quote["unverified"])

    def test_catering_unit_must_be_grounded_not_invented(self):
        quote = checked(speech=[{"role": "user", "text": "Price 450 rupees"}])
        self.assertIn("price_amount", quote["unverified"])

    def test_non_catering_per_plate_is_rejected(self):
        self.assertTrue(quote_problems(BASE, "priest"))

    def test_typical_vendor_languages_are_supported_for_charge_labels(self):
        for text in ("एडवांस 5000", "ಮುಂಗಡ 5000", "అడ్వాన్స్ 5000", "முன்பணம் 5000", "advance 5000"):
            self.assertNotIn("advance_amount", checked(speech=[{"role": "user", "text": text}])["unverified"], text)

    def test_extra_transport_charge_is_one_transport_charge(self):
        for text in ("1500 रुपये अतिरिक्त परिवहन शुल्क।", "extra transport charge 1500 rupees", "Transport extra 1500",
                     "हाँ, यात्रा हेतु 1500 रुपये अतिरिक्त लगेंगे।"):
            quote = checked(speech=[{"role": "user", "text": text}])
            self.assertNotIn("transport_charge", quote["unverified"], text)
        # Two different charges in one clause are still not guessed apart.
        quote = checked({**BASE, "extra_charges_amount": 500}, [{"role": "user", "text": "transport and staff extra 1500 500"}])
        self.assertIn("transport_charge", quote["unverified"])

    def test_hindi_price_of_one_plate_is_per_plate(self):
        for text in ("एक प्लेट की कीमत 450 रुपये है", "हाँ, प्लेट की कीमत 450 रुपये है।", "price 450 rupees a plate"):
            speech = [m for m in dialogue(BASE) if not m["text"].startswith("base price")] + [{"role": "user", "text": text}]
            self.assertNotIn("price_amount", checked(speech=speech)["unverified"], text)

    def test_kannada_per_plate_wording(self):
        for text in ("ಪ್ರತಿ ತಟ್ಟೆಗೆ 450 ರೂಪಾಯಿ ಆಗುತ್ತದೆ", "ಪ್ರತಿ ಪ್ಲೇಟ್‌ಗೆ 450 ರೂಪಾಯಿ ಆಗುತ್ತದೆ", "ಪ್ಲೇಟ್ಗೆ 450 ರೂಪಾಯಿ"):
            speech = [m for m in dialogue(BASE) if not m["text"].startswith("base price")] + [{"role": "user", "text": text}]
            self.assertNotIn("price_amount", checked(speech=speech)["unverified"], text)

    def test_hinglish_charge_words(self):
        speech = [{"role": "user", "text": t} for t in (
            "पर प्लेट रेट 450 रुपये है", "ट्रांसपोर्ट 1500 रुपये", "एक्स्ट्रा चार्ज कुछ नहीं", "एडवांस 5000 रुपये", "जीएसटी इनक्लूडेड है")]
        quote = checked(speech=speech)
        self.assertEqual(quote["unverified"], [])
        self.assertTrue(quote["amount_checks"]["gst_statement"])

    def test_hinglish_zero_and_guest_count(self):
        quote = checked(speech=[m for m in dialogue(BASE) if not m["text"].startswith("other extra")]
                        + [{"role": "user", "text": "एक्स्ट्रा चार्ज ज़ीरो रुपये"}])
        self.assertNotIn("extra_charges_amount", quote["unverified"])
        self.assertEqual(price_safety.numbers_heard("40 गेस्ट के लिए पर प्लेट 450 रुपये"), {450})

    def test_gst_sahit_means_included(self):
        quote = checked(speech=dialogue(BASE)[:-1] + [{"role": "user", "text": "450 रुपये प्रति प्लेट GST सहित।"}])
        self.assertTrue(quote["amount_checks"]["gst_statement"])

    def test_callback_cannot_reassign_old_charge_evidence(self):
        previous = checked()
        quote = checked({**BASE, "transport_charge": 5000}, [], previous=previous)
        self.assertIn("transport_charge", quote["unverified"])

    def test_callback_must_not_inherit_a_flagged_field(self):
        previous = {**checked(), "unverified": ["transport_charge"]}
        self.assertIn("transport_charge", checked(speech=[], previous=previous)["unverified"])

    def test_latest_explicit_correction_invalidates_old_statement(self):
        speech = dialogue(BASE) + [{"role": "user", "text": "Transport is 2000 rupees"}]
        self.assertIn("transport_charge", checked(speech=speech)["unverified"])

    def test_approximate_or_conditional_amounts_need_a_firm_repeat(self):
        for text in ("Transport around 1500", "Transport 1500 if you pay today", "Transport maybe 1500"):
            quote = checked(speech=dialogue(BASE) + [{"role": "user", "text": text}])
            self.assertIn("transport_charge", quote["unverified"], text)

    def test_rejected_readback_cannot_preserve_old_proof(self):
        speech = dialogue(BASE) + [{"role": "assistant", "text": "The base price is 450 per plate, right?"}, {"role": "user", "text": "No"}]
        self.assertIn("price_amount", checked(speech=speech)["unverified"])

    def test_gst_true_needs_vendor_confirmation(self):
        quote = checked(speech=dialogue(BASE)[:-1])
        self.assertTrue(any(p["field"] == "gst_included" for p in price_safety.issues(quote, BRIEF, "caterer")))
        speech = dialogue(BASE)[:-1] + [{"role": "assistant", "text": "Is GST included?"}, {"role": "user", "text": "हाँ जी"}]
        self.assertFalse(any(p["field"] == "gst_included" for p in price_safety.issues(checked(speech=speech), BRIEF, "caterer")))

    def test_excluded_gst_blocks_booking_even_if_other_amounts_verified(self):
        self.assertTrue(any(p["field"] == "gst_included" for p in price_safety.issues(checked({**BASE, "gst_included": False}), BRIEF, "caterer")))

    def test_included_gst_does_not_verify_all_other_extras_as_zero(self):
        self.assertIn("extra_charges_amount", checked(speech=[{"role": "user", "text": "GST included"}])["unverified"])

    def test_unusual_figures_need_two_distinct_vendor_statements(self):
        large = {"available": True, "price_amount": 67000, "price_unit": "total", "transport_charge": 500000,
                 "extra_charges_amount": 5000, "advance_amount": 500000}
        once = checked(large, dialogue(large), "priest")
        self.assertEqual({p["field"] for p in price_safety.issues(once, BRIEF, "priest")}, {"price_amount", "transport_charge", "advance_amount"})
        twice = checked(large, dialogue(large, 2), "priest")
        self.assertEqual(price_safety.issues(twice, BRIEF, "priest"), [])

    def test_yes_to_large_readback_does_not_count_as_a_repeat(self):
        large = {**BASE, "transport_charge": 500000}
        speech = dialogue(large) + [{"role": "assistant", "text": "Transport is 500000, correct?"}, {"role": "user", "text": "Yes"}]
        self.assertTrue(any(p["field"] == "transport_charge" for p in price_safety.issues(checked(large, speech), BRIEF, "caterer")))

    def test_repeating_number_twice_in_one_utterance_is_not_two_confirmations(self):
        large = {**BASE, "transport_charge": 500000}
        speech = [{"role": "user", "text": "Transport 500000; transport 500000"}]
        proof = checked(large, speech)["amount_checks"]["fields"]["transport_charge"]
        self.assertEqual(len(proof["statements"]), 1)

    def test_invalid_numeric_types_and_negative_values_are_blocked(self):
        for amount in (True, -1, 1.5, "450", 10**30):
            quote = {**BASE, "transport_charge": amount}
            self.assertTrue(price_safety.value_problems(quote, "caterer"), amount)
            self.assertIsNone(rules.landed_cost(quote, BRIEF))

    def test_advance_larger_than_total_cannot_be_confirmed_away(self):
        quote = {**BASE, "advance_amount": 500000}
        problems = price_safety.issues(checked(quote, dialogue(quote, 2)), BRIEF, "caterer")
        self.assertTrue(any("entire total" in p["reason"] for p in problems))

    def test_model_cannot_inject_audit_metadata_or_opening_price(self):
        malicious = {**BASE, "amount_checks": checked()["amount_checks"], "unverified": [],
                     "initial_price_amount": 50000000, "initial_price_verified": True}
        quote = checked(malicious, [])
        self.assertEqual(set(quote["unverified"]), set(price_safety.FIELDS))
        self.assertNotIn("initial_price_amount", quote)
        self.assertIsNone(rules.savings(quote, BRIEF))

    def test_unverified_opening_price_cannot_produce_savings(self):
        quote = {**checked(), "initial_price_amount": 50000000}
        self.assertIsNone(rules.savings(quote, BRIEF))

    def test_savings_keep_the_original_pricing_unit(self):
        quote = fixture_quote({**BASE, "price_amount": 450, "initial_price_amount": 20000, "initial_price_unit": "total"}, "caterer")
        self.assertEqual(rules.savings(quote, BRIEF), 2000)

    def test_waived_transport_is_counted_without_inventing_base_price_savings(self):
        opening = fixture_quote(BASE, "caterer")
        final = fixture_quote({**BASE, "transport_charge": 0}, "caterer")
        final.update({"initial_price_amount": 450, "initial_price_verified": True, "opening_quote": opening})
        self.assertEqual(rules.savings(final, BRIEF), 1500)

    def test_negotiation_does_not_offer_negative_base_price(self):
        offer = {**BASE, "price_amount": 100, "transport_charge": 500000}
        self.assertFalse(rules.counter_offer("caterer", offer, BRIEF, round_no=1, competing_total=None)["negotiate"])


class PriceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "prices.db"
        self.brief = prompts.SAMPLE_NAAMKARAN
        self.event_id = store.save_event(self.brief)
        self.quote = fixture_quote({"available": True, "price_amount": 2400, "price_unit": "total",
            "transport_charge": 100, "extra_charges_amount": 0, "advance_amount": 500}, "cake")
        store.save_quote(self.event_id, "K01", None, self.quote)

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    def test_legacy_quote_without_audit_cannot_be_booked(self):
        legacy = {k: v for k, v in self.quote.items() if k != "amount_checks"}
        store.save_quote(self.event_id, "K01", None, legacy)
        with self.assertRaises(ValueError):
            workflow.book("cake", "K01")

    def test_unverified_extra_charge_blocks_booking_not_just_price(self):
        store.save_quote(self.event_id, "K01", None, {**self.quote, "unverified": ["transport_charge"]})
        with self.assertRaises(ValueError):
            workflow.book("cake", "K01")

    def test_rechecked_booked_charges_wait_for_family_approval(self):
        workflow.book("cake", "K01")
        corrected = fixture_quote({**self.quote, "transport_charge": 200}, "cake")
        result = workflow.record_quote(self.event_id, "K01", "synthetic-recheck", corrected)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["transport_charge"], 100)
        self.assertEqual(store.update(result["update_id"])["kind"], "quote")
        workflow.decide_update(self.event_id, result["update_id"], True)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["transport_charge"], 200)

    def test_blocked_correction_cannot_be_approved(self):
        workflow.book("cake", "K01")
        result = workflow.record_quote(self.event_id, "K01", "synthetic-recheck", {**self.quote, "transport_charge": 500000})
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, result["update_id"], True)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["transport_charge"], 100)

    def test_price_rechecking_does_not_replace_operational_notes(self):
        workflow.book("cake", "K01")
        current = {**self.quote, "includes_notes": "The agreed cake and delivery"}
        store.save_quote(self.event_id, "K01", None, current)
        result = workflow.record_quote(self.event_id, "K01", "synthetic-recheck", {**current, "includes_notes": "Model paraphrase"})
        self.assertIn("update_id", result)  # changed coverage must now be reviewed too
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["includes_notes"], current["includes_notes"])

    def test_changed_baseline_invalidates_old_correction(self):
        workflow.book("cake", "K01")
        result = workflow.record_quote(self.event_id, "K01", "synthetic-recheck", fixture_quote({**self.quote, "transport_charge": 200}, "cake"))
        store.save_quote(self.event_id, "K01", None, fixture_quote({**self.quote, "advance_amount": 600}, "cake"))
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, result["update_id"], True)

    def test_extreme_price_revision_needs_a_repeat(self):
        data = {"kind": "price", "price_amount": 15000, "price_unit": "total", "notes": "New cake rate"}
        with self.assertRaises(ValueError):
            workflow.record_vendor_update(self.event_id, "K01", data, "Base price 15000 in total")
        update_id = workflow.record_vendor_update(self.event_id, "K01", data, [
            {"role": "user", "text": "Base price 15000 in total"}, {"role": "user", "text": "Base price 15000 in total"}])
        self.assertEqual(store.update(update_id)["status"], "pending")

    def test_unsafe_standby_quote_is_not_activated(self):
        store.save_quote(self.event_id, "K03", None, self.quote)
        book_confirmed("cake", "K01")
        store.save_quote(self.event_id, "K03", None, {**self.quote, "unverified": ["advance_amount"]})
        checkin = store.checkins(self.event_id)[0]
        workflow.record_checkin(checkin["id"], "cancelled", None, "Cannot deliver")
        primary = next(b for b in store.bookings(self.event_id) if b["role"] == "primary")
        self.assertEqual(primary["vendor_id"], "K01")
        self.assertIn("No confirmed replacement", store.alerts(self.event_id)[0]["message"])

    def test_reconfirmation_mode_is_price_verification_not_booking_confirmation(self):
        cfg = SessionConfig.from_dict({"mode": "vendor_call", "vendor_id": "K01", "event_id": self.event_id, "reconfirm": "1"}, channel="app")
        prompt, tools, _, _ = _plan(cfg)
        self.assertIn("not a booking confirmation", prompt)
        self.assertIn("Never bargain", prompt)
        self.assertEqual([t.name for t in tools], ["record_quote", "end_call"])


class PlainNoToAnyChargeTest(unittest.TestCase):
    """From a live call: 'is there any transport charge?' 'no' was not taken as zero, so it was asked three times."""

    def test_plain_no_to_is_there_any_charge_means_zero(self):
        for question, answer, field in [
            ("ट्रांसपोर्ट या डिलीवरी का कोई एक्स्ट्रा चार्ज है क्या?", "नहीं।", "transport_charge"),
            ("डेकोरेशन में कोई और एक्स्ट्रा चार्ज है क्या, जैसे स्टाफ?", "नई।", "extra_charges_amount"),
            ("Is there any transport charge?", "No.", "transport_charge"),
            ("ट्रांसपोर्ट चार्ज कितना है, रुपये में बताइए?", "नहीं है, कोई charge नहीं है।", "transport_charge"),
        ]:
            self.assertEqual(price_safety._assertions(answer, question, "decorator"), {field: (0, None)}, answer)

    def test_no_to_a_read_back_is_still_a_correction_not_zero(self):
        self.assertEqual(price_safety._assertions("नहीं।", "तो 500 रुपये ट्रांसपोर्ट, सही?", "decorator"), {})
        self.assertEqual(price_safety._assertions("नहीं, 500 लगेगा", "ट्रांसपोर्ट का कोई चार्ज है क्या?", "decorator"), {})
        self.assertEqual(price_safety._assertions("हाँ है", "ट्रांसपोर्ट का कोई चार्ज है क्या?", "decorator"), {})

    def test_audit_accepts_the_plain_no(self):
        dialogue = [
            {"role": "assistant", "text": "ट्रांसपोर्ट का कोई चार्ज है क्या?"},
            {"role": "user", "text": "नहीं।"},
        ]
        checks = price_safety.audit({"transport_charge": 0}, dialogue, "decorator", call_id="t")
        self.assertEqual(len(checks["fields"]["transport_charge"]["statements"]), 1)


class FillStatedChargesTest(unittest.TestCase):
    """From a live call: the vendor said there was no extra charge, the model saved extras as null, nothing was saved."""

    DIALOGUE = [
        {"role": "bandobast", "text": "एक प्लेट का रेट क्या है?"},
        {"role": "vendor_or_family", "text": "450 रुपये।"},
        {"role": "bandobast", "text": "ट्रांसपोर्ट का कोई चार्ज है क्या?"},
        {"role": "vendor_or_family", "text": "नहीं।"},
        {"role": "bandobast", "text": "कोई एक्स्ट्रा स्टाफ या सामग्रियों का चार्ज है क्या?"},
        {"role": "vendor_or_family", "text": "नहीं नहीं, कोई extra staff नहीं चाहिए, हम staff लेके आएँगे।"},
        {"role": "bandobast", "text": "एडवांस कितना चाहिए?"},
        {"role": "vendor_or_family", "text": "हूँ। 5000"},
    ]

    def test_blank_charges_are_filled_from_the_vendors_words(self):
        from conversation import fill_stated_charges
        args = {"available": True, "price_amount": 450, "price_unit": "per_plate", "transport_charge": None,
                "extra_charges_amount": None, "advance_amount": 5000, "gst_included": True}
        fill_stated_charges(args, self.DIALOGUE, "caterer")
        self.assertEqual((args["transport_charge"], args["extra_charges_amount"]), (0, 0))
        self.assertEqual(quote_problems(args, "caterer"), [])

    def test_the_models_own_values_are_never_overwritten(self):
        from conversation import fill_stated_charges
        args = {"available": True, "price_amount": 450, "transport_charge": 300, "extra_charges_amount": None, "advance_amount": 5000}
        fill_stated_charges(args, self.DIALOGUE, "caterer")
        self.assertEqual(args["transport_charge"], 300)  # the audit decides whether 300 holds up

    def test_annoyed_no_to_is_there_any_charge_is_zero_but_dont_know_is_not(self):
        q = "स्टाफ या कोई और एक्स्ट्रा चार्ज है क्या?"
        self.assertEqual(price_safety._assertions("नहीं बोला तो नहीं है।", q, "caterer"), {"extra_charges_amount": (0, None)})
        self.assertEqual(price_safety._assertions("नहीं पता, बाद में बताऊँगा", q, "caterer"), {})


class GstPercentTest(unittest.TestCase):
    """From a live call: "GST अलग से लगेगा, 28%" led to "GST सहित रेट?" then "बिना GST के रेट?" loops."""

    QUOTE = {"available": True, "price_amount": 450, "price_unit": "per_plate", "transport_charge": 0,
             "extra_charges_amount": 0, "advance_amount": 5000}
    SPEECH = [
        {"role": "assistant", "text": "एक प्लेट का रेट क्या है?"}, {"role": "user", "text": "450 रुपये"},
        {"role": "assistant", "text": "ट्रांसपोर्ट का कोई चार्ज है क्या?"}, {"role": "user", "text": "नहीं"},
        {"role": "assistant", "text": "कोई एक्स्ट्रा चार्ज है क्या?"}, {"role": "user", "text": "नहीं"},
        {"role": "assistant", "text": "एडवांस कितना चाहिए?"}, {"role": "user", "text": "5000"},
        {"role": "assistant", "text": "GST इंक्लूडेड है क्या?"}, {"role": "user", "text": "नहीं, वो अलग से लगेगा, 5%."},
    ]

    def audited(self, quote):
        quote = dict(quote)
        quote["amount_checks"] = price_safety.audit(quote, self.SPEECH, "caterer", call_id="gst")
        return quote

    def test_extra_gst_at_the_stated_percent_is_settled_and_added_to_the_total(self):
        quote = self.audited({**self.QUOTE, "gst_included": False, "gst_percent": 5})
        self.assertEqual([p for p in price_safety.issues(quote, BRIEF, "caterer") if p["field"] == "gst_included"], [])
        self.assertEqual(rules.landed_cost(quote, BRIEF), 450 * 40 * 105 // 100)

    def test_a_different_or_missing_percent_is_not(self):
        for gst in ({"gst_included": False, "gst_percent": 18}, {"gst_included": False}, {"gst_included": True}):
            quote = self.audited({**self.QUOTE, **gst})
            self.assertTrue(any(p["field"] == "gst_included" for p in price_safety.issues(quote, BRIEF, "caterer")), gst)


class FlexibleExtrasTest(unittest.TestCase):
    """From a live call: "13000 में" and "materials we'll bill later" were both pushed to "so 0 rupees, right?"."""

    SPEECH = [
        {"role": "assistant", "text": "मेन डोर पर तोरण, गेंदे के फूल चाहिए, इसका रेट कितना होगा?"},
        {"role": "user", "text": "13,000।"},
        {"role": "assistant", "text": "क्या ट्रांसपोर्ट या कोई एक्स्ट्रा चार्ज है या सब 13,000 में शामिल है?"},
        {"role": "user", "text": "13000 में।"},
        {"role": "assistant", "text": "एडवांस कितना चाहिए?"},
        {"role": "user", "text": "advance चाहिएगा 5000।"},
        {"role": "assistant", "text": "कोई और एक्स्ट्रा चार्ज है क्या, जैसे स्टाफ या सामग्रियों का?"},
        {"role": "user", "text": "अगर हम कुछ सामग्री extra ख़रीदेंगे, तो वो bill में बाद में add कर देंगे।"},
    ]
    QUOTE = {"available": True, "price_amount": 13000, "price_unit": "total", "transport_charge": 0,
             "extra_charges_amount": 0, "advance_amount": 5000}

    def issues_for(self, quote):
        quote = dict(quote)
        quote["amount_checks"] = price_safety.audit(quote, self.SPEECH, "decorator", call_id="flex")
        return {p["field"] for p in price_safety.issues(quote, BRIEF, "decorator")}

    def test_all_in_and_billed_later_are_accepted(self):
        quote = {**self.QUOTE, "extras_at_actuals": True, "extra_charges_notes": "materials bought extra"}
        self.assertEqual(self.issues_for(quote), set())

    def test_all_in_answer_counts_for_transport(self):
        self.assertNotIn("transport_charge", self.issues_for(self.QUOTE))

    def test_billed_later_is_not_a_silent_zero_without_the_flag(self):
        speech = self.SPEECH[:2] + self.SPEECH[4:]  # without "13000 में", only "billed later" speaks to extras
        quote = dict(self.QUOTE)
        quote["amount_checks"] = price_safety.audit(quote, speech, "decorator", call_id="flex")
        self.assertIn("extra_charges_amount", {p["field"] for p in price_safety.issues(quote, BRIEF, "decorator")})


class ConfirmedReadbackTest(unittest.TestCase):
    """From a live call: transport 2000 on a 2500 base was asked three times despite "हाँ हाँ, सही है"."""

    QUOTE = {"available": True, "price_amount": 2500, "price_unit": "total", "transport_charge": 2000,
             "extra_charges_amount": 0, "advance_amount": 1000}

    def speech(self, answer):
        return [
            {"role": "assistant", "text": "इसका रेट कितना होगा?"}, {"role": "user", "text": "2500"},
            {"role": "assistant", "text": "ट्रांसपोर्ट का कोई चार्ज है क्या?"}, {"role": "user", "text": "हाँ, 2000।"},
            {"role": "assistant", "text": "कोई और एक्स्ट्रा चार्ज है क्या?"}, {"role": "user", "text": "नहीं कोई और extra charge नहीं।"},
            {"role": "assistant", "text": "एडवांस कितना चाहिए?"}, {"role": "user", "text": "1000"},
            {"role": "assistant", "text": "तो 2500 रुपये टोटल, ट्रांसपोर्ट 2000 रुपये, एडवांस 1000 रुपये, सही है?"},
            {"role": "user", "text": answer},
        ]

    def fields(self, quote, answer):
        quote = dict(quote)
        quote["amount_checks"] = price_safety.audit(quote, self.speech(answer), "photographer", call_id="rb")
        return {p["field"] for p in price_safety.issues(quote, BRIEF, "photographer")}

    def test_yes_to_the_readback_settles_the_unusual_transport(self):
        self.assertNotIn("transport_charge", self.fields(self.QUOTE, "हाँ हाँ, सही है।"))

    def test_without_the_yes_it_still_needs_a_repeat(self):
        self.assertIn("transport_charge", self.fields(self.QUOTE, "एक मिनट"))

    def test_a_yes_never_proves_an_amount_they_never_said(self):
        self.assertIn("transport_charge", self.fields({**self.QUOTE, "transport_charge": 3000}, "हाँ हाँ, सही है।"))
        speech = [{"role": "assistant", "text": "तो ट्रांसपोर्ट 2000 रुपये, सही है?"}, {"role": "user", "text": "हाँ"}]
        checks = price_safety.audit(self.QUOTE, speech, "photographer", call_id="rb")
        self.assertEqual(checks["fields"]["transport_charge"]["statements"], [])


class RepeatedRateTest(unittest.TestCase):
    """From a live call: the per-plate rate was asked four times though the vendor said 1200 each time."""

    def test_no_to_a_gst_question_does_not_cancel_the_rate(self):
        quote = {"available": True, "price_amount": 1200, "price_unit": "per_plate", "transport_charge": 5000,
                 "extra_charges_amount": 0, "advance_amount": 3000, "gst_included": False, "gst_percent": 28}
        speech = [
            {"role": "assistant", "text": "मीडियम साइज़ का फंक्शन है, लगभग 40 गेस्ट, पर प्लेट रेट कितना होगा?"},
            {"role": "user", "text": "40 guest हैं ना, तो per plate होगा आपका 1200 रुपये।"},
            {"role": "assistant", "text": "GST आपके रेट में शामिल है क्या?"},
            {"role": "user", "text": "नहीं नहीं, GST नहीं शामिल है, वो बाद में 28% अलग से लगेगा।"},
        ]
        checks = price_safety.audit(quote, speech, "caterer", call_id="rate")
        self.assertEqual(len(checks["fields"]["price_amount"]["statements"]), 1)

    def test_a_time_in_another_sentence_does_not_hide_the_rate(self):
        self.assertEqual(price_safety._assertions("बोले तो 1200। हाँ, 1 minute।", "पर प्लेट रेट कितने रुपये है?", "caterer"),
                         {"price_amount": (1200, "per_plate")})

    def test_a_no_to_our_readback_still_cancels_it(self):
        quote = {"available": True, "price_amount": 1200, "price_unit": "per_plate"}
        speech = [{"role": "assistant", "text": "पर प्लेट रेट कितना होगा?"}, {"role": "user", "text": "1200 रुपये"},
                  {"role": "assistant", "text": "तो 1200 रुपये पर प्लेट, सही है?"}, {"role": "user", "text": "नहीं"}]
        self.assertEqual(price_safety.audit(quote, speech, "caterer", call_id="rate")["fields"]["price_amount"]["statements"], [])
