"""What of an LLM reply reaches the voice. Run: uv run python -m unittest discover tests"""

import unittest

import asyncio

from conversation import QuestionGate, drop_unspeakable, quote_problems


def spoken(chunks: list[str], **gate_options) -> str:
    """Stream one reply through the gate, as the LLM would, and return what gets spoken."""
    gate = QuestionGate(**gate_options)
    return "".join(gate.feed(c) for c in chunks) + gate.end()


class QuestionGateTest(unittest.TestCase):
    def test_drops_everything_after_the_first_question(self):
        self.assertEqual(spoken(["दक्षिणा कितनी होगी?", " सामग्री शामिल है?", " एडवांस कितना?"]), "दक्षिणा कितनी होगी?")

    def test_drops_a_long_recap_before_the_question(self):
        recap = "पंद्रह नवंबर दो हज़ार छब्बीस को एचएसआर लेआउट में सुबह साढ़े सात बजे के गृह प्रवेश के लिए चालीस मेहमानों का खाना चाहिए।"
        self.assertEqual(spoken([recap, " क्या आप", " उपलब्ध हैं?"]), "क्या आप उपलब्ध हैं?")

    def test_keeps_a_short_lead_in(self):
        self.assertEqual(spoken(["ठीक है।", " एडवांस कितना होगा?"]), "ठीक है। एडवांस कितना होगा?")

    def test_question_split_across_chunks(self):
        self.assertEqual(spoken(["बुकिंग के लिए कितना ", "एडवांस", " देना होगा", "?", " धन्यवाद।"]), "बुकिंग के लिए कितना एडवांस देना होगा?")

    def test_reply_without_a_question_is_spoken_whole(self):
        goodbye = "धन्यवाद, परिवार जल्द संपर्क करेगा। आपका दिन शुभ हो।"
        self.assertEqual(spoken([goodbye]), goodbye)

    def test_greeting_is_spoken_untrimmed(self):
        greeting = "नमस्ते जी, मैं बंदोबस्त से शर्मा परिवार की ओर से उनके गृह प्रवेश के बारे में बात कर रहा हूँ। क्या अभी बात हो सकती है?"
        self.assertEqual(spoken([greeting], greeting=True), greeting)

    def test_drops_leaked_english_notes_in_a_hindi_call(self):
        self.assertEqual(spoken(["Then wait.", " ठीक है।", " GST शामिल है?"], english_ok=False), "ठीक है। GST शामिल है?")
        self.assertEqual(spoken(["Then wait.", " ठीक है।", " GST शामिल है?"]), "ठीक है। GST शामिल है?")  # lead-in kept

    def test_nothing_new_while_the_last_question_is_unanswered(self):
        self.assertEqual(spoken(["ठीक है।", " एडवांस कितना होगा?"], may_ask=False), "")
        self.assertEqual(spoken(["धन्यवाद, अलविदा।"], may_ask=False), "")


class DropUnspeakableTest(unittest.TestCase):
    def clean(self, text: str) -> str:
        return asyncio.run(drop_unspeakable(text, "*"))

    def test_strips_tool_calls_and_brackets(self):
        self.assertEqual(self.clean('ठीक है। record_quote({"a": 1}) [Awaiting response] धन्यवाद'), "ठीक है। धन्यवाद")

    def test_collapses_stuck_repeats(self):
        self.assertEqual(self.clean("धन्यवाद धन्यवाद धन्यवाद धन्यवाद, अलविदा।"), "धन्यवाद, अलविदा।")
        self.assertEqual(self.clean("हाँ हाँ बिल्कुल"), "हाँ हाँ बिल्कुल")  # a natural double stays


class QuoteProblemsTest(unittest.TestCase):
    def test_caterer_quote_needs_transport_gst_and_advance(self):
        args = {"available": True, "price_amount": 480, "price_unit": "per_plate", "transport_charge": 0, "extra_charges_amount": 0}
        self.assertEqual(quote_problems(args, "caterer"), ["whether GST is included", "advance"])

    def test_zero_counts_as_an_answer(self):
        args = {"available": True, "price_amount": 12000, "price_unit": "total", "extra_charges_amount": 0, "transport_charge": 0, "advance_amount": 0}
        self.assertEqual(quote_problems(args, "priest"), [])

    def test_unavailable_vendor_needs_nothing_else(self):
        self.assertEqual(quote_problems({"available": False}, "decorator"), [])


if __name__ == "__main__":
    unittest.main()
