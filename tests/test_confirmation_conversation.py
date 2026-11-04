"""Actual confirmation tool/router handlers with synthetic speech and audio-delivery events."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import EndTaskFrame, LLMContextFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import confirmation_rules
import confirmations
import conversation
import prompts
import store
import workflow
from price_fixture import checked_quote


class Processor(FrameProcessor):
    pass


class LLM(Processor):
    def __init__(self):
        super().__init__()
        self._settings = SimpleNamespace(model='synthetic-confirmation')
        self.handlers = {}
        self.pushed = []

    def register_function(self, name, handler):
        self.handlers[name] = handler

    async def push_frame(self, frame, direction):
        self.pushed.append(frame)


class TTS(Processor):
    def __init__(self, **kwargs):
        super().__init__()
        self.events = {}

    @staticmethod
    def Settings(**kwargs):
        return SimpleNamespace(**kwargs)

    def event_handler(self, name):
        def register(callback):
            self.events[name] = callback
            return callback
        return register


class ConfirmationConversationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'voice-confirmation.db'
        self.event_id = store.save_event(prompts.SAMPLE_BRIEF)
        self.quote = checked_quote({'available': True, 'price_amount': 11000, 'price_unit': 'total',
            'transport_charge': 0, 'extra_charges_amount': 0, 'advance_amount': 0}, 'priest')
        store.save_quote(self.event_id, 'P01', None, self.quote)
        self.confirmation_id = workflow.book('priest', 'P01')['confirmation_id']
        confirmations.claim(self.confirmation_id)

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    async def run_voice(self, script):
        holder = {}
        results = []
        llm = LLM()
        tts = TTS()
        tracker_class = conversation.ConfirmationSpeechTracker
        router_class = conversation.ConfirmationAnswerRouter

        class Pair:
            def __init__(self, context, **kwargs):
                holder['context'] = context

            def user(self):
                return Processor()

            def assistant(self):
                return Processor()

        class Task:
            def __init__(self, pipeline, **kwargs):
                self.frames = []
                holder['task'] = self

            def event_handler(self, name):
                return lambda callback: callback

            async def queue_frames(self, frames):
                self.frames.extend(frames)

        def tracker_factory(state, turns):
            holder['tracker'] = tracker_class(state, turns)
            return holder['tracker']

        def router_factory(state, advance):
            holder['router'] = router_class(state, advance)
            return holder['router']

        async def callback(value, **kwargs):
            results.append(value)

        async def tool(name, **arguments):
            await llm.handlers[name](SimpleNamespace(function_name=name, arguments=arguments, result_callback=callback, llm=llm))

        def say(text):
            holder['context'].add_message({'role': 'user', 'content': text})

        async def deliver():
            frame = next(f for f in reversed(holder['task'].frames) if isinstance(f, TTSSpeakFrame))
            context_id = f"synthetic-audio-{len(holder['task'].frames)}"
            await tts.events['on_tts_request'](tts, context_id, frame.text)
            self.assertFalse(holder['tracker'].mark_delivery('wrong-audio-context'))
            self.assertTrue(holder['tracker'].mark_delivery(context_id))
            return frame.text

        class Runner:
            def __init__(self, **kwargs):
                pass

            async def run(self, task):
                await script(holder, tool, say, deliver, results, llm)

        transport = SimpleNamespace(input=lambda: Processor(), output=lambda: Processor(), event_handler=lambda name: lambda callback: callback)
        cfg = conversation.SessionConfig(mode='confirmation', channel='app', vendor_id='P01', event_id=self.event_id,
                                         confirmation_id=self.confirmation_id, call_id='synthetic-confirmation-call')
        with patch.dict('os.environ', {'GNANI_API_KEY': 'synthetic-test-key'}), \
             patch('conversation.GnaniSTTService', return_value=Processor()), \
             patch('conversation.GnaniTTSService', return_value=tts) as tts_type, \
             patch('conversation.make_llm', return_value=llm), \
             patch('conversation.SileroVADAnalyzer'), \
             patch('conversation.LLMContextAggregatorPair', Pair), \
             patch('conversation.PipelineTask', Task), \
             patch('conversation.WorkerRunner', Runner), \
             patch('conversation.ConfirmationSpeechTracker', tracker_factory), \
             patch('conversation.ConfirmationAnswerRouter', router_factory):
            tts_type.Settings.side_effect = TTS.Settings
            await conversation.run_conversation(transport, cfg, sample_rate_in=16000, sample_rate_out=24000)
        return results

    async def test_a_greeting_yes_cannot_confirm_the_booking(self):
        async def script(holder, tool, say, deliver, results, llm):
            say('yes')
            await tool('record_confirmation', status='agreed')
            self.assertFalse(results[-1]['ok'])
            self.assertEqual(store.confirmation(self.confirmation_id)['answers'], [])
        await self.run_voice(script)
        self.assertNotEqual(confirmations.primary(self.event_id, 'priest')['status'], 'active')

    async def test_end_call_is_refused_while_a_readback_is_unanswered(self):
        async def script(holder, tool, say, deliver, results, llm):
            say('yes')
            await tool('start_confirmation')
            await tool('end_call', reason='Already done', callback_requested=False)
            self.assertFalse(results[-1]['ok'])
            self.assertFalse(any(isinstance(f, EndTaskFrame) for f in llm.pushed))
        await self.run_voice(script)

    async def test_yes_before_tts_completion_does_not_count(self):
        async def script(holder, tool, say, deliver, results, llm):
            say('yes')
            await tool('start_confirmation')
            say('yes')
            await tool('record_confirmation', status='agreed')
            self.assertFalse(results[-1]['ok'])
            self.assertEqual(store.confirmation(self.confirmation_id)['answers'], [])
        await self.run_voice(script)

    async def test_repeat_reuses_the_approved_question_without_advancing(self):
        async def script(holder, tool, say, deliver, results, llm):
            say('yes')
            await tool('start_confirmation')
            first = await deliver()
            say('Please repeat that')
            await tool('record_confirmation', status='repeat')
            self.assertEqual(await deliver(), first)
            self.assertEqual(store.confirmation(self.confirmation_id)['answers'], [])
        await self.run_voice(script)

    async def test_each_delivered_code_question_is_required_and_clear_yeses_skip_llm(self):
        async def script(holder, tool, say, deliver, results, llm):
            say('yes')
            await tool('start_confirmation')
            expected = confirmation_rules.questions(store.confirmation(self.confirmation_id)['terms'], store.vendor('P01')['language'])
            for index, question in enumerate(expected):
                self.assertEqual(await deliver(), question['text'])
                say('yes')
                router = holder['router']
                router.push_frame = AsyncMock()
                with patch.object(FrameProcessor, 'process_frame', new_callable=AsyncMock):
                    await router.process_frame(LLMContextFrame(holder['context']), FrameDirection.DOWNSTREAM)
                router.push_frame.assert_not_awaited()
                self.assertEqual(len(store.confirmation(self.confirmation_id)['answers']), index + 1)
                if index < len(expected) - 1:
                    self.assertEqual(store.checkins(self.event_id), [])
            await tool('end_call', reason='All approved terms confirmed', callback_requested=False)
            self.assertTrue(results[-1]['ok'])
        await self.run_voice(script)
        self.assertEqual(store.confirmation(self.confirmation_id)['status'], 'confirmed')
        self.assertEqual(confirmations.primary(self.event_id, 'priest')['status'], 'active')
        self.assertEqual(len(store.checkins(self.event_id)), 4)

    async def test_a_changed_quote_is_saved_for_family_review_not_confirmed(self):
        async def revised_script(holder, tool, say, deliver, results, llm):
            say('Base price changed to 12000')
            await tool('record_confirmation', status='changed', change_type='quote', notes='Base price is now 12000')
            for text in ('Base price 12000 in total', 'Transport 0', 'Extra charges 0', 'Advance 0'):
                say(text)
            args = {k: v for k, v in self.quote.items() if k in conversation.RECORD_QUOTE.properties}
            args['price_amount'] = 12000
            await tool('record_quote', **args)
        await self.run_voice(revised_script)
        self.assertEqual(store.latest_quotes(self.event_id)['P01']['price_amount'], 11000)
        self.assertEqual(confirmations.primary(self.event_id, 'priest')['status'], 'awaiting_changes')
        self.assertEqual(store.updates(self.event_id)[0]['status'], 'pending')
