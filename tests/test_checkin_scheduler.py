"""Synthetic clocks and call channels for durable, idempotent, venue-time check-ins."""

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import channels
import checkin_dispatch
import checkin_policy
import confirmations
import prompts
import rules
import store
import workflow
from checkin_scheduler import CheckinScheduler
from confirmation_fixture import book_confirmed
from price_fixture import checked_quote


class PolicyTest(unittest.TestCase):
    def test_naive_venue_times_are_ist_not_server_local_time(self):
        due = checkin_policy.venue_time('2026-11-19T18:00')
        self.assertEqual(due.utcoffset(), timedelta(hours=5, minutes=30))
        self.assertEqual(due.astimezone(timezone.utc).hour, 12)
        self.assertEqual(due.astimezone(timezone.utc).minute, 30)

    def test_aware_times_are_converted_to_the_same_venue_instant(self):
        self.assertEqual(checkin_policy.venue_time('2026-11-19T12:30:00Z'), checkin_policy.venue_time('2026-11-19T18:00'))

    def test_final_call_has_a_short_window_before_required_arrival(self):
        due = dict(rules.checkin_schedule(prompts.SAMPLE_BRIEF, 'caterer'))['Final: ETA check']
        expiry = checkin_policy.expires_at(prompts.SAMPLE_BRIEF, 'caterer', 'Final: ETA check', due)
        self.assertEqual(expiry.strftime('%H:%M'), '05:55')

    def test_skipped_calls_do_not_create_vendor_no_answer_strikes(self):
        self.assertIsNone(rules.assess_risk([{'status': 'skipped', 'label': 'D-3 confirmation'}, {'status': 'skipped', 'label': 'D-1 confirmation'}], prompts.SAMPLE_BRIEF, 'priest'))

    def test_confirmed_but_late_eta_still_activates_backup_rules(self):
        risk = rules.assess_risk([{'status': 'confirmed', 'eta': '07:15', 'label': 'Final: ETA check'}], prompts.SAMPLE_BRIEF, 'caterer', only_on_cancel=False)
        self.assertEqual(risk['action'], 'activate_backup')


class CheckinTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'scheduler.db'
        self.event_id = store.save_event(prompts.SAMPLE_NAAMKARAN)
        quote = checked_quote({'available': True, 'price_amount': 2400, 'price_unit': 'total', 'transport_charge': 0,
            'extra_charges_amount': 0, 'advance_amount': 500}, 'cake')
        store.save_quote(self.event_id, 'K01', None, quote)
        store.save_quote(self.event_id, 'K03', None, checked_quote({**quote, 'price_amount': 2200}, 'cake'))
        self.confirmation_id = book_confirmed('cake', 'K01')['confirmation_id']
        self.checkins = store.checkins(self.event_id)
        self.first = self.checkins[0]
        self.at = checkin_policy.venue_time(self.first['due_at']) + timedelta(seconds=1)
        self.lock_patch = patch('channels.CALL_LOCK', asyncio.Lock())
        self.lock_patch.start()
        self.queue_patch = patch('enquiries.queue_pending_confirmations')
        self.queue_patch.start()

    def tearDown(self):
        self.queue_patch.stop()
        self.lock_patch.stop()
        store.DB_PATH = self.original
        self.tmp.cleanup()

    async def confirmed_call(self, vendor, session, caller, **kwargs):
        self.assertTrue(kwargs['before_dial']())
        call_id = f"synthetic-checkin-{session['checkin_attempt_id']}"
        checkin_dispatch.bind(session['checkin_attempt_id'], session['checkin_id'], call_id, vendor['id'], session['event_id'])
        workflow.record_checkin(session['checkin_id'], 'confirmed', None, 'Still coming', attempt_id=session['checkin_attempt_id'], call_id=call_id)
        return {'result': 'answered', 'call_id': call_id}

    async def test_due_call_runs_once_and_records_a_confirmed_result(self):
        scheduler = CheckinScheduler(clock=lambda: self.at)
        with patch('channels.call', new_callable=AsyncMock, side_effect=self.confirmed_call) as call:
            self.assertIsNotNone(await scheduler.tick())
            self.assertIsNone(await scheduler.tick())
            call.assert_awaited_once()
        self.assertEqual(store.checkin(self.first['id'])['status'], 'confirmed')
        self.assertEqual(store.checkin(self.first['id'])['dispatch_state'], 'completed')

    async def test_future_checkins_are_not_dialled(self):
        scheduler = CheckinScheduler(clock=lambda: self.at - timedelta(days=1))
        with patch('channels.call', new_callable=AsyncMock) as call:
            self.assertIsNone(await scheduler.tick())
            call.assert_not_called()

    async def test_expired_windows_are_skipped_without_a_catchup_burst(self):
        at = checkin_policy.venue_time(self.checkins[1]['due_at']) + timedelta(hours=3)
        with patch('channels.call', new_callable=AsyncMock) as call:
            await CheckinScheduler(clock=lambda: at).tick()
            call.assert_not_called()
        self.assertEqual(store.checkin(self.first['id'])['status'], 'skipped')
        self.assertEqual(store.checkin(self.checkins[1]['id'])['status'], 'skipped')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    async def test_a_busy_gnani_lock_does_not_create_a_queue_of_expiring_dials(self):
        await channels.CALL_LOCK.acquire()
        try:
            self.assertIsNone(await CheckinScheduler(clock=lambda: self.at).tick())
            self.assertEqual(store.checkin_attempts(self.first['id']), [])
        finally:
            channels.CALL_LOCK.release()

    async def test_manual_and_automatic_dispatch_share_the_same_claim(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], manual=True, at=self.at)
        with self.assertRaises(ValueError):
            checkin_dispatch.enqueue(self.first['id'], at=self.at)
        with patch('channels.call', new_callable=AsyncMock, side_effect=self.confirmed_call) as call:
            await checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at)
            await checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at)
            call.assert_awaited_once()

    async def test_two_workers_cannot_dial_the_same_attempt(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        started, release = asyncio.Event(), asyncio.Event()
        async def fake_call(*args, **kwargs):
            started.set()
            await release.wait()
            return {'result': 'no_answer', 'call_id': 'synthetic-call'}
        with patch('channels.call', new_callable=AsyncMock, side_effect=fake_call) as call:
            first = asyncio.create_task(checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at))
            await started.wait()
            await checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at)
            release.set()
            await first
            call.assert_awaited_once()

    async def test_no_answer_is_one_result_not_multiple_retry_strikes(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        with patch('channels.call', new_callable=AsyncMock, return_value={'result': 'no_answer', 'call_id': 'synthetic-call'}):
            await checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at)
        self.assertEqual(store.checkin(self.first['id'])['status'], 'unreachable')
        with self.assertRaises(ValueError):
            checkin_dispatch.enqueue(self.first['id'], manual=True, at=self.at)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    async def test_system_failure_retries_without_recording_vendor_unreachable(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        with patch('channels.call', new_callable=AsyncMock, return_value={'result': 'failed', 'call_id': None}):
            await checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at)
        row = store.checkin(self.first['id'])
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['dispatch_state'], 'scheduled')
        self.assertIsNotNone(row['next_attempt_at'])
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    async def test_restart_recovers_running_attempt_without_a_vendor_strike(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        store.claim_checkin_attempt(attempt_id, self.at.isoformat())
        checkin_dispatch.recover(self.at + timedelta(minutes=1))
        self.assertEqual(store.checkin_attempt(attempt_id)['status'], 'interrupted')
        self.assertEqual(store.checkin(self.first['id'])['status'], 'pending')
        self.assertEqual(store.checkin(self.first['id'])['dispatch_state'], 'scheduled')

    async def test_queued_attempt_survives_restart_and_runs_once(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        checkin_dispatch.recover(self.at)
        with patch('channels.call', new_callable=AsyncMock, side_effect=self.confirmed_call) as call:
            self.assertEqual(await CheckinScheduler(clock=lambda: self.at).tick(), attempt_id)
            call.assert_awaited_once()

    async def test_retry_limit_skips_system_failure_without_activating_backup(self):
        for _ in range(checkin_policy.MAX_ATTEMPTS):
            attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
            with patch('channels.call', new_callable=AsyncMock, return_value={'result': 'failed', 'call_id': None}):
                await checkin_dispatch.run_attempt(attempt_id, clock=lambda: self.at)
            retry = store.checkin(self.first['id'])['next_attempt_at']
            if retry:
                self.at = checkin_policy.venue_time(retry)
        self.assertEqual(store.checkin(self.first['id'])['status'], 'skipped')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    async def test_old_booking_attempt_cannot_update_new_confirmation(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        store.claim_checkin_attempt(attempt_id, self.at.isoformat())
        old = store.checkin(self.first['id'])
        with store.connect() as conn:
            conn.execute('UPDATE checkins SET confirmation_id = ? WHERE id = ?', (self.confirmation_id + 999, old['id']))
        with self.assertRaises(ValueError):
            workflow.record_checkin(old['id'], 'confirmed', None, 'Late result', attempt_id=attempt_id)

    async def test_duplicate_result_is_rejected(self):
        workflow.record_checkin(self.first['id'], 'confirmed', None, 'Still coming')
        with self.assertRaises(ValueError):
            workflow.record_checkin(self.first['id'], 'unreachable', None, 'Duplicate')

    async def test_session_plan_binds_the_checkin_to_its_attempt_and_booking(self):
        from conversation import SessionConfig, _plan
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        store.claim_checkin_attempt(attempt_id, self.at.isoformat())
        cfg = SessionConfig(mode='checkin', channel='app', vendor_id='K01', event_id=self.event_id,
            checkin_id=self.first['id'], checkin_attempt_id=attempt_id, call_id='synthetic-voice')
        _, tools, _, event_id = _plan(cfg)
        self.assertEqual(event_id, self.event_id)
        self.assertEqual(store.checkin_attempt(attempt_id)['call_id'], 'synthetic-voice')
        self.assertEqual([t.name for t in tools], ['record_checkin', 'end_call'])

    async def test_wrong_vendor_or_call_cannot_bind_attempt(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        store.claim_checkin_attempt(attempt_id, self.at.isoformat())
        with self.assertRaises(ValueError):
            checkin_dispatch.bind(attempt_id, self.first['id'], 'wrong-call', 'K03', self.event_id)
        checkin_dispatch.bind(attempt_id, self.first['id'], 'correct-call', 'K01', self.event_id)
        with self.assertRaises(ValueError):
            checkin_dispatch.bind(attempt_id, self.first['id'], 'other-call', 'K01', self.event_id)

    async def test_before_dial_guard_prevents_expired_call_after_lock_wait(self):
        with patch('channels.channel_for', return_value='app'), patch('channels._call_app', new_callable=AsyncMock) as dial:
            outcome = await channels.call(store.vendor('K01'), {}, {}, before_dial=lambda: False)
            self.assertEqual(outcome['result'], 'stale')
            dial.assert_not_awaited()

    async def test_vendor_callback_time_is_honored_within_useful_window(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        store.claim_checkin_attempt(attempt_id, self.at.isoformat())
        callback = self.at + timedelta(minutes=20)
        checkin_dispatch.fail(attempt_id, 'Vendor requested 20 minutes later', status='callback', at=self.at, callback_at=callback)
        self.assertEqual(checkin_policy.venue_time(store.checkin(self.first['id'])['next_attempt_at']), callback)
        self.assertEqual(store.checkin(self.first['id'])['status'], 'pending')

    async def test_out_of_window_callback_does_not_trigger_an_earlier_unrequested_call(self):
        attempt_id = checkin_dispatch.enqueue(self.first['id'], at=self.at)
        store.claim_checkin_attempt(attempt_id, self.at.isoformat())
        checkin_dispatch.fail(attempt_id, 'Call tomorrow', status='callback', at=self.at, callback_at=self.at + timedelta(days=1))
        self.assertEqual(store.checkin(self.first['id'])['status'], 'skipped')

    async def test_replaced_vendor_checkin_is_not_dialled(self):
        store.set_booking(self.event_id, 'cake', 'primary', 'K03', 'active')
        with patch('channels.call', new_callable=AsyncMock) as call:
            await CheckinScheduler(clock=lambda: self.at).tick()
            call.assert_not_awaited()
        self.assertEqual(store.checkin(self.first['id'])['status'], 'skipped')

    async def test_an_old_function_is_not_automatically_called(self):
        store.save_event(prompts.SAMPLE_BRIEF)
        with self.assertRaises(ValueError):
            checkin_dispatch.enqueue(self.first['id'], at=self.at)

    async def test_scheduler_shutdown_releases_running_attempt_without_vendor_fault(self):
        started = asyncio.Event()
        async def fake_call(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        scheduler = CheckinScheduler(clock=lambda: self.at, poll_seconds=0.1)
        with patch('channels.call', new_callable=AsyncMock, side_effect=fake_call):
            scheduler.start()
            await asyncio.wait_for(started.wait(), 2)
            await scheduler.stop()
        self.assertEqual(store.checkin(self.first['id'])['status'], 'pending')
        self.assertEqual(store.checkin(self.first['id'])['dispatch_state'], 'scheduled')
        self.assertEqual(store.checkin_attempts(self.first['id'])[0]['status'], 'interrupted')


class CheckinMigrationTest(unittest.TestCase):
    def test_old_rows_gain_dispatch_metadata_without_losing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            original = store.DB_PATH
            store.DB_PATH = Path(directory) / 'legacy.db'
            try:
                with sqlite3.connect(store.DB_PATH) as conn:
                    conn.execute('CREATE TABLE checkins (id INTEGER PRIMARY KEY, event_id INTEGER, vendor_id TEXT, category TEXT, label TEXT, due_at TEXT, status TEXT, eta TEXT, notes TEXT, done_at TEXT)')
                    conn.execute("INSERT INTO checkins VALUES (1,1,'K01','cake','D-3 confirmation','2026-11-19T18:00','confirmed',NULL,'Already done','2026-11-19T18:01')")
                row = store.checkin(1)
                self.assertEqual(row['status'], 'confirmed')
                self.assertEqual(row['dispatch_state'], 'completed')
                self.assertEqual(row['notes'], 'Already done')
            finally:
                store.DB_PATH = original
