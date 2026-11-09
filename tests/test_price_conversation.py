"""Exercise actual tool handlers with a synthetic runner: no STT/TTS/LLM network calls."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipecat.processors.frame_processor import FrameProcessor

import conversation
import prompts
import store
import workflow


class DummyProcessor(FrameProcessor):
    pass


class FakeLLM(DummyProcessor):
    def __init__(self):
        super().__init__()
        self._settings = SimpleNamespace(model="synthetic-test")
        self.handlers = {}

    def register_function(self, name, handler):
        self.handlers[name] = handler

    async def push_frame(self, frame, direction=None):
        self.spoken = getattr(self, "spoken", []) + [getattr(frame, "text", None)]


class PriceConversationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "conversation.db"
        self.event_id = store.save_event(prompts.SAMPLE_BRIEF)
        self.args = {"available": True, "price_amount": 11000, "price_unit": "total", "transport_charge": 0,
                     "extra_charges_amount": 0, "advance_amount": 0}

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    async def run_handlers(self, args, speech, attempts, between=()):
        llm = FakeLLM()
        captured = {}
        results = []

        class Pair:
            def __init__(self, context, **kwargs):
                captured["context"] = context

            def user(self):
                return DummyProcessor()

            def assistant(self):
                return DummyProcessor()

        async def callback(result, **kwargs):
            results.append(result)

        class Runner:
            def __init__(self, **kwargs):
                pass

            async def run(self, task):
                context = captured["context"]
                for text in speech:
                    context.add_message({"role": "user", "content": text})
                for attempt in range(attempts):
                    if attempt and attempt - 1 < len(between):
                        context.add_message({"role": "user", "content": between[attempt - 1]})  # their answer
                    await llm.handlers["record_quote"](SimpleNamespace(function_name="record_quote", arguments=args, result_callback=callback, llm=llm))

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
            event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="P01",
                                         event_id=self.event_id, call_id="synthetic-price-call")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=llm), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        return results

    async def test_exhausted_retries_save_only_a_blocked_quote(self):
        results = await self.run_handlers(self.args, ["Yes, I am available."], 2)
        self.assertFalse(results[0]["ok"])  # one round of asking again...
        self.assertTrue(results[1]["ok"])   # ...then saved, blocked, rather than looping on the same question
        self.assertFalse(results[1]["bookable"])
        quote = store.latest_quotes(self.event_id)["P01"]
        self.assertEqual(set(quote["unverified"]), {"price_amount", "transport_charge", "extra_charges_amount", "advance_amount"})
        with self.assertRaises(ValueError):
            workflow.book("priest", "P01")

    async def test_invented_opening_price_is_not_saved_or_advertised(self):
        args = {**self.args, "initial_price_amount": 50000000, "initial_price_verified": True,
                "amount_checks": {"version": 1, "category": "priest"}}
        results = await self.run_handlers(args, ["Base price 11000 in total", "Transport 0", "Extra charges 0", "Advance 0"], 1)
        self.assertTrue(results[0]["bookable"])
        quote = store.latest_quotes(self.event_id)["P01"]
        self.assertEqual(quote["initial_price_amount"], 11000)
        self.assertTrue(quote["initial_price_verified"])
        self.assertEqual(workflow.book("priest", "P01")["primary"], "P01")

    async def test_too_high_rate_gets_sorry_then_too_high_then_is_saved(self):
        args = {**self.args, "price_amount": 400000}
        speech = ["Base price 400000 in total", "Transport 0", "Extra charges 0", "Advance 0"]
        results = await self.run_handlers(args, speech, 3, between=["400000 total", "400000 total"])
        self.assertIn("ठीक से सुना नहीं", results[0]["next"])
        self.assertIn("बहुत हाई", results[1]["next"])
        self.assertNotIn("बहुत हाई", str(results[2]))  # the third time the amount stands, checks apply as usual

    async def test_normal_rate_is_not_pushed_back(self):
        results = await self.run_handlers(self.args, ["Base price 11000 in total", "Transport 0", "Extra charges 0", "Advance 0"], 1)
        self.assertTrue(results[0]["ok"])


class HighPriceRightAwayTest(unittest.IsolatedAsyncioTestCase):
    """The push-back comes the moment the vendor names a too-high rate, not at the read-back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "conversation.db"
        self.event_id = store.save_event(prompts.SAMPLE_BRIEF)

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    async def test_sorry_then_too_high_as_soon_as_the_rate_is_said(self):
        hooks, notes = [], []
        captured = {}

        class Pair:
            def __init__(self, context, **kwargs):
                captured["context"] = context

            def user(self):
                return DummyProcessor()

            def assistant(self):
                return DummyProcessor()

        def before_reply(*given):
            hooks.extend(given)
            return DummyProcessor()

        class Runner:
            def __init__(self, **kwargs):
                pass

            async def run(self, task):
                context = captured["context"]
                for mohan, vendor_says in [("एक प्लेट का रेट क्या है?", "10000 रुपये"),
                                            (conversation.HIGH_PRICE_LINES[0], "पर plate 50,000।"),  # live call
                                            ("एक प्लेट का रेट क्या है?", "अच्छा 550 कर देंगे")]:
                    context.add_message({"role": "assistant", "content": mohan})
                    context.add_message({"role": "user", "content": vendor_says})
                    frames = hooks[1]()  # [start_review, high_price_hint]
                    notes.append(frames[0].text if frames else None)

        llm = FakeLLM()
        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="C02",
                                         event_id=self.event_id, call_id="synthetic-high-price")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=llm), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        self.assertIn("ठीक से सुना नहीं", notes[0])  # spoken by code, word for word
        self.assertIn("बहुत हाई", notes[1])
        self.assertIn("मैक्सिमम 620 रुपये पर प्लेट", notes[1])  # budget 25000 for 40 guests
        self.assertIsNone(notes[2])  # a normal rate gets no push-back


class FunctionDetailsTest(unittest.IsolatedAsyncioTestCase):
    """From a live call: "function कितना बड़ा है?" got "रेट कितना है?" four times."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "conversation.db"
        self.event_id = store.save_event(prompts.SAMPLE_BRIEF)

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    def test_the_vendor_questions_from_the_call_are_recognised(self):
        for said in ["अ। package का rate कितने, कितना बड़ा function है, उसके हिसाब से depend करता है।",
                     "नहीं, आप पहले बताइए कितना बड़ा ये है, function है, क्या है, कितने लोग हैं, कैसे है?",
                     "How big is the function?"]:
            self.assertTrue(conversation.ASKS_SCOPE.search(said), said)
        self.assertFalse(conversation.ASKS_SCOPE.search("हाँ, कर देंगे।"))

    def test_details_line_and_prompt(self):
        import catalog
        line, _ = catalog.scope_line("decorator", prompts.SAMPLE_BRIEF)
        self.assertIn("मीडियम साइज़", line)
        self.assertIn("40 गेस्ट", line)
        text = prompts.vendor_prompt(vendor={"name": "Shubh Mangal", "category": "decorator", "language": "hi-IN"}, brief=prompts.SAMPLE_BRIEF)
        self.assertIn(line, text)
        self.assertIn("never just repeat", text)

    async def test_code_answers_with_the_details_and_asks_the_rate(self):
        hooks, captured, spoken = [], {}, []

        class Pair:
            def __init__(self, context, **kwargs):
                captured["context"] = context

            def user(self):
                return DummyProcessor()

            def assistant(self):
                return DummyProcessor()

        def before_reply(*given):
            hooks.extend(given)
            return DummyProcessor()

        class Runner:
            def __init__(self, **kwargs):
                pass

            async def run(self, task):
                context = captured["context"]
                context.add_message({"role": "assistant", "content": "पैकेज का रेट कितना है?"})
                context.add_message({"role": "user", "content": "कितना बड़ा function है, उसके हिसाब से depend करता है।"})
                frames = hooks[2]()
                spoken.append(frames[0].text if frames else None)

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="D02",
                                         event_id=self.event_id, call_id="synthetic-details")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=FakeLLM()), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        self.assertIn("मीडियम साइज़ का फंक्शन है, लगभग 40 गेस्ट", spoken[0])
        self.assertTrue(spoken[0].endswith("इसका रेट कितना होगा?"))


class BargainNowTest(FunctionDetailsTest):
    """Above our maximum: say the maximum when they name the rate. In range: say nothing, it's accepted."""

    test_the_vendor_questions_from_the_call_are_recognised = None
    test_details_line_and_prompt = None
    test_code_answers_with_the_details_and_asks_the_rate = None

    async def run_hook(self, vendor_says):
        hooks, captured, spoken = [], {}, []

        class Pair:
            def __init__(self, context, **kwargs):
                captured["context"] = context

            def user(self):
                return DummyProcessor()

            def assistant(self):
                return DummyProcessor()

        def before_reply(*given):
            hooks.extend(given)
            return DummyProcessor()

        class Runner:
            def __init__(self, **kwargs):
                pass

            async def run(self, task):
                context = captured["context"]
                context.add_message({"role": "assistant", "content": "पर प्लेट रेट कितना होगा?"})
                context.add_message({"role": "user", "content": vendor_says})
                frames = hooks[1]()
                spoken.append(frames[0].text if frames else None)

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="C02",
                                         event_id=self.event_id, call_id="synthetic-bargain")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=FakeLLM()), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        return spoken[0]

    async def test_above_our_maximum_mohan_says_it_right_away(self):
        said = await self.run_hook("40 guest हैं ना, तो per plate होगा आपका 1200 रुपये।")  # budget 25000 / 40 = 620 max
        self.assertEqual(said, "सर, हम मैक्सिमम 620 रुपये पर प्लेट तक कर सकते हैं, इतने में हो जाएगा?")

    async def test_in_range_is_just_accepted(self):
        self.assertIsNone(await self.run_hook("600 रुपये पर प्लेट"))
