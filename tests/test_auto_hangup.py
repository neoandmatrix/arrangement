import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndTaskFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from conversation import AutoHangup

DOWN, UP = FrameDirection.DOWNSTREAM, FrameDirection.UPSTREAM


class AutoHangupTest(unittest.IsolatedAsyncioTestCase):
    def make(self, *, asking=False):
        self.turns = 3
        hangup = AutoHangup(user_turns=lambda: self.turns, awaiting_answer=lambda: asking, outcome={}, grace_secs=0.05)
        hangup.push_frame = AsyncMock()
        return hangup

    async def feed(self, hangup, *frames):
        with patch.object(FrameProcessor, "process_frame", AsyncMock()):
            for frame in frames:
                await hangup.process_frame(frame, DOWN)

    def ended(self, hangup):
        return any(isinstance(c.args[0], EndTaskFrame) and c.args[1] == UP for c in hangup.push_frame.call_args_list)

    async def test_hangs_up_after_the_goodbye_once_the_quote_is_saved(self):
        hangup = self.make()
        hangup.arm()
        await self.feed(hangup, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
        await asyncio.sleep(0.1)
        self.assertTrue(self.ended(hangup))
        self.assertIn("automatically", hangup.outcome["end_reason"])

    async def test_never_hangs_up_before_the_job_is_done(self):
        hangup = self.make()
        await self.feed(hangup, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
        await asyncio.sleep(0.1)
        self.assertFalse(self.ended(hangup))

    async def test_speech_that_started_before_the_save_is_not_the_goodbye(self):
        hangup = self.make()
        await self.feed(hangup, BotStartedSpeakingFrame())
        hangup.arm()  # record_quote saved while "ok ji" was still playing
        await self.feed(hangup, BotStoppedSpeakingFrame())
        await asyncio.sleep(0.1)
        self.assertFalse(self.ended(hangup))

    async def test_waits_if_the_vendor_speaks_or_was_asked_something(self):
        hangup = self.make()
        hangup.arm()
        await self.feed(hangup, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(), UserStartedSpeakingFrame())
        await asyncio.sleep(0.1)
        self.assertFalse(self.ended(hangup))

        asking = self.make(asking=True)
        asking.arm()
        await self.feed(asking, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
        await asyncio.sleep(0.1)
        self.assertFalse(self.ended(asking))

    async def test_a_new_vendor_turn_cancels_the_pending_hang_up(self):
        hangup = self.make()
        hangup.arm()
        await self.feed(hangup, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
        self.turns += 1  # they said something that STT finished before the timer fired
        await asyncio.sleep(0.1)
        self.assertFalse(self.ended(hangup))


if __name__ == "__main__":
    unittest.main()
