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
