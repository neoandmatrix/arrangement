"""Offline synthetic workflow rehearsal. This test is not a recorded or live Gnani voice demo."""

from unittest.mock import patch
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app_api
import backups
import checkin_dispatch
import checkin_policy
import confirmations
import prompts
import rules
import store
import workflow
from checkin_scheduler import CheckinScheduler
from confirmation_fixture import finish_confirmation
from price_fixture import checked_quote


@patch.object(backups, "STANDBY_CALLS", True)  # the standby-call flow, off by default since 2026-11-09
@patch.object(workflow, "BACKUP_ONLY_ON_CANCEL", False)
class DemoWalkthroughTest(unittest.IsolatedAsyncioTestCase):
    async def test_housewarming_quotes_confirmation_standby_failure_and_activation(self):
        original = store.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            store.DB_PATH = Path(directory) / 'synthetic-demo.db'
            try:
                brief = {**prompts.SAMPLE_BRIEF, 'family_name': 'Demo Sharma', 'family_id': 'F12345678',
                    'date': (checkin_policy.now() + timedelta(days=4)).date().isoformat(), 'start_time': '09:00',
                    'needs': ['caterer'], 'location': {'lat': 12.9121, 'lon': 77.6446}, 'budget': {'caterer': 25000}}
                event_id = store.save_event(brief)
                shortlist = {row['vendor_id'] for row in rules.shortlist('caterer', store.vendors(), brief)}
                self.assertTrue({'C02', 'C03'}.issubset(shortlist))
                for vendor_id, opening, final, advance in (('C02', 500, 480, 4000), ('C03', 520, 500, 5000)):
                    quote = checked_quote({'available': True, 'price_amount': final, 'initial_price_amount': opening,
                        'price_unit': 'per_plate', 'transport_charge': 0, 'extra_charges_amount': 0, 'gst_included': True,
                        'advance_amount': advance, 'includes_notes': 'Vegetarian meal for forty guests, serving staff included'}, 'caterer')
                    store.save_quote(event_id, vendor_id, 'synthetic-quote', quote)
                    store.set_enquiry(event_id, vendor_id, 'caterer', 'quoted')
                    self.assertEqual(rules.savings(quote, brief), 800)
                chosen = workflow.book('caterer', 'C03')
                self.assertEqual(chosen['backup'], 'C02')
                self.assertEqual(confirmations.primary(event_id, 'caterer')['status'], 'awaiting_confirmation')
                finish_confirmation(chosen['confirmation_id'])
                standby_id = store.latest_confirmation(event_id, 'caterer', 'backup')['id']
                finish_confirmation(standby_id)
                self.assertTrue(backups.secured(event_id, 'caterer'))
                self.assertEqual(confirmations.primary(event_id, 'caterer')['vendor_id'], 'C03')

                checkin = store.checkins(event_id)[0]
                at = checkin_policy.venue_time(checkin['due_at']) + timedelta(seconds=1)
                async def cancelled_call(vendor, session, caller, **kwargs):
                    self.assertTrue(kwargs['before_dial']())
                    call_id = 'synthetic-cancellation-checkin'
                    checkin_dispatch.bind(session['checkin_attempt_id'], session['checkin_id'], call_id, vendor['id'], event_id)
                    workflow.record_checkin(session['checkin_id'], 'cancelled', None, 'Caterer cannot fulfil the booking',
                        attempt_id=session['checkin_attempt_id'], call_id=call_id)
                    return {'result': 'answered', 'call_id': call_id}
                with patch('enquiries.queue_pending_confirmations'), patch('channels.call', new_callable=AsyncMock, side_effect=cancelled_call):
                    await CheckinScheduler(clock=lambda: at).tick()
                self.assertEqual(confirmations.primary(event_id, 'caterer')['status'], 'replacement_pending')
                self.assertEqual(store.checkins(event_id)[0]['status'], 'cancelled')
                activation_id = store.latest_confirmation(event_id, 'caterer', 'backup')['id']
                self.assertNotEqual(activation_id, standby_id)
                finish_confirmation(activation_id)
                state = app_api.event_status(event_id, brief)
                service = state['services'][0]
                self.assertEqual(service['booking']['primary']['vendor_id'], 'C02')
                self.assertEqual(service['booking']['primary']['status'], 'active')
                self.assertEqual(service['booking']['primary']['confirmation']['purpose'], 'replacement')
                self.assertEqual(service['booking']['primary']['confirmation']['terms']['total'], 19200)
                self.assertTrue(all(c['vendor_id'] == 'C02' for c in store.checkins(event_id) if c['status'] == 'pending'))
            finally:
                store.DB_PATH = original
