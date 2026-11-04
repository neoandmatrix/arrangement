"""Shortlist, the family's approve / reject choices. Run: uv run python -m unittest discover tests"""

import tempfile
import unittest
from pathlib import Path

import prompts
import rules
import store
import workflow
from price_fixture import checked_quote

KORAMANGALA = {**prompts.SAMPLE_NAAMKARAN, "location": {"lat": 12.9352, "lon": 77.6245}}


def quote(price, **overrides):
    return checked_quote({"available": True, "price_amount": price, "price_unit": "total", "transport_charge": 0,
            "advance_amount": 500, **overrides}, "cake")


class ShortlistTest(unittest.TestCase):
    def test_three_per_service_clean_reviews_first(self):
        rows = rules.shortlist("cake", store.vendors(), KORAMANGALA)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["vendor_id"], "K02")  # the late-delivery review goes last
        self.assertTrue(all(r["distance_km"] < rules.MAX_DISTANCE_KM for r in rows))

    def test_far_vendors_are_left_out(self):
        yelahanka = {**KORAMANGALA, "location": {"lat": 13.1005, "lon": 77.5963}}
        ids = {r["vendor_id"] for r in rules.shortlist("tent_sound", store.vendors(), yelahanka)}
        self.assertNotIn("T02", ids)  # Electronic City is ~29 km away

    def test_no_pin_means_no_distance_filter(self):
        self.assertEqual(len(rules.shortlist("cake", store.vendors(), prompts.SAMPLE_BRIEF)), 3)


class FamilyChoiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = store.DB_PATH
        store.DB_PATH = Path(self._tmp.name) / "test.db"
        self.event_id = store.save_event(KORAMANGALA)
        for vendor_id, price in (("K01", 2400), ("K03", 2200), ("K02", 1800)):
            store.save_quote(self.event_id, vendor_id, None, quote(price))

    def tearDown(self):
        store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_family_can_book_a_vendor_other_than_the_top_pick(self):
        result = workflow.book("cake", "K01")
        self.assertEqual(result["primary"], "K01")
        self.assertIn(result["backup"], ("K03", "K02"))
        self.assertEqual(store.enquiry(self.event_id, "K01")["status"], "awaiting_confirmation")
        with self.assertRaises(ValueError):
            workflow.book("cake", "K03")  # already booked

    def test_rejected_vendor_leaves_the_comparison_and_is_never_the_backup(self):
        workflow.reject("K03")
        ranked = workflow.comparison(self.event_id, KORAMANGALA)["cake"]["ranked"]
        self.assertNotIn("K03", [r["vendor_id"] for r in ranked])
        self.assertNotEqual(workflow.book("cake")["backup"], "K03")

    def test_a_booked_vendor_cannot_be_rejected(self):
        workflow.book("cake", "K01")
        with self.assertRaises(ValueError):
            workflow.reject("K01")


if __name__ == "__main__":
    unittest.main()
