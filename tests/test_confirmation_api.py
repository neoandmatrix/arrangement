"""Confirmation API starts/retries calls only after family approval; no provider network access."""

import backups
from unittest.mock import patch
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app_api
import confirmations
import prompts
import store
import workflow
import checkin_policy
from confirmation_fixture import finish_confirmation
from price_fixture import checked_quote


@patch.object(backups, "STANDBY_CALLS", True)  # the standby-call flow, off by default since 2026-11-09
@patch.object(workflow, "BACKUP_ONLY_ON_CANCEL", False)
class ConfirmationAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'confirmation-api.db'
        self.env = patch.dict(os.environ, {'APP_KEY': 'synthetic-test-key'})
        self.env.start()
        self.family = 'F12345678'
        self.other_family = 'F87654321'
        store.save_participant(self.family, None)
        store.save_participant(self.other_family, None)
        self.brief = {**prompts.SAMPLE_NAAMKARAN, 'family_id': self.family}
        self.event_id = store.save_event(self.brief)
        for vendor_id in ('K01', 'K03'):
            store.save_quote(self.event_id, vendor_id, None, checked_quote({'available': True, 'price_amount': 2400,
                'price_unit': 'total', 'transport_charge': 100, 'extra_charges_amount': 0, 'advance_amount': 500}, 'cake'))
            store.set_enquiry(self.event_id, vendor_id, 'cake', 'quoted')
        app = FastAPI()
        app.include_router(app_api.router)
        self.client = TestClient(app)
        self.client.__enter__()
        self.calls_patch = patch('enquiries.confirmation_call', new_callable=AsyncMock, return_value='pending')
        self.calls = self.calls_patch.start()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.calls_patch.stop()
        self.env.stop()
        store.DB_PATH = self.original
        self.tmp.cleanup()

    def headers(self, participant=None):
        return {'X-App-Key': 'synthetic-test-key', 'X-Participant-ID': participant or self.family}

    def approve(self):
        response = self.client.post('/app/events/current/vendors/K01/approve', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['confirmation_id']

    def test_approval_queues_confirmation_without_claiming_a_booking(self):
        confirmation_id = self.approve()
        self.calls.assert_called_once_with(confirmation_id)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_confirmation')
        self.assertEqual(store.checkins(self.event_id), [])

    def test_wrong_family_or_vendor_cannot_approve_or_retry(self):
        for participant in (self.other_family, 'K01'):
            self.assertEqual(self.client.post('/app/events/current/vendors/K01/approve', headers=self.headers(participant)).status_code, 403)
            self.assertEqual(self.client.post('/app/events/current/vendors/K01/confirm', headers=self.headers(participant)).status_code, 403)
        self.calls.assert_not_called()

    def test_confirmation_retry_requires_the_approved_vendor(self):
        self.approve()
        self.assertEqual(self.client.post('/app/events/current/vendors/K03/confirm', headers=self.headers()).status_code, 400)

    def test_retry_creates_a_fresh_attempt_after_no_answer(self):
        first = self.approve()
        confirmations.claim(first)
        confirmations.finish(first, 'no_answer', 'No answer')
        response = self.client.post('/app/events/current/vendors/K01/confirm', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()['confirmation_id'], first)
        self.calls.assert_called_with(response.json()['confirmation_id'])

    def test_price_review_queues_a_new_confirmation_not_an_active_booking(self):
        first = self.approve()
        finish_confirmation(first)
        update_id = workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'price', 'price_amount': 2800,
            'price_unit': 'total', 'notes': 'New cake price'}, 'Base price 2800 in total')
        response = self.client.post(f'/app/updates/{update_id}/approve', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_confirmation')
        self.assertEqual(store.checkins(self.event_id), [])
        self.calls.assert_called_with(store.latest_confirmation(self.event_id, 'cake')['id'])

    def test_both_parties_receive_the_same_confirmed_summary(self):
        confirmation_id = self.approve()
        finish_confirmation(confirmation_id)
        family = self.client.get('/app/events/current', headers=self.headers()).json()
        service = next(s for s in family['services'] if s['category'] == 'cake')
        vendor = self.client.get('/app/vendor/K01/jobs', headers=self.headers('K01')).json()
        self.assertEqual(service['booking']['primary']['confirmation']['terms'], vendor['confirmation']['terms'])
        self.assertEqual(vendor['confirmation']['status'], 'confirmed')

    def test_unconfirmed_selection_can_be_cancelled_but_confirmed_booking_cannot(self):
        self.approve()
        response = self.client.post('/app/events/current/bookings/cake/cancel', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'cancelled')
        second = self.client.post('/app/events/current/vendors/K03/approve', headers=self.headers()).json()['confirmation_id']
        finish_confirmation(second)
        self.assertEqual(self.client.post('/app/events/current/bookings/cake/cancel', headers=self.headers()).status_code, 400)

    def test_backup_confirmation_is_family_scoped_and_requires_the_selected_vendor(self):
        first = self.approve()
        finish_confirmation(first)
        self.assertEqual(self.client.post('/app/events/current/vendors/K03/confirm-backup', headers=self.headers(self.other_family)).status_code, 403)
        self.assertEqual(self.client.post('/app/events/current/vendors/K01/confirm-backup', headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.post('/app/events/current/vendors/K03/confirm-backup', headers=self.headers()).status_code, 200)

    def test_backup_summary_is_standby_not_an_active_booking(self):
        first = self.approve()
        finish_confirmation(first)
        backup_id = store.latest_confirmation(self.event_id, 'cake', 'backup')['id']
        finish_confirmation(backup_id)
        family = self.client.get('/app/events/current', headers=self.headers()).json()
        service = next(s for s in family['services'] if s['category'] == 'cake')
        vendor = self.client.get('/app/vendor/K03/jobs', headers=self.headers('K03')).json()
        self.assertTrue(service['booking']['backup']['secured'])
        self.assertEqual(vendor['confirmation']['purpose'], 'standby')
        self.assertEqual(vendor['booking']['role'], 'backup')
        self.assertEqual({c['vendor_id'] for c in store.checkins(self.event_id)}, {'K01'})

    def test_manual_checkin_uses_one_durable_attempt_and_rejects_duplicate_triggers(self):
        first = self.approve()
        finish_confirmation(first)
        checkin_id = store.checkins(self.event_id)[0]['id']
        with patch('checkin_dispatch.run_attempt', new_callable=AsyncMock) as run:
            response = self.client.post(f'/app/checkins/{checkin_id}/call', headers=self.headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.post(f'/app/checkins/{checkin_id}/call', headers=self.headers()).status_code, 409)
            self.assertEqual(len(store.checkin_attempts(checkin_id)), 1)
            run.assert_called_once_with(response.json()['attempt_id'])

    def test_wrong_family_cannot_trigger_a_checkin(self):
        first = self.approve()
        finish_confirmation(first)
        checkin_id = store.checkins(self.event_id)[0]['id']
        self.assertEqual(self.client.post(f'/app/checkins/{checkin_id}/call', headers=self.headers(self.other_family)).status_code, 403)

    def test_expired_manual_checkin_is_not_forced_into_a_late_call(self):
        first = self.approve()
        finish_confirmation(first)
        checkin = store.checkins(self.event_id)[0]
        from datetime import timedelta
        late = checkin_policy.venue_time(checkin['expires_at']) + timedelta(seconds=1)
        with patch('checkin_policy.now', return_value=late), patch('checkin_dispatch.run_attempt', new_callable=AsyncMock) as run:
            self.assertEqual(self.client.post(f"/app/checkins/{checkin['id']}/call", headers=self.headers()).status_code, 409)
            run.assert_not_called()
