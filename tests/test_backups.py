"""Synthetic standby consent, fresh activation and family authorization for extra cost/advance."""

from unittest.mock import patch
import sqlite3
import tempfile
import unittest
from pathlib import Path

import backups
import confirmation_rules
import confirmations
import prompts
import store
import workflow
from confirmation_fixture import finish_confirmation
from price_fixture import checked_quote


@patch.object(backups, "STANDBY_CALLS", True)  # the standby-call flow, off by default since 2026-11-09
@patch.object(workflow, "BACKUP_ONLY_ON_CANCEL", False)
class BackupWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'backups.db'
        self.brief = prompts.SAMPLE_NAAMKARAN
        self.event_id = store.save_event(self.brief)
        self.primary_quote = checked_quote({'available': True, 'price_amount': 2400, 'price_unit': 'total',
            'transport_charge': 0, 'extra_charges_amount': 0, 'advance_amount': 500}, 'cake')
        self.backup_quote = checked_quote({**self.primary_quote, 'price_amount': 2200, 'advance_amount': 400}, 'cake')
        for vendor_id, quote in (('K01', self.primary_quote), ('K03', self.backup_quote)):
            store.save_quote(self.event_id, vendor_id, None, quote)
        self.primary_id = workflow.book('cake', 'K01')['confirmation_id']

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    def confirm_primary(self):
        finish_confirmation(self.primary_id)
        return store.latest_confirmation(self.event_id, 'cake', 'backup')['id']

    def secure_backup(self):
        backup_id = self.confirm_primary()
        finish_confirmation(backup_id)
        return backup_id

    def fail_primary(self):
        workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'cancelled', 'notes': 'Cannot deliver'}, 'Cannot deliver')

    def test_selection_is_not_secured_standby(self):
        self.assertFalse(backups.secured(self.event_id, 'cake'))
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'awaiting_backup_confirmation')
        with self.assertRaises(ValueError):
            backups.request_standby(self.event_id, 'cake')

    def test_primary_confirmation_creates_a_separate_pending_standby_attempt(self):
        backup_id = self.confirm_primary()
        self.assertNotEqual(backup_id, self.primary_id)
        self.assertEqual(store.latest_confirmation(self.event_id, 'cake')['id'], self.primary_id)
        backup = store.confirmation(backup_id)
        self.assertEqual(backup['role'], 'backup')
        self.assertEqual(backup['purpose'], 'standby')
        self.assertEqual(backup['parent_confirmation_id'], self.primary_id)

    def test_standby_confirmation_does_not_book_or_schedule_the_backup(self):
        backup_id = self.secure_backup()
        self.assertTrue(backups.secured(self.event_id, 'cake'))
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'standby')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K01')
        self.assertEqual({c['vendor_id'] for c in store.checkins(self.event_id)}, {'K01'})
        self.assertEqual(len(store.checkins(self.event_id)), 4)
        question = confirmation_rules.questions(store.confirmation(backup_id)['terms'], 'en-IN')[-1]['text']
        self.assertIn('not booked', question)
        self.assertIn('no advance', question)

    def test_standby_no_answer_preserves_primary_booking_and_checkins(self):
        backup_id = self.confirm_primary()
        confirmations.claim(backup_id)
        confirmations.finish(backup_id, 'no_answer', 'No answer')
        self.assertFalse(backups.secured(self.event_id, 'cake'))
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')
        self.assertEqual(len(store.checkins(self.event_id)), 4)
        self.assertNotEqual(backups.retry(self.event_id, 'cake'), backup_id)

    def test_standby_refusal_makes_backup_unavailable_without_cancelling_primary(self):
        backup_id = self.confirm_primary()
        confirmations.claim(backup_id)
        confirmations.bind(backup_id, 'synthetic-backup-call', vendor_id='K03', event_id=self.event_id)
        confirmations.finish(backup_id, 'declined', 'Cannot stay available', call_id='synthetic-backup-call')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')
        self.assertFalse(store.latest_quotes(self.event_id)['K03']['available'])
        self.assertFalse(backups.secured(self.event_id, 'cake'))

    def test_primary_failure_does_not_activate_backup_before_fresh_confirmation(self):
        standby_id = self.secure_backup()
        self.fail_primary()
        activation = store.latest_confirmation(self.event_id, 'cake', 'backup')
        self.assertNotEqual(activation['id'], standby_id)
        self.assertEqual(activation['purpose'], 'replacement')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'replacement_pending')
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'awaiting_activation')
        self.assertEqual(store.checkins(self.event_id), [])

    def test_fresh_activation_atomically_promotes_the_backup(self):
        self.secure_backup()
        self.fail_primary()
        activation_id = store.latest_confirmation(self.event_id, 'cake', 'backup')['id']
        finish_confirmation(activation_id)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K03')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')
        self.assertEqual(confirmations.summary(self.event_id, 'cake')['id'], activation_id)
        self.assertEqual(store.confirmation(activation_id)['role'], 'primary')
        self.assertEqual(backups.selected(self.event_id, 'cake')['vendor_id'], 'K01')
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'replaced')
        self.assertEqual({c['vendor_id'] for c in store.checkins(self.event_id)}, {'K03'})

    def test_activation_storage_failure_rolls_back_promotion_and_confirmation(self):
        self.secure_backup()
        self.fail_primary()
        record = store.latest_confirmation(self.event_id, 'cake', 'backup')
        confirmations.claim(record['id'])
        confirmations.bind(record['id'], 'synthetic-activation', vendor_id='K03', event_id=self.event_id)
        questions = confirmation_rules.questions(record['terms'], store.vendor('K03')['language'])
        for index, question in enumerate(questions):
            store.add_confirmation_answer(record['id'], 'synthetic-activation', index, {'step': question['key'], 'answer': 'yes'})
        with self.assertRaises(sqlite3.IntegrityError):
            store.complete_confirmation(record['id'], 'synthetic-activation', len(questions), [('Invalid due time', None)], 'Replacement confirmed')
        self.assertEqual(store.confirmation(record['id'])['role'], 'backup')
        self.assertEqual(store.confirmation(record['id'])['status'], 'calling')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K01')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'replacement_pending')

    def test_server_restart_keeps_activation_unconfirmed_and_retryable(self):
        self.secure_backup()
        self.fail_primary()
        record = store.latest_confirmation(self.event_id, 'cake', 'backup')
        confirmations.claim(record['id'])
        confirmations.recover_interrupted()
        self.assertEqual(store.confirmation(record['id'])['status'], 'interrupted')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'needs_replacement')
        self.assertIsNotNone(backups.retry(self.event_id, 'cake'))

    def test_higher_replacement_cost_needs_family_approval(self):
        store.save_quote(self.event_id, 'K03', None, checked_quote({**self.backup_quote, 'price_amount': 2600}, 'cake'))
        self.secure_backup()
        self.fail_primary()
        pending = next(u for u in store.updates(self.event_id) if u['kind'] == 'backup_activation')
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'activation_awaiting_approval')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'replacement_pending')
        result = workflow.decide_update(self.event_id, pending['id'], True)
        self.assertIsNotNone(result['confirmation_id'])
        self.assertNotEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')
        finish_confirmation(result['confirmation_id'])
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K03')

    def test_higher_advance_needs_approval_even_when_total_is_lower(self):
        store.save_quote(self.event_id, 'K03', None, checked_quote({**self.backup_quote, 'advance_amount': 600}, 'cake'))
        self.secure_backup()
        self.fail_primary()
        self.assertTrue(any(u['kind'] == 'backup_activation' and u['status'] == 'pending' for u in store.updates(self.event_id)))

    def test_rejecting_activation_leaves_no_fake_active_replacement(self):
        store.save_quote(self.event_id, 'K03', None, checked_quote({**self.backup_quote, 'price_amount': 2600}, 'cake'))
        self.secure_backup()
        self.fail_primary()
        update = next(u for u in store.updates(self.event_id) if u['kind'] == 'backup_activation')
        workflow.decide_update(self.event_id, update['id'], False)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'needs_replacement')
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'cancelled')

    def test_changed_backup_quote_invalidates_old_activation_approval(self):
        store.save_quote(self.event_id, 'K03', None, checked_quote({**self.backup_quote, 'price_amount': 2600}, 'cake'))
        self.secure_backup()
        self.fail_primary()
        update = next(u for u in store.updates(self.event_id) if u['kind'] == 'backup_activation')
        store.save_quote(self.event_id, 'K03', None, checked_quote({**self.backup_quote, 'price_amount': 2700}, 'cake'))
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, update['id'], True)

    def test_price_safeguards_cannot_be_bypassed_by_activation_approval(self):
        store.save_quote(self.event_id, 'K03', None, checked_quote({**self.backup_quote, 'price_amount': 2600}, 'cake'))
        self.secure_backup()
        self.fail_primary()
        update = next(u for u in store.updates(self.event_id) if u['kind'] == 'backup_activation')
        store.save_quote(self.event_id, 'K03', None, {**store.latest_quotes(self.event_id)['K03'], 'unverified': ['advance_amount']})
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, update['id'], True)

    def test_activation_no_answer_is_retryable_without_restoring_failed_primary(self):
        self.secure_backup()
        self.fail_primary()
        first = store.latest_confirmation(self.event_id, 'cake', 'backup')['id']
        confirmations.claim(first)
        confirmations.finish(first, 'no_answer', 'No answer')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'needs_replacement')
        second = backups.retry(self.event_id, 'cake')
        self.assertNotEqual(first, second)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'replacement_pending')

    def test_old_standby_reply_cannot_confirm_replacement_activation(self):
        standby_id = self.confirm_primary()
        confirmations.claim(standby_id)
        confirmations.bind(standby_id, 'old-standby-call', vendor_id='K03', event_id=self.event_id)
        question = confirmation_rules.questions(store.confirmation(standby_id)['terms'], 'kn-IN')[0]['text']
        self.fail_primary()
        with self.assertRaises(ValueError):
            confirmations.accept_answer(standby_id, 'old-standby-call', 0, 'yes', user_turn=1, ready_turn=0,
                question=question, tts_context='old-audio')

    def test_switching_candidate_invalidates_the_old_backup_call(self):
        old_id = self.confirm_primary()
        confirmations.claim(old_id)
        store.save_quote(self.event_id, 'K02', None, self.backup_quote)
        new_id = backups.choose(self.event_id, 'cake', 'K02')
        self.assertNotEqual(new_id, old_id)
        with self.assertRaises(ValueError):
            confirmations.bind(old_id, 'old-call', vendor_id='K03', event_id=self.event_id)

    def test_primary_revision_invalidates_old_standby_consent(self):
        old_id = self.secure_backup()
        update_id = workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'price', 'price_amount': 2500,
            'price_unit': 'total', 'notes': 'New rate'}, 'Base price 2500 in total')
        self.assertFalse(backups.secured(self.event_id, 'cake'))
        result = workflow.decide_update(self.event_id, update_id, True)
        finish_confirmation(result['confirmation_id'])
        self.assertNotEqual(store.latest_confirmation(self.event_id, 'cake', 'backup')['id'], old_id)

    def test_primary_revision_supersedes_an_in_flight_standby_call(self):
        old_id = self.confirm_primary()
        confirmations.claim(old_id)
        workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'price', 'price_amount': 2500,
            'price_unit': 'total', 'notes': 'New rate'}, 'Base price 2500 in total')
        self.assertEqual(store.confirmation(old_id)['status'], 'superseded')
        confirmations.finish(old_id, 'declined', 'Late reply from old standby call')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_changes')

    def test_backup_price_change_does_not_cancel_primary_confirmation(self):
        self.secure_backup()
        workflow.record_vendor_update(self.event_id, 'K03', {'kind': 'price', 'price_amount': 2300,
            'price_unit': 'total', 'notes': 'New rate'}, 'Base price 2300 in total')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')
        self.assertEqual(confirmations.summary(self.event_id, 'cake')['status'], 'confirmed')
        self.assertFalse(backups.secured(self.event_id, 'cake'))
        self.assertEqual(len(store.checkins(self.event_id)), 4)

    def test_missing_backup_never_reports_a_successful_switch(self):
        self.confirm_primary()
        store.set_booking(self.event_id, 'cake', 'backup', 'K03', 'cancelled')
        self.fail_primary()
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'needs_replacement')
        self.assertEqual(store.checkins(self.event_id), [])
        self.assertIn('No confirmed replacement', store.alerts(self.event_id)[0]['message'])

    def test_cancel_replacement_request_invalidates_pending_activation(self):
        self.secure_backup()
        self.fail_primary()
        activation_id = store.latest_confirmation(self.event_id, 'cake', 'backup')['id']
        confirmations.cancel_selection(self.event_id, 'cake')
        with self.assertRaises(ValueError):
            confirmations.claim(activation_id)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'cancelled')

    def test_standby_questions_never_request_a_current_booking_or_payment(self):
        backup_id = self.confirm_primary()
        snapshot = store.confirmation(backup_id)['terms']
        for language in ('en-IN', 'hi-IN', 'kn-IN', 'te-IN', 'ta-IN'):
            questions = confirmation_rules.questions(snapshot, language)
            self.assertEqual(questions[-1]['key'], 'standby')
            self.assertTrue(all(q['text'].count('?') == 1 for q in questions))
        self.assertIn('not booked', confirmation_rules.questions(snapshot, 'en-IN')[-1]['text'])


@patch.object(backups, "STANDBY_CALLS", True)  # the standby-call flow, off by default since 2026-11-09
@patch.object(workflow, "BACKUP_ONLY_ON_CANCEL", False)
class BackupMigrationTest(unittest.TestCase):
    def test_existing_primary_confirmation_is_preserved_by_role_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            original = store.DB_PATH
            store.DB_PATH = Path(directory) / 'old.db'
            try:
                with sqlite3.connect(store.DB_PATH) as conn:
                    conn.execute('''CREATE TABLE booking_confirmations (id INTEGER PRIMARY KEY, event_id INTEGER, category TEXT,
                        vendor_id TEXT, status TEXT, terms TEXT, approved_quote TEXT, answers TEXT, call_id TEXT, reason TEXT,
                        created_at TEXT, updated_at TEXT)''')
                    conn.execute("INSERT INTO booking_confirmations VALUES (1, 1, 'cake', 'K01', 'confirmed', '{}', '{}', '[]', 'old-call', NULL, '2026-11-04', '2026-11-04')")
                record = store.confirmation(1)
                self.assertEqual(record['role'], 'primary')
                self.assertEqual(record['purpose'], 'booking')
                self.assertEqual(record['status'], 'confirmed')
                self.assertEqual(record['call_id'], 'old-call')
                self.assertIsNone(store.latest_confirmation(1, 'cake', 'backup'))
            finally:
                store.DB_PATH = original


class BackupOnFileTest(unittest.TestCase):
    """Default since 2026-11-09: no standby call after booking; the backup is called only if the primary cancels."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'backups.db'
        self.event_id = store.save_event(prompts.SAMPLE_NAAMKARAN)
        primary = checked_quote({'available': True, 'price_amount': 2400, 'price_unit': 'total',
            'transport_charge': 0, 'extra_charges_amount': 0, 'advance_amount': 500}, 'cake')
        for vendor_id, quote in (('K01', primary), ('K03', checked_quote({**primary, 'price_amount': 2200, 'advance_amount': 400}, 'cake'))):
            store.save_quote(self.event_id, vendor_id, None, quote)
        finish_confirmation(workflow.book('cake', 'K01')['confirmation_id'])

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    def test_no_standby_call_after_booking(self):
        self.assertIsNone(store.latest_confirmation(self.event_id, 'cake', 'backup'))  # nobody is called
        self.assertEqual(backups.selected(self.event_id, 'cake')['status'], 'backup_on_file')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    def test_a_cancellation_calls_the_backup_to_book_them(self):
        workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'cancelled', 'notes': 'Cannot deliver'}, 'Cannot deliver')
        activation = store.latest_confirmation(self.event_id, 'cake', 'backup')
        self.assertEqual((activation['vendor_id'], activation['purpose']), ('K03', 'replacement'))
        finish_confirmation(activation['id'])
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K03')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    def test_a_late_arrival_only_warns_the_family(self):
        workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'delayed', 'eta': '09:55', 'notes': 'Traffic'}, 'Traffic')
        self.assertIsNone(store.latest_confirmation(self.event_id, 'cake', 'backup'))
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K01')
