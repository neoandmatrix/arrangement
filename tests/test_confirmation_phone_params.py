"""Signalling parameters only: no real phone numbers, credentials or Twilio API calls."""

import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from conversation import SessionConfig


class ConfirmationPhoneParamsTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_and_event_ids_survive_twiml_custom_parameters(self):
        with patch('dotenv.load_dotenv', return_value=False):
            import server
        with patch('server.public_host', return_value='synthetic.invalid'):
            response = await server.twiml(mode='confirmation', vendor_id='K01', confirmation_id='12', event_id='5', checkin_attempt_id='8')
        root = ET.fromstring(response.body)
        parameters = {item.attrib['name']: item.attrib['value'] for item in root.findall('.//Parameter')}
        self.assertEqual(parameters['confirmation_id'], '12')
        self.assertEqual(parameters['event_id'], '5')
        cfg = SessionConfig.from_dict(parameters, channel='phone', call_id='synthetic-phone-call')
        self.assertEqual(cfg.mode, 'confirmation')
        self.assertEqual(cfg.confirmation_id, 12)
        self.assertEqual(cfg.event_id, 5)
        self.assertEqual(cfg.checkin_attempt_id, 8)
