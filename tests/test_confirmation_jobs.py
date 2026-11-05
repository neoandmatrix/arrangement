"""Serial, retryable confirmation jobs with fake call channels."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import confirmations
import enquiries
import prompts
import store
import workflow
from price_fixture import checked_quote


class ConfirmationJobsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'confirmation-jobs.db'
        self.event_id = store.save_event(prompts.SAMPLE_NAAMKARAN)
        store.save_quote(self.event_id, 'K01', None, checked_quote({'available': True, 'price_amount': 2400,
            'price_unit': 'total', 'transport_charge': 0, 'advance_amount': 500}, 'cake'))
        self.confirmation_id = workflow.book('cake', 'K01')['confirmation_id']

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    async def test_ring_decline_and_no_answer_do_not_mean_a_spoken_booking_refusal(self):
        for result in ('declined', 'no_answer', 'unreachable'):
            with patch('channels.call', new_callable=AsyncMock, return_value={'result': result, 'call_id': None}) as call:
                outcome = await enquiries.confirmation_call(self.confirmation_id)
                self.assertEqual(outcome, 'no_answer')
                self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'confirmation_failed')
                self.assertTrue(store.latest_quotes(self.event_id)['K01']['available'])
                self.assertEqual(call.call_args.args[1]['mode'], 'confirmation')
                self.assertEqual(call.call_args.args[1]['confirmation_id'], self.confirmation_id)
            self.confirmation_id = confirmations.request(self.event_id, 'cake')

    async def test_duplicate_job_cannot_ring_the_vendor_twice(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fake_call(*args):
            started.set()
            await release.wait()
            return {'result': 'no_answer', 'call_id': None}

        with patch('channels.call', new_callable=AsyncMock, side_effect=fake_call) as call:
            first = asyncio.create_task(enquiries.confirmation_call(self.confirmation_id))
            await started.wait()
            self.assertEqual(await enquiries.confirmation_call(self.confirmation_id), 'already_started')
            release.set()
            await first
            call.assert_awaited_once()

    async def test_answering_without_recording_confirmation_is_not_success(self):
        with patch('channels.call', new_callable=AsyncMock, return_value={'result': 'answered', 'call_id': 'synthetic-call'}):
            self.assertEqual(await enquiries.confirmation_call(self.confirmation_id), 'failed')
        self.assertEqual(store.checkins(self.event_id), [])

    async def test_channel_failure_remains_retryable(self):
        with patch('channels.call', new_callable=AsyncMock, side_effect=RuntimeError('synthetic failure')):
            self.assertEqual(await enquiries.confirmation_call(self.confirmation_id), 'failed')
        self.assertNotEqual(confirmations.request(self.event_id, 'cake'), self.confirmation_id)

    async def test_successful_primary_call_automatically_queues_standby_confirmation(self):
        from confirmation_fixture import finish_confirmation
        standby_done = asyncio.Event()
        store.save_quote(self.event_id, 'K03', None, checked_quote({'available': True, 'price_amount': 2200,
            'price_unit': 'total', 'transport_charge': 0, 'advance_amount': 400}, 'cake'))
        store.set_booking(self.event_id, 'cake', 'backup', 'K03', 'awaiting_backup_confirmation')

        async def fake_call(vendor, session, caller):
            finish_confirmation(session['confirmation_id'])
            if caller['purpose'] == 'standby_confirmation':
                standby_done.set()
            return {'result': 'answered', 'call_id': f"synthetic-confirmation-{session['confirmation_id']}"}

        with patch('channels.call', new_callable=AsyncMock, side_effect=fake_call):
            await enquiries.confirmation_call(self.confirmation_id)
            await asyncio.wait_for(standby_done.wait(), 2)
        self.assertEqual(confirmations.summary(self.event_id, 'cake', 'backup')['purpose'], 'standby')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K01')
