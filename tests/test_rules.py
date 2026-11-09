"""Rules engine and booking workflow. Run: uv run python -m unittest discover tests"""

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import prompts
import price_safety
import rules
import store
import workflow
from price_fixture import checked_quote
from confirmation_fixture import book_confirmed, finish_replacement

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

    def test_only_a_cancellation_switches_to_the_backup(self):
        self.assertEqual(rules.assess_risk([{"status": "cancelled", "eta": None, "label": "D-1 confirmation"}], BRIEF, "caterer")["action"],
                         "activate_backup")
        for done in ([{"status": "delayed", "eta": "07:15", "label": "Final: ETA check"}],
                     [{"status": "unreachable", "eta": None, "label": "D-1 confirmation"}, {"status": "unreachable", "eta": None, "label": "Morning: on the way?"}],
                     [{"status": "unsure", "eta": None, "label": "Final: ETA check"}]):
            self.assertEqual(rules.assess_risk(done, BRIEF, "caterer")["action"], "warn", done)  # the family is warned

    def test_late_eta_activates_backup(self):
        done = [{"status": "delayed", "eta": "07:15", "label": "Final: ETA check"}]
        self.assertEqual(rules.assess_risk(done, BRIEF, "caterer", only_on_cancel=False)["action"], "activate_backup")

    def test_small_delay_only_warns(self):
        done = [{"status": "delayed", "eta": "06:15", "label": "Morning: on the way?"}]
        self.assertEqual(rules.assess_risk(done, BRIEF, "caterer")["action"], "warn")

    def test_two_unreachable_in_a_row_activates_backup(self):
        done = [
            {"status": "confirmed", "eta": None, "label": "D-3 confirmation"},
            {"status": "unreachable", "eta": None, "label": "D-1 confirmation"},
            {"status": "unreachable", "eta": None, "label": "Morning: on the way?"},
        ]
        self.assertEqual(rules.assess_risk(done, BRIEF, "caterer", only_on_cancel=False)["action"], "activate_backup")


@patch.object(price_safety, "RELAXED", False)  # the original strict guards
class NegotiationTest(unittest.TestCase):
    TIGHT = {**BRIEF, "budget": {"caterer": 18000}}
    OFFER = {"price_amount": 480, "price_unit": "per_plate", "transport_charge": 1000, "extra_charges_amount": 0}

    def test_price_plan_is_fixed_from_the_budget(self):
        plan = rules.price_plan("caterer", self.TIGHT)  # 18000 for 40 guests = 450 a plate
        self.assertEqual(plan, {"unit": "per_plate", "expected": 380, "maximum": 450, "tolerance": 40})  # 10%, to 10s
        self.assertEqual(rules.price_plan("decorator", {"guests": 40, "budget": {"decorator": 12000}})["tolerance"], 1200)
        self.assertEqual(rules.price_plan("cake", {"guests": 40, "budget": {"cake": 1500}})["tolerance"], 200)  # floor
        self.assertEqual(rules.price_plan("cake", {"guests": 40})["maximum"], 2800)  # typical 2400 + 20%, no budget

    BUDGET = {**BRIEF, "budget": {"caterer": 25000}}  # 620 a plate at most, 60 tolerance, counters capped at 740

    def bargain(self, rates):
        said, ask, last = [], None, None
        for round_no, rate in enumerate(rates, 1):
            r = rules.counter_offer("caterer", {"price_amount": rate, "price_unit": "per_plate"}, self.BUDGET,
                                    round_no=round_no, previous_ask=ask, previous_rate=last)
            if not r["negotiate"]:
                said.append(r.get("close"))
                break
            said.append(r["price_amount"] if r["reason"] == "counter" else f"max {r['price_amount']}")
            ask, last = r["price_amount"], rate
        return said

    def test_moderately_above_we_ask_about_ten_percent_below(self):
        self.assertEqual(self.bargain([700, 680]), [650, "accept"])
        self.assertEqual(self.bargain([800, 750, 720]), [700, 720, "accept"])

    def test_far_above_we_open_with_our_maximum_and_never_chase_their_price(self):
        self.assertEqual(self.bargain([5000, 3000, 3000]), ["max 620", 740, "pass_on"])
        self.assertEqual(self.bargain([3000, 700]), ["max 620", 660])

    def test_holding_a_near_price_is_final_but_a_far_one_goes_to_the_family(self):
        self.assertEqual(self.bargain([700, 700]), [650, "accept"])
        self.assertEqual(self.bargain([900, 900]), [740, "pass_on"])

    def test_within_our_maximum_is_accepted(self):
        for rate in (300, 600, 680):
            self.assertEqual(self.bargain([rate]), ["accept"], rate)

    def test_a_caterers_total_quote_is_compared_per_plate(self):
        r = rules.counter_offer("caterer", {**self.OFFER, "price_amount": 28000, "price_unit": "total"}, self.BUDGET, round_no=1)
        self.assertEqual((r["price_amount"], r["price_unit"]), (650, "per_plate"))  # 28000 / 40 = 700 a plate -> 650

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
        result = book_confirmed("caterer")
        self.assertEqual(result["primary"], "C02")
        self.assertEqual(result["backup"], "C06")

        first = next(c for c in store.checkins(self.event_id) if c["vendor_id"] == "C02")
        risk = workflow.record_checkin(first["id"], "cancelled", None, "Another booking came up")
        self.assertEqual(risk["action"], "activate_backup")
        self.assertFalse(any(b['role'] == 'primary' and b['status'] == 'active' for b in store.bookings(self.event_id)))
        finish_replacement(self.event_id, 'caterer')

        roles = {b["role"]: b for b in store.bookings(self.event_id)}
        self.assertEqual((roles["primary"]["vendor_id"], roles["primary"]["status"]), ("C06", "active"))
        self.assertEqual(roles["backup"]["status"], "replaced")
        pending = {c["vendor_id"] for c in store.checkins(self.event_id) if c["status"] == "pending"}
        self.assertEqual(pending, {"C06"})
        self.assertIn('replacement booking', store.alerts(self.event_id)[0]['message'])


if __name__ == "__main__":
    unittest.main()
