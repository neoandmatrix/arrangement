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

    async def test_no_please_repeat_line_at_save(self):
        args = {**self.args, "price_amount": 40000}
        speech = ["Base price 40000 in total", "Transport 0", "Extra charges 0", "Advance 0"]
        results = await self.run_handlers(args, speech, 1)
        self.assertNotIn("ठीक से सुना नहीं", str(results))

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
                                            ("सर, ये रेट बहुत हाई है, हम मैक्सिमम 620 रुपये पर प्लेट तक कर सकते हैं, इतने में हो जाएगा?", "पर plate 5,000।"),
                                            ("सर, 740 रुपये पर प्लेट कर दीजिए, हो जाएगा?", "अच्छा 700 कर देंगे")]:
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
        # max 620 a plate (25000 for 40 guests): far above it, we open with the maximum; no "please repeat" line
        self.assertEqual(notes[0], "सर, ये रेट बहुत हाई है, हम मैक्सिमम 620 रुपये पर प्लेट तक कर सकते हैं, इतने में हो जाएगा?")
        self.assertEqual(notes[1], "सर, 740 रुपये पर प्लेट कर दीजिए, हो जाएगा?")  # halfway, capped at 20% over our maximum
        self.assertEqual(notes[2], "ठीक है सर, 700 रुपये पर प्लेट फाइनल।")


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

    async def test_above_range_mohan_counters_right_away(self):
        said = await self.run_hook("40 guest हैं ना, तो per plate होगा आपका 700 रुपये।")  # 620 max + 60
        self.assertEqual(said, "सर, 650 रुपये पर प्लेट कर दीजिए, हो जाएगा?")

    async def test_in_range_is_just_accepted(self):
        self.assertIsNone(await self.run_hook("600 रुपये पर प्लेट"))


class BargainFlowTest(BargainNowTest):
    """From a live call: after "650 कर दीजिए", "नहीं, 700 करिए" must be settled, not met with "620 तक" forever."""

    test_above_range_mohan_counters_right_away = None
    test_in_range_is_just_accepted = None

    async def run_script(self, lines):
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
                context, mohan = captured["context"], "पर प्लेट रेट कितना होगा?"
                for vendor in lines:
                    context.add_message({"role": "assistant", "content": mohan})
                    context.add_message({"role": "user", "content": vendor})
                    frames = hooks[1]() or []
                    mohan = next((f.text for f in frames if hasattr(f, "text")), None) or "next question"
                    spoken.append(mohan)

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="C06",
                                         event_id=self.event_id, call_id="synthetic-flow")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=FakeLLM()), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        return spoken

    async def test_the_live_call_now_settles_on_their_counter(self):
        spoken = await self.run_script(["2800", "700 कर सकते हैं आप?", "नहीं, 700 करिए, 700 में हो जाएगा।"])
        self.assertIn("मैक्सिमम 620", spoken[0])
        self.assertEqual(spoken[1], "सर, 660 रुपये पर प्लेट कर दीजिए, हो जाएगा?")
        self.assertEqual(spoken[2], "ठीक है सर, 700 रुपये पर प्लेट फाइनल।")

    async def test_holding_a_far_too_high_price_goes_to_the_family_not_final(self):
        spoken = await self.run_script(["5000", "3000", "3000"])
        self.assertIn("मैक्सिमम 620", spoken[0])
        self.assertEqual(spoken[1], "सर, 740 रुपये पर प्लेट कर दीजिए, हो जाएगा?")
        self.assertEqual(spoken[2], "ठीक है सर, मैं 3000 रुपये पर प्लेट फैमिली को बता दूँगा, वो डिसाइड करेंगे।")


class BargainFlowFixesTest(BargainFlowTest):
    """From a live call: 2800 caught late, "900 में हो जाएगा" countered with 800, and the confirmed quote never saved."""

    test_the_live_call_now_settles_on_their_counter = None

    async def test_rate_in_another_clause_is_caught_right_away(self):
        spoken = await self.run_script(["खाना पर plate rate होगा आपका कुछ, यही 2800 रुपये।"])
        self.assertIn("मैक्सिमम 620", spoken[0])

    async def test_agreeing_to_our_counter_settles_it(self):
        spoken = await self.run_script(["900 रुपये पर प्लेट।", "740 में हो जाएगा।"])
        self.assertEqual(spoken, ["सर, 740 रुपये पर प्लेट कर दीजिए, हो जाएगा?", "ठीक है सर, 740 रुपये पर प्लेट फाइनल।"])
        spoken = await self.run_script(["800", "हाँ ठीक है, हो जाएगा"])
        self.assertEqual(spoken[1], "ठीक है सर, 700 रुपये पर प्लेट फाइनल।")

    def test_readback_and_agreement_helpers(self):
        self.assertTrue(conversation.amount_in_readback("तो 800 रुपये पर प्लेट, ट्रांसपोर्ट 2000 रुपये, सही है?"))
        self.assertFalse(conversation.amount_in_readback("ट्रांसपोर्ट का कोई चार्ज है क्या?"))
        self.assertTrue(conversation.agrees("हाँ ठीक है, हो जाएगा"))
        self.assertFalse(conversation.agrees("नहीं, 700 करिए"))


class SecondMessageTest(BargainFlowTest):
    """From a live call: "3500" then "और delivery लेगा?" before the counter was heard made code settle at 3500."""

    test_the_live_call_now_settles_on_their_counter = None

    async def test_an_old_message_is_not_read_again(self):
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
                for vendor in ["800 रुपये पर प्लेट", "और delivery लेगा?"]:  # the counter isn't in the context yet
                    context.add_message({"role": "user", "content": vendor})
                    frames = hooks[1]() or []
                    spoken.append(next((f.text for f in frames if hasattr(f, "text")), None))

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="C06",
                                         event_id=self.event_id, call_id="synthetic-second")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=FakeLLM()), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        self.assertEqual(spoken, ["सर, 700 रुपये पर प्लेट कर दीजिए, हो जाएगा?", None])


class TimingTest(FunctionDetailsTest):
    """From a live call: "cake कितने बजे देना है?" was ignored four times."""

    test_the_vendor_questions_from_the_call_are_recognised = None
    test_details_line_and_prompt = None
    test_code_answers_with_the_details_and_asks_the_rate = None

    def setUp(self):
        super().setUp()
        self.event_id = store.save_event(prompts.SAMPLE_NAAMKARAN)

    async def test_code_answers_the_time_and_repeats_the_pending_question(self):
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
                context.add_message({"role": "assistant", "content": "क्या आप दो किलो एगलेस चॉकलेट केक बना पाएंगे?"})
                context.add_message({"role": "user", "content": "कब देना है cake? कितने बजे देना है?"})
                frames = hooks[3]()  # [start_review, high_price_hint, function_details_hint, timing_hint]
                spoken.append(frames[0].text if frames else None)

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="K02",
                                         event_id=self.event_id, call_id="synthetic-timing")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=FakeLLM()), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        self.assertEqual(spoken[0], "फंक्शन 22 नवंबर को है, मुहूर्त सुबह 10 बजे का है, केक सुबह 8 बजे तक चाहिए। "
                                    "क्या आप दो किलो एगलेस चॉकलेट केक बना पाएंगे?")


class AdvanceTest(FunctionDetailsTest):
    """From a live call: "एडवांस 50,000" on a 10,000 job went by without a word."""

    test_the_vendor_questions_from_the_call_are_recognised = None
    test_details_line_and_prompt = None
    test_code_answers_with_the_details_and_asks_the_rate = None

    async def run_advance(self, advance):
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
                for mohan, vendor, hook in [("पैकेज का रेट कितना होगा?", "10000", 1),
                                            ("ट्रांसपोर्ट का कोई चार्ज है क्या?", "हाँ, 2000", 4),
                                            ("एडवांस कितना चाहिए?", advance, 4)]:
                    context.add_message({"role": "assistant", "content": mohan})
                    context.add_message({"role": "user", "content": vendor})
                    frames = hooks[hook]() or []
                    spoken.append(next((f.text for f in frames if hasattr(f, "text")), None))

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
                                    event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="D02",
                                         event_id=self.event_id, call_id="synthetic-advance")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key", "SUPERVISOR": "0"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=FakeLLM()), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.BeforeReply", before_reply), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        return spoken[-1]

    async def test_advance_above_the_total_is_challenged(self):
        self.assertEqual(await self.run_advance("50,000 50,000"),
                         "सर, एडवांस टोटल से ज़्यादा नहीं हो सकता, टोटल लगभग 12000 रुपये बन रहा है। एडवांस कितना लेंगे?")

    async def test_advance_above_half_gets_one_polite_ask(self):
        self.assertEqual(await self.run_advance("8000"), "सर, आधे से ज़्यादा एडवांस मुश्किल है, 6000 रुपये तक एडवांस हो जाएगा?")

    async def test_a_normal_advance_passes(self):
        self.assertIsNone(await self.run_advance("3000"))
