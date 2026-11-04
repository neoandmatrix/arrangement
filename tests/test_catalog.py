"""Functions and vendor types: every part of the system agrees on them. Run: uv run python -m unittest discover tests"""

import unittest

import catalog
import prompts
import rules
import store
from conversation import SAVE_BRIEF, brief_problems, quote_problems

NAAMKARAN = prompts.SAMPLE_NAAMKARAN


def brief_args(**overrides):
    """save_brief arguments as the model would send them."""
    args = {
        "function_type": "puja",
        "function_variant": "satyanarayan",
        "family_name": "Iyer",
        "contact_name": "Lakshmi Iyer",
        "city": "Bengaluru",
        "area": "Jayanagar",
        "date": "2099-01-10",
        "start_time": "09:00",
        "guests": 30,
        "needs": ["priest", "caterer"],
    }
    return {**args, **overrides}


class CatalogTest(unittest.TestCase):
    def test_every_category_is_fully_defined(self):
        self.assertEqual(set(rules.CATEGORIES), set(catalog.CATEGORIES))
        self.assertEqual(set(rules.CATEGORIES), set(catalog.QUOTE_REQUIRED))
        self.assertEqual(set(rules.CATEGORIES), set(rules.ARRIVAL_LEAD))
        for f in catalog.FUNCTIONS.values():
            self.assertTrue(set(f["default_needs"] + f["optional"]) <= set(rules.CATEGORIES))

    def test_every_synthetic_vendor_has_a_known_category_and_an_invalid_phone(self):
        for v in store.vendors():
            self.assertIn(v["category"], rules.CATEGORIES)
            self.assertTrue(v["phone"].startswith("+9100000"), v["id"])  # never a real number
        for category in rules.CATEGORIES:
            self.assertGreaterEqual(sum(v["category"] == category for v in store.vendors()), 3, category)

    def test_save_brief_tool_offers_every_function_and_category(self):
        props = SAVE_BRIEF.properties
        self.assertEqual(props["function_type"]["enum"], list(catalog.FUNCTIONS))
        self.assertIn("naamkaran", props["function_variant"]["enum"])
        self.assertEqual(props["needs"]["items"]["enum"], list(catalog.CATEGORIES))
        self.assertIn("budget_tent_sound", props)
        self.assertIn("details_cake", props)

    def test_function_label(self):
        self.assertEqual(catalog.function_label(NAAMKARAN), "Naamkaran (naming ceremony)")
        self.assertEqual(catalog.function_label({"function_type": "griha pravesh"}), "griha pravesh")  # older brief


class ChecklistTest(unittest.TestCase):
    def test_priest_is_asked_about_the_actual_puja(self):
        brief = {**prompts.SAMPLE_BRIEF, "function_type": "puja", "function_variant": "rudrabhishek"}
        self.assertIn("Rudrabhishek", " ".join(catalog.checklist("priest", brief)))

    def test_decoration_matches_the_function(self):
        items = " ".join(catalog.checklist("decorator", NAAMKARAN))
        self.assertIn("cradle", items)
        self.assertNotIn("toran", items)

    def test_family_wishes_reach_the_vendor(self):
        self.assertIn("eggless chocolate", " ".join(catalog.checklist("cake", NAAMKARAN)))

    def test_every_vendor_prompt_renders(self):
        for vendor in store.vendors():
            text = prompts.vendor_prompt(vendor=vendor, brief=NAAMKARAN)
            self.assertIn(vendor["name"], text)
        self.assertIn("Naamkaran", prompts.intake_prompt())


class BriefProblemsTest(unittest.TestCase):
    def test_a_complete_puja_brief_passes(self):
        self.assertEqual(brief_problems(brief_args()), [])

    def test_area_names_are_not_mistaken_for_placeholders(self):
        for area in ("Jayanagar", "Indiranagar", "Banashankari", "Rajajinagar"):
            self.assertEqual(brief_problems(brief_args(area=area)), [], area)
        self.assertEqual(brief_problems(brief_args(area="N/A")), ["area"])

    def test_unknown_function_is_refused(self):
        self.assertTrue(brief_problems(brief_args(function_type="wedding"))[0].startswith("the function"))

    def test_variant_must_belong_to_the_function(self):
        problems = brief_problems(brief_args(function_variant="naamkaran"))
        self.assertTrue(problems[0].startswith("which puja"))

    def test_a_cake_needs_its_details(self):
        self.assertTrue(brief_problems(brief_args(needs=["cake"]))[0].startswith("the cake"))
        self.assertEqual(brief_problems(brief_args(needs=["cake"], details_cake="1 kg eggless")), [])


class NewCategoryRulesTest(unittest.TestCase):
    def test_photographer_must_say_what_is_covered(self):
        args = {"available": True, "price_amount": 9000, "price_unit": "total", "transport_charge": 0, "extra_charges_amount": 0, "advance_amount": 2000, "includes_notes": ""}
        self.assertEqual(quote_problems(args, "photographer"), ["hours covered, photos or video, and when they're delivered"])

    def test_cake_gets_one_gentle_round_only(self):
        offer = {"price_amount": 3000, "price_unit": "total", "transport_charge": 0}
        r1 = rules.counter_offer("cake", offer, NAAMKARAN, round_no=1, competing_total=None)
        self.assertTrue(r1["negotiate"])
        self.assertEqual(r1["price_amount"], 2500)  # 15% off is 2550; totals round down to 100
        self.assertFalse(rules.counter_offer("cake", offer, NAAMKARAN, round_no=2, competing_total=None)["negotiate"])

    def test_tent_checkins_are_scheduled_before_the_four_hour_setup(self):
        final = dict(rules.checkin_schedule(NAAMKARAN, "tent_sound"))["Final: ETA check"]
        self.assertEqual(final, "2026-11-22T05:15")  # muhurat 10:00 - 4 h - 45 min


if __name__ == "__main__":
    unittest.main()
