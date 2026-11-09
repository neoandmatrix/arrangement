"""Web demo: synthetic tests only, no model calls or network."""

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import demo
import prompts
import store
from vendor_bot import HELLO, Pricing, Recorder, persona_prompt, pricing, readback_reply, short_reply, stated_terms


class FakeRequest:
    def __init__(self, ip="203.0.113.7"):
        self.headers = {"cf-connecting-ip": ip}
        self.client = None


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(store, "DB_PATH", root / "demo.db"),
            patch.object(demo, "RECORDINGS", root / "rec"),
            patch.object(demo, "USAGE_PATH", root / "rec" / "usage.json"),
            patch.dict(os.environ, {"DEMO_DAILY_CALLS": "5", "DEMO_IP_CALLS": "2"}),
        ]
        for p in self.patches:
            p.start()
        demo._runs.clear()
        app = FastAPI()
        app.include_router(demo.router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_vendor_price_card_leaves_room_to_bargain_but_has_a_floor(self):
        brief = {**prompts.SAMPLE_BRIEF, "guests": 40, "budget": {"caterer": 18000, "decorator": 12000}}
        for seed in ("a", "b", "c"):
            caterer = pricing("caterer", brief, seed)
            self.assertEqual(caterer.unit, "per_plate")
            self.assertLess(caterer.floor, caterer.opening)
            self.assertGreater(caterer.opening, 18000 / 40)
            decorator = pricing("decorator", brief, seed)
            self.assertEqual(decorator.unit, "total")
            self.assertLess(decorator.floor, decorator.opening)
        self.assertEqual(pricing("caterer", brief, "x"), pricing("caterer", brief, "x"))  # stable per run

    def test_persona_keeps_the_floor_secret_and_speaks_the_vendor_language(self):
        vendor = store.vendor("C01")  # Kannada
        text = persona_prompt(vendor, prompts.SAMPLE_BRIEF, Pricing("per_plate", 550, 460, 5500), renegotiation=False)
        self.assertIn("Kannada", text)
        self.assertIn("Never mention your lowest price", text)
        self.assertIn("kn-IN", HELLO)

    def test_vendor_replies_are_cut_to_two_sentences(self):
        self.assertEqual(short_reply("540 रुपये प्रति प्लेट। परिवहन 500 रुपये। कुल 1,900 रुपये। धन्यवाद!"),
                         "540 रुपये प्रति प्लेट। परिवहन 500 रुपये।")
        self.assertEqual(short_reply("Yes, 18 November is fine."), "Yes, 18 November is fine.")
        self.assertEqual(short_reply("500 रुपये अतिरिक्त लगेगा।आपको कुल मिलाकर क्या चाहिए?आपकी सुविधा।"),
                         "500 रुपये अतिरिक्त लगेगा। आपको कुल मिलाकर क्या चाहिए?")
        self.assertEqual(short_reply("Price is 1,500.50 total."), "Price is 1,500.50 total.")

    def test_readbacks_are_answered_by_code_and_corrections_verify(self):
        import price_safety
        quote = {"available": True, "price_amount": 540, "price_unit": "per_plate", "transport_charge": 0,
                 "extra_charges_amount": 0, "advance_amount": 5500, "gst_included": True}
        dialogue = [
            {"role": "bandobast", "text": "प्रति प्लेट मूल्य कितना होगा?"}, {"role": "vendor_or_family", "text": "540 रुपये प्रति प्लेट।"},
            {"role": "bandobast", "text": "क्या ट्रांसपोर्ट का कोई शुल्क है?"}, {"role": "vendor_or_family", "text": "कोई ट्रांसपोर्ट शुल्क नहीं है।"},
            {"role": "bandobast", "text": "कितना एडवांस चाहिए?"}, {"role": "vendor_or_family", "text": "5,500 रुपये एडवांस चाहिए।"},
        ]
        terms = {}
        for question, answer in zip(dialogue[::2], dialogue[1::2]):
            terms.update(stated_terms(answer["text"], question["text"], "caterer"))
        self.assertEqual(terms, {"price_amount": 540, "transport_charge": 0, "advance_amount": 5500})
        # "GST is included in this price" must not read as a price of 0 (seen live).
        self.assertEqual(stated_terms("जी हाँ, इस कीमत में GST शामिल है।", "क्या इस कीमत में जीएसटी शामिल है?", "caterer"), {})
        right = "तो 540 रुपये प्रति प्लेट, 0 रुपये ट्रांसपोर्ट, 5,500 रुपये एडवांस, सही है?"
        self.assertEqual(readback_reply(right, terms, "caterer", "hi-IN"), "हाँ जी, सही है।")
        self.assertIsNone(readback_reply("क्या आप 460 रुपये प्रति प्लेट कर सकते हैं?", terms, "caterer", "hi-IN"))
        wrong = "तो 480 रुपये प्रति प्लेट, 0 रुपये ट्रांसपोर्ट, 5,500 रुपये एडवांस, सही है?"
        correction = readback_reply(wrong, terms, "caterer", "hi-IN")
        self.assertEqual(correction, "नहीं जी। पर प्लेट रेट 540 रुपये।")
        # Bandobast's own price check must accept the corrected amount as the vendor's statement.
        checks = price_safety.audit(quote, dialogue + [{"role": "bandobast", "text": wrong},
                                                       {"role": "vendor_or_family", "text": correction}], "caterer", call_id="t")
        self.assertTrue(checks["fields"]["price_amount"]["statements"])
        self.assertEqual(readback_reply(right, terms, "caterer", "kn-IN"), "ಹೌದು, ಸರಿ ಇದೆ.")

    def test_daily_and_per_visitor_caps(self):
        demo.spend(FakeRequest("198.51.100.1"), 2)
        with self.assertRaises(HTTPException) as e:
            demo.spend(FakeRequest("198.51.100.1"), 1)
        self.assertEqual(e.exception.status_code, 429)
        demo.spend(FakeRequest("198.51.100.2"), 2)
        with self.assertRaises(HTTPException):
            demo.spend(FakeRequest("198.51.100.3"), 2)  # 4 + 2 > 5 for the day

    def test_run_survives_a_restart(self):
        run = demo.DemoRun("runABCDEF12", 7, dict(prompts.SAMPLE_BRIEF), decisions={"C02": "approved"})
        run.calls.append(demo.DemoCall("caterer", "C02", status="in_call", call_id="AP1"))
        demo._save(run)
        demo._runs.clear()
        loaded = demo._load("runABCDEF12")
        self.assertEqual(loaded.decisions, {"C02": "approved"})
        self.assertEqual(loaded.calls[0].status, "failed")  # interrupted mid-call
        self.assertIsNone(demo._load("../../etc"))

    def test_recorder_mixes_both_sides_on_one_timeline(self):
        rec = Recorder()
        rec.add("bandobast", b"\x10\x00" * 2400, 24000)  # 0.1 s at 24 kHz
        rec.add("vendor", b"\x10\x00" * 1600, 16000)  # 0.1 s at 16 kHz
        self.assertEqual(sum(len(c) for _, c in rec.chunks), 3200)
        path = rec.save(Path(self.tmp.name) / "rec" / "call")
        self.assertTrue(path.exists())

    def test_family_run_is_validated_before_spending_calls(self):
        base = {"function_type": "griha_pravesh", "function_variant": "griha_pravesh", "family_name": "Sharma",
                "area": "HSR Layout", "date": (date.today() + timedelta(days=5)).isoformat(), "start_time": "08:00",
                "guests": 40, "services": {"caterer": "C02"}, "budget": {"caterer": 18000}}
        cases = [
            ({**base, "services": {}}, "Pick one or two"),
            ({**base, "services": {"caterer": "C02", "decorator": "D01", "priest": "P01"}}, "Pick one or two"),
            ({**base, "area": "Mars"}, "area"),
            ({**base, "services": {"caterer": "D01"}}, "Unknown caterer"),
            ({**base, "date": "2020-01-01"}, "date"),
        ]
        for body, message in cases:
            r = self.client.post("/demo/api/family-runs", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(message, r.json()["detail"])
        self.assertFalse(demo.USAGE_PATH.exists())

    def test_public_reads(self):
        self.assertEqual(self.client.get("/demo/api/runs/doesnotexist").status_code, 404)
        shortlist = self.client.get("/demo/api/shortlist", params={"category": "caterer", "area": "HSR Layout"}).json()
        self.assertTrue(shortlist["vendors"])
        request = self.client.get("/demo/api/vendor-request", params={"vendor_id": "K01"}).json()
        self.assertEqual(request["service"], "Cake")
        self.assertGreater(request["date"], date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
