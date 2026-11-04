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

    async def push_frame(self, frame, direction):
        pass


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

    async def run_handlers(self, args, speech, attempts):
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
                for _ in range(attempts):
                    await llm.handlers["record_quote"](SimpleNamespace(function_name="record_quote", arguments=args, result_callback=callback, llm=llm))

        transport = SimpleNamespace(input=lambda: DummyProcessor(), output=lambda: DummyProcessor(),
            event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode="vendor_call", channel="app", vendor_id="P01",
                                         event_id=self.event_id, call_id="synthetic-price-call")
        with patch.dict("os.environ", {"GNANI_API_KEY": "synthetic-test-key"}), \
             patch("conversation.GnaniSTTService", return_value=DummyProcessor()), \
             patch("conversation.GnaniTTSService", return_value=DummyProcessor()), \
             patch("conversation.make_llm", return_value=llm), \
             patch("conversation.SileroVADAnalyzer"), \
             patch("conversation.LLMContextAggregatorPair", Pair), \
             patch("conversation.WorkerRunner", Runner):
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        return results

    async def test_exhausted_retries_save_only_a_blocked_quote(self):
        results = await self.run_handlers(self.args, ["Yes, I am available."], 3)
        self.assertFalse(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertTrue(results[2]["ok"])
        self.assertFalse(results[2]["bookable"])
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
