import unittest
from unittest.mock import AsyncMock, patch

import supervisor


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_passes_on_only_real_notes(self):
        self.assertIsNone(supervisor.parse('{"ok": true}'))
        self.assertEqual(supervisor.parse('```json\n{"ok": false, "note": "Ask the advance; it was skipped."}\n```'),
                         "Ask the advance; it was skipped.")
        self.assertIsNone(supervisor.parse('{"ok": false}'))
        self.assertIsNone(supervisor.parse("not json"))

    def test_render_labels_both_sides(self):
        text = supervisor.render([{"role": "bandobast", "text": "रेट क्या है?"}, {"role": "vendor_or_family", "text": "500"}], "Caterer")
        self.assertIn("Mohan: रेट क्या है?", text)
        self.assertIn("Vendor (speech-to-text): 500", text)

    def test_off_without_a_key_or_when_disabled(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "", "SUPERVISOR": "1"}):
            self.assertFalse(supervisor.enabled())
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "k", "SUPERVISOR": "0"}):
            self.assertFalse(supervisor.enabled())

    async def test_errors_never_reach_the_call(self):
        client = AsyncMock()
        client.chat.completions.create.side_effect = TimeoutError()
        with patch.object(supervisor, "_llm", return_value=client):
            self.assertIsNone(await supervisor.review([{"role": "vendor_or_family", "text": "hi"}], "x"))


if __name__ == "__main__":
    unittest.main()
