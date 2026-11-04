"""Rules engine and booking workflow. Run: uv run python -m unittest discover tests"""

import tempfile
import unittest
from pathlib import Path

import prompts
import rules
import store
import workflow
from price_fixture import checked_quote

BRIEF = prompts.SAMPLE_BRIEF  # 40 guests, muhurat 2026-11-15 07:30


def caterer_quote(**overrides):
    quote = {
        "available": True,
        "price_amount": 450,
        "price_unit": "per_plate",
        "transport_charge": 1500,
        "extra_charges_amount": 0,
        "gst_included": True,
        "advance_amount": 5000,
    }
    return checked_quote({**quote, **overrides}, "caterer")


class RulesTest(unittest.TestCase):
    def test_landed_cost_per_plate_adds_extras(self):
        q = caterer_quote(extra_charges_amount=2000)
        self.assertEqual(rules.landed_cost(q, BRIEF), 450 * 40 + 1500 + 2000)

    def test_landed_cost_total_price(self):
        q = {"price_amount": 11000, "price_unit": "total", "transport_charge": 500}
        self.assertEqual(rules.landed_cost(q, BRIEF), 11500)

    def test_missing_price_blocks_booking(self):
        vendor = store.vendor("C06")
        flags = rules.quote_flags(caterer_quote(price_amount=None), vendor, BRIEF)
        self.assertIn("block", [f["level"] for f in flags])

    def test_big_advance_and_bad_reviews_are_flagged(self):
        vendor = store.vendor("C04")  # review: cancelled the night before
        texts = " ".join(f["text"] for f in rules.quote_flags(caterer_quote(advance_amount=15000), vendor, BRIEF))
        self.assertIn("more than half", texts)
        self.assertIn("Cancelled the night before", texts)

    def test_review_keywords_match_whole_words(self):
        self.assertEqual(rules.review_warnings({"reviews": ["Great price per plate, chocolate too."]}), [])
        self.assertEqual(len(rules.review_warnings({"reviews": ["Came 2 hours late."]})), 1)

    def test_numbers_heard_handles_commas_and_devanagari_digits(self):
        self.assertEqual(rules.numbers_heard("480 रुपये, transport 1,000, एडवांस ५०००"), {480, 1000, 5000})

    def test_invented_amounts_are_unverified_and_block_on_price(self):
        said = "40 लोगों के लिए 480 रुपये प्रति प्लेट, transport का 1,000 अलग, 5,000 एडवांस"
        # This legacy numeric helper is not the field-specific booking guard.
        quote = caterer_quote(price_amount=250, transport_charge=1000, extra_charges_amount=3000, advance_amount=5000)
        quote["unverified"] = rules.unverified_amounts(quote, said)
        self.assertEqual(quote["unverified"], ["price_amount", "extra_charges_amount"])
        flags = rules.quote_flags(quote, store.vendor("C06"), BRIEF)
        self.assertIn("block", [f["level"] for f in flags if "not heard" in f["text"]])

    def test_ranking_prefers_bookable_then_fewer_warnings_then_cheaper(self):
        vendors = {v["id"]: v for v in store.vendors()}
        quotes = {
            "C06": caterer_quote(price_amount=500),  # clean reviews
            "C04": caterer_quote(price_amount=300),  # cheaper, but a cancellation review
            "C03": caterer_quote(available=False),  # not available
        }
        ranked = rules.rank_category("caterer", quotes, vendors, BRIEF)
        self.assertEqual([r["vendor_id"] for r in ranked], ["C06", "C04", "C03"])
        self.assertEqual(rules.pick(ranked), ("C06", "C04"))

    def test_checkin_schedule_ends_before_required_arrival(self):
        schedule = rules.checkin_schedule(BRIEF, "caterer")
        self.assertEqual([s[0] for s in schedule][0], "D-3 confirmation")
        self.assertEqual(schedule[-1][1], "2026-11-15T05:45")  # 07:30 - 60 min lead - 45 min

    def test_late_eta_activates_backup(self):
        done = [{"status": "delayed", "eta": "07:15", "label": "Final: ETA check"}]
        self.assertEqual(rules.assess_risk(done, BRIEF, "caterer")["action"], "activate_backup")

    def test_small_delay_only_warns(self):
        done = [{"status": "delayed", "eta": "06:15", "label": "Morning: on the way?"}]
        self.assertEqual(rules.assess_risk(done, BRIEF, "caterer")["action"], "warn")

    def test_two_unreachable_in_a_row_activates_backup(self):
        done = [
            {"status": "confirmed", "eta": None, "label": "D-3 confirmation"},
            {"status": "unreachable", "eta": None, "label": "D-1 confirmation"},
            {"status": "unreachable", "eta": None, "label": "Morning: on the way?"},
        ]
        self.assertEqual(rules.assess_risk(done, BRIEF, "caterer")["action"], "activate_backup")


class NegotiationTest(unittest.TestCase):
    TIGHT = {**BRIEF, "budget": {"caterer": 18000}}
    OFFER = {"price_amount": 480, "price_unit": "per_plate", "transport_charge": 1000, "extra_charges_amount": 0}

    def test_over_budget_opens_near_budget_then_meets_halfway_then_stops(self):
        r1 = rules.counter_offer("caterer", self.OFFER, self.TIGHT, round_no=1, competing_total=None)
        self.assertEqual((r1["price_amount"], r1["reason"]), (420, "budget"))  # (18000 - 1000) / 40, rounded down
        r2 = rules.counter_offer(
            "caterer", {**self.OFFER, "price_amount": 450}, self.TIGHT,
            round_no=2, competing_total=None, previous_ask_total=r1["asked_total"],
        )
        self.assertEqual(r2["price_amount"], 430)  # halfway between our ask and their 450
        r3 = rules.counter_offer("caterer", self.OFFER, self.TIGHT, round_no=3, competing_total=None)
        self.assertFalse(r3["negotiate"])

    def test_never_opens_lower_than_fifteen_percent_off(self):
        tiny_budget = {**BRIEF, "budget": {"caterer": 5000}}
        r = rules.counter_offer("caterer", self.OFFER, tiny_budget, round_no=1, competing_total=None)
        self.assertGreaterEqual(r["asked_total"], int(rules.landed_cost(self.OFFER, BRIEF) * 0.85) - 400)

    def test_under_budget_asks_once_to_include_transport(self):
        r1 = rules.counter_offer("caterer", self.OFFER, BRIEF, round_no=1, competing_total=None)
        self.assertEqual(r1["ask"], "waive_transport")
        self.assertFalse(rules.counter_offer("caterer", self.OFFER, BRIEF, round_no=2, competing_total=None)["negotiate"])

    def test_cites_a_real_cheaper_quote(self):
        r = rules.counter_offer("caterer", self.OFFER, BRIEF, round_no=1, competing_total=19000)
        self.assertEqual((r["reason"], r["price_amount"]), ("competing_quote", 450))

    def test_competitor_total_ignores_unverified_and_unavailable_quotes(self):
        quotes = [
            checked_quote({**self.OFFER, "available": True, "price_amount": 400}, "caterer"),
            checked_quote({**self.OFFER, "available": True, "price_amount": 300, "unverified": ["price_amount"]}, "caterer"),
            {**self.OFFER, "available": False, "price_amount": 200},
        ]
        self.assertEqual(rules.best_competing_total(quotes, BRIEF), 400 * 40 + 1000)

    def test_no_haggling_with_the_priest(self):
        self.assertFalse(rules.counter_offer("priest", self.OFFER, self.TIGHT, round_no=1, competing_total=None)["negotiate"])

    def test_savings_shown_as_a_flag(self):
        quote = caterer_quote(price_amount=450, initial_price_amount=480)
        self.assertEqual(rules.savings(quote, BRIEF), 30 * 40)
        texts = [f["text"] for f in rules.quote_flags(quote, store.vendor("C06"), BRIEF)]
        self.assertTrue(any("saves ₹1200" in t for t in texts))


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = store.DB_PATH
        store.DB_PATH = Path(self._tmp.name) / "test.db"
        self.event_id = store.save_event(BRIEF)

    def tearDown(self):
        store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_booking_then_cancellation_switches_to_backup(self):
        store.save_quote(self.event_id, "C06", None, caterer_quote(price_amount=500))
        store.save_quote(self.event_id, "C02", None, caterer_quote(price_amount=480))
        result = workflow.book("caterer")
        self.assertEqual(result["primary"], "C02")
        self.assertEqual(result["backup"], "C06")

        first = next(c for c in store.checkins(self.event_id) if c["vendor_id"] == "C02")
        risk = workflow.record_checkin(first["id"], "cancelled", None, "Another booking came up")
        self.assertEqual(risk["action"], "activate_backup")

        roles = {b["role"]: b for b in store.bookings(self.event_id)}
        self.assertEqual((roles["primary"]["vendor_id"], roles["primary"]["status"]), ("C06", "active"))
        self.assertEqual(roles["backup"]["status"], "replaced")
        pending = {c["vendor_id"] for c in store.checkins(self.event_id) if c["status"] == "pending"}
        self.assertEqual(pending, {"C06"})
        self.assertEqual(store.alerts(self.event_id)[0]["level"], "critical")


if __name__ == "__main__":
    unittest.main()
