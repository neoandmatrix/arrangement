import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog
import conversation
import greeting
import prompts
from pipecat.frames.frames import TTSAudioRawFrame, TTSSpeakFrame

PRIEST = {"name": "Pandit Ramakant Sharma", "category": "priest", "language": "hi-IN"}
CATERER = {"name": "Udupi Ruchi Catering", "category": "caterer", "language": "kn-IN"}


class GreetingTest(unittest.TestCase):
    def test_fixed_first_line_per_language(self):
        self.assertEqual(greeting.opening_line(PRIEST, "hi-IN"),
                         "हैलो, हाँ जी, मैं मोहन बोल रहा हूँ, Pandit Ramakant Sharma जी से बात हो रही है क्या?")
        self.assertIn("Udupi Ruchi Catering", greeting.opening_line(CATERER, "kn-IN"))
        self.assertIsNone(greeting.opening_line(CATERER, "bn-IN"))  # the model says it then

    def test_prompt_knows_the_greeting_was_already_played(self):
        text = prompts.vendor_prompt(vendor={**CATERER, "language": "hi-IN"}, brief=prompts.SAMPLE_BRIEF)
        self.assertIn("already opened with your greeting", text)
        self.assertIn("Don't greet again", text)

    def test_saved_audio_plays_without_tts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(greeting, "CACHE", Path(tmp)):
            line = greeting.opening_line(PRIEST, "hi-IN")
            greeting._path(line, "hi-IN", 24000).write_bytes(b"\x01\x00" * 480)
            frames = conversation.greeting_frames(line, "hi-IN", 24000)
            audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
            self.assertEqual(len(audio), 1)
            self.assertEqual(audio[0].sample_rate, 24000)
            self.assertFalse(any(isinstance(f, TTSSpeakFrame) for f in frames))


class MissingGreetingTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_audio_falls_back_to_live_tts_and_saves_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(greeting, "CACHE", Path(tmp)), \
                patch.object(greeting, "_synthesize", return_value=b"\x02\x00" * 10) as synth:
            line = greeting.opening_line(PRIEST, "hi-IN")
            frames = conversation.greeting_frames(line, "hi-IN", 24000)
            self.assertIsInstance(frames[0], TTSSpeakFrame)
            self.assertFalse(frames[0].append_to_context)  # added to the context by hand, once
            for _ in range(50):
                if greeting.cached(line, "hi-IN", 24000):
                    break
                import asyncio
                await asyncio.sleep(0.01)
            self.assertEqual(greeting.cached(line, "hi-IN", 24000), b"\x02\x00" * 10)
            synth.assert_called_once()


class WishQuestionTest(unittest.TestCase):
    def test_family_wish_is_its_own_plain_question(self):
        questions = catalog.checklist("photographer", prompts.SAMPLE_NAAMKARAN)
        wish = [q for q in questions if "what the family wants" in q]
        self.assertEqual(len(wish), 1)
        self.assertNotIn("how many hours", " ".join(questions))
        plain = catalog.checklist("photographer", {**prompts.SAMPLE_NAAMKARAN, "details": {}})
        self.assertIn("how many hours they cover", plain)


if __name__ == "__main__":
    unittest.main()
