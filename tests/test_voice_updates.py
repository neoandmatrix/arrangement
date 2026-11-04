"""Inbound vendor changes must preserve agreed terms until the family decides."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import prompts
import rules
import store
import workflow
from conversation import SessionConfig, _plan, brief_problems


class VendorUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "updates.db"
        self.brief = {**prompts.SAMPLE_NAAMKARAN, "family_id": "F12345678",
                      "date": (date.today() + timedelta(days=20)).isoformat()}
        self.event_id = store.save_event(self.brief)
        self.quote = {"available": True, "price_amount": 2400, "price_unit": "total", "transport_charge": 100,
                      "extra_charges_amount": 0, "advance_amount": 500, "initial_price_amount": 2500}
        for vendor_id, price in (("K01", 2400), ("K03", 2600)):
            store.save_quote(self.event_id, vendor_id, None, {**self.quote, "price_amount": price})
        workflow.book("cake", "K01")

    def tearDown(self):
        store.DB_PATH = self.original_db
        self.tmp.cleanup()

    def price_update(self, amount=2800):
        return workflow.record_vendor_update(self.event_id, "K01",
            {"kind": "price", "price_unit": "total", "price_amount": amount, "notes": "The cake base price has changed."}, f"The price is {amount} rupees")

    def test_increase_waits_for_approval(self):
        update_id = self.price_update()
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["price_amount"], 2400)
        self.assertEqual(store.update(update_id)["status"], "pending")
        workflow.decide_update(self.event_id, update_id, True)
        updated = store.latest_quotes(self.event_id)["K01"]
        self.assertEqual(updated["price_amount"], 2800)
        self.assertEqual(updated["transport_charge"], 100)
        self.assertEqual(updated["advance_amount"], 500)
        self.assertEqual(updated["initial_price_amount"], 2500)

    def test_decrease_also_waits_for_approval(self):
        update_id = self.price_update(2000)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["price_amount"], 2400)
        workflow.decide_update(self.event_id, update_id, False)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["price_amount"], 2400)

    def test_amount_not_said_is_rejected_not_saved(self):
        with self.assertRaises(ValueError):
            workflow.record_vendor_update(self.event_id, "K01", {"kind": "price", "price_unit": "total", "price_amount": 9000, "notes": "Rate change"}, "The price is 2800")
        self.assertEqual(store.updates(self.event_id), [])

    def test_new_revision_supersedes_old_and_cannot_be_applied_twice(self):
        first = self.price_update()
        second = self.price_update(2900)
        self.assertEqual(store.update(first)["status"], "superseded")
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, first, True)
        workflow.decide_update(self.event_id, second, True)
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, second, True)

    def test_other_unverified_charges_remain_flagged(self):
        store.save_quote(self.event_id, "K01", None, {**self.quote, "unverified": ["price_amount", "transport_charge"]})
        update_id = self.price_update()
        workflow.decide_update(self.event_id, update_id, True)
        self.assertEqual(store.latest_quotes(self.event_id)["K01"]["unverified"], ["transport_charge"])

    def test_cancellation_switches_backup_and_disables_cancelled_quote(self):
        workflow.record_vendor_update(self.event_id, "K01", {"kind": "cancelled", "notes": "Cannot deliver"}, "Cannot deliver")
        primary = next(b for b in store.bookings(self.event_id) if b["role"] == "primary")
        self.assertEqual(primary["vendor_id"], "K03")
        self.assertEqual(store.enquiry(self.event_id, "K01")["status"], "replaced")
        self.assertFalse(store.latest_quotes(self.event_id)["K01"]["available"])

    def test_late_eta_uses_category_deadline(self):
        workflow.record_vendor_update(self.event_id, "K01", {"kind": "delayed", "eta": "23:00", "notes": "Running late"}, "I will arrive at 23:00")
        self.assertEqual(next(b for b in store.bookings(self.event_id) if b["role"] == "primary")["vendor_id"], "K03")

    def test_invalid_eta_and_nonpositive_prices_are_rejected(self):
        for data in ({"kind": "delayed", "eta": "29:00", "notes": "Late"}, {"kind": "delayed", "eta": "7:30", "notes": "Late"},
                     {"kind": "price", "price_unit": "total", "price_amount": -10, "notes": "Rate"}):
            with self.assertRaises(ValueError):
                workflow.record_vendor_update(self.event_id, "K01", data, "10")

    def test_note_does_not_change_booking_or_quote(self):
        workflow.record_vendor_update(self.event_id, "K01", {"kind": "note", "notes": "Please prepare a table"}, "Please prepare a table")
        self.assertEqual(store.latest_quotes(self.event_id)["K01"], self.quote)
        self.assertEqual(store.updates(self.event_id)[0]["status"], "received")

    def test_pending_rate_cannot_be_approved_after_vendor_cancels(self):
        update_id = self.price_update()
        workflow.record_vendor_update(self.event_id, "K01", {"kind": "cancelled", "notes": "Cannot deliver"}, "Cannot deliver")
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, update_id, True)

    def test_update_is_bound_to_original_event_not_latest(self):
        store.save_event({**self.brief, "family_id": "F87654321"})
        update_id = self.price_update()
        self.assertEqual(store.update(update_id)["event_id"], self.event_id)
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id + 1, update_id, True)

    def test_form_plan_has_only_draft_tool_and_selected_language(self):
        cfg = SessionConfig.from_dict({"mode": "form", "language": "kn-IN", "draft": {"family_name": "Demo"}}, channel="app")
        prompt, tools, language, event_id = _plan(cfg)
        self.assertEqual([t.name for t in tools], ["fill_form", "end_call"])
        self.assertEqual(language, "kn-IN")
        self.assertIsNone(event_id)
        self.assertIn("Never choose a location", prompt)

    def test_time_validation_rejects_hours_over_23(self):
        self.assertIn("the muhurat start time", brief_problems({**self.brief, "start_time": "29:00"}))

    def test_price_unit_must_be_confirmed_and_cannot_change(self):
        for unit in (None, "per_plate"):
            with self.assertRaises(ValueError):
                workflow.record_vendor_update(self.event_id, "K01", {"kind": "price", "price_unit": unit, "price_amount": 2800, "notes": "New rate"}, "2800")

    def test_cancellation_without_standby_allows_family_to_choose_replacement(self):
        store.set_booking(self.event_id, "cake", "backup", "K03", "cancelled")
        workflow.record_vendor_update(self.event_id, "K01", {"kind": "cancelled", "notes": "Cannot deliver"}, "Cannot deliver")
        primary = next(b for b in store.bookings(self.event_id) if b["role"] == "primary")
        self.assertEqual(primary["status"], "cancelled")
        self.assertEqual(workflow.book("cake", "K03")["primary"], "K03")

    def test_pending_revision_cannot_be_booked_at_the_old_rate(self):
        event_id = store.save_event(self.brief)
        store.save_quote(event_id, "K01", None, self.quote)
        update_id = workflow.record_vendor_update(event_id, "K01", {"kind": "price", "price_unit": "total", "price_amount": 2800, "notes": "Rate changed"}, "2800")
        with self.assertRaisesRegex(ValueError, "pending price revision"):
            workflow.book("cake", "K01")
        workflow.decide_update(event_id, update_id, True)
        self.assertEqual(workflow.book("cake", "K01")["primary"], "K01")
