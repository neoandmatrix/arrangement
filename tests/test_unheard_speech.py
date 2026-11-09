import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, TranscriptionFrame,
                                   TTSSpeakFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from conversation import ASK_AGAIN, UnheardSpeech


class UnheardSpeechTest(unittest.IsolatedAsyncioTestCase):
    def make(self):
        watcher = UnheardSpeech(lambda: [TTSSpeakFrame(ASK_AGAIN["hi-IN"])], grace_secs=0.05)
        watcher.push_frame = AsyncMock()
        return watcher

    async def feed(self, watcher, *frames, spoke=1.5):
        with patch.object(FrameProcessor, "process_frame", AsyncMock()):
            for frame in frames:
                if isinstance(frame, VADUserStoppedSpeakingFrame):
                    watcher.speech_started = time.monotonic() - spoke - frame.stop_secs
                await watcher.process_frame(frame, FrameDirection.DOWNSTREAM)

    def asked(self, watcher):
        return [c.args[0].text for c in watcher.push_frame.call_args_list if isinstance(c.args[0], TTSSpeakFrame)]

    async def test_speech_with_no_words_gets_one_please_repeat(self):
        watcher = self.make()
        await self.feed(watcher, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(stop_secs=0.8))
        await asyncio.sleep(0.1)
        self.assertEqual(self.asked(watcher), [ASK_AGAIN["hi-IN"]])
        await self.feed(watcher, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(stop_secs=0.8))
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.asked(watcher)), 1)  # only once until they are heard again

    async def test_words_or_short_noise_or_mohan_talking_do_not_trigger_it(self):
        heard = self.make()
        await self.feed(heard, VADUserStartedSpeakingFrame(), TranscriptionFrame("500 रुपये", "u", "t"),
                        VADUserStoppedSpeakingFrame(stop_secs=0.8))
        noise = self.make()
        await self.feed(noise, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(stop_secs=0.8), spoke=0.3)
        talking = self.make()
        await self.feed(talking, BotStartedSpeakingFrame(), VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(stop_secs=0.8))
        await asyncio.sleep(0.1)
        self.assertEqual(self.asked(heard) + self.asked(noise) + self.asked(talking), [])

    async def test_re_armed_after_they_are_heard(self):
        watcher = self.make()
        await self.feed(watcher, VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(stop_secs=0.8))
        await asyncio.sleep(0.1)
        await self.feed(watcher, TranscriptionFrame("हाँ", "u", "t"), BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(),
                        VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(stop_secs=0.8))
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.asked(watcher)), 2)


if __name__ == "__main__":
    unittest.main()
