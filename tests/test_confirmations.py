"""Synthetic booking confirmation, immutable approvals, retries and spoken-agreement guards."""

import tempfile
import unittest
from pathlib import Path

import confirmation_rules
import confirmations
import prompts
import store
import workflow
from confirmation_fixture import finish_confirmation
from price_fixture import checked_quote


class BookingConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / 'confirmation.db'
        self.brief = prompts.SAMPLE_NAAMKARAN
        self.event_id = store.save_event(self.brief)
        self.quote = checked_quote({'available': True, 'price_amount': 2400, 'price_unit': 'total',
            'transport_charge': 100, 'extra_charges_amount': 0, 'advance_amount': 500, 'includes_notes': 'Eggless chocolate cake'}, 'cake')
        for vendor_id in ('K01', 'K03'):
            store.save_quote(self.event_id, vendor_id, None, self.quote)
        self.result = workflow.book('cake', 'K01')
        self.confirmation_id = self.result['confirmation_id']

    def tearDown(self):
        store.DB_PATH = self.original
        self.tmp.cleanup()

    def begin(self):
        record = confirmations.claim(self.confirmation_id)
        confirmations.bind(self.confirmation_id, 'synthetic-call', vendor_id='K01', event_id=self.event_id)
        return confirmation_rules.questions(record['terms'], store.vendor('K01')['language'])

    def answer(self, questions, index, text='yes', **overrides):
        args = {'user_turn': index + 1, 'ready_turn': index, 'question': questions[index]['text'], 'tts_context': f'synthetic-tts-{index}'}
        args.update(overrides)
        return confirmations.accept_answer(self.confirmation_id, 'synthetic-call', index, text, **args)

    def test_family_approval_does_not_activate_booking_or_create_checkins(self):
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_confirmation')
        self.assertEqual(store.confirmation(self.confirmation_id)['status'], 'pending')
        self.assertEqual(store.checkins(self.event_id), [])
        self.assertEqual(self.result['backup'], 'K03')

    def test_a_second_family_selection_is_blocked_while_confirmation_is_pending(self):
        with self.assertRaises(ValueError):
            workflow.book('cake', 'K03')

    def test_claim_is_idempotent_and_prevents_duplicate_confirmation_calls(self):
        self.assertIsNotNone(confirmations.claim(self.confirmation_id))
        self.assertIsNone(confirmations.claim(self.confirmation_id))

    def test_wrong_vendor_event_or_call_cannot_bind(self):
        self.begin()
        for vendor_id, event_id, call_id in (('K03', self.event_id, 'synthetic-call'), ('K01', self.event_id + 1, 'synthetic-call'), ('K01', self.event_id, 'other-call')):
            with self.assertRaises(ValueError):
                confirmations.bind(self.confirmation_id, call_id, vendor_id=vendor_id, event_id=event_id)

    def test_every_approved_question_must_be_answered_before_activation(self):
        questions = self.begin()
        for index in range(len(questions) - 1):
            self.assertFalse(self.answer(questions, index))
            self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'confirming')
            self.assertEqual(store.checkins(self.event_id), [])
        self.assertTrue(self.answer(questions, len(questions) - 1))
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')
        self.assertEqual(store.confirmation(self.confirmation_id)['status'], 'confirmed')
        self.assertEqual(len(store.checkins(self.event_id)), 4)

    def test_yes_before_audio_delivery_does_not_confirm(self):
        questions = self.begin()
        for kwargs in ({'tts_context': None}, {'user_turn': 1, 'ready_turn': 1}):
            with self.assertRaises(ValueError):
                self.answer(questions, 0, **kwargs)
        self.assertEqual(store.confirmation(self.confirmation_id)['answers'], [])

    def test_unrelated_conditional_or_changed_answers_do_not_count_as_yes(self):
        questions = self.begin()
        for text in ('yes but the rate changed', '5000', 'I am available tomorrow', 'not sure', 'no', ''):
            with self.assertRaises(ValueError):
                self.answer(questions, 0, text)
        self.assertEqual(store.confirmation(self.confirmation_id)['answers'], [])

    def test_wrong_question_cannot_be_substituted_by_the_model(self):
        questions = self.begin()
        with self.assertRaises(ValueError):
            self.answer(questions, 0, question='The price is now 1 rupee, correct?')

    def test_one_yes_cannot_be_replayed_for_another_step(self):
        questions = self.begin()
        self.answer(questions, 0)
        with self.assertRaises(ValueError):
            self.answer(questions, 0)
        with self.assertRaises(ValueError):
            self.answer(questions, 1, user_turn=1, ready_turn=0)

    def test_no_answer_and_ring_decline_do_not_confirm(self):
        confirmations.claim(self.confirmation_id)
        confirmations.finish(self.confirmation_id, 'no_answer', 'No answer')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'confirmation_failed')
        self.assertEqual(store.latest_quotes(self.event_id)['K01']['available'], True)
        self.assertEqual(store.checkins(self.event_id), [])

    def test_a_spoken_refusal_releases_the_selection_without_activating_backup(self):
        self.begin()
        confirmations.finish(self.confirmation_id, 'declined', 'Cannot accept this booking', call_id='synthetic-call')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'declined')
        backup = next(b for b in store.bookings(self.event_id) if b['role'] == 'backup')
        self.assertEqual(backup['status'], 'standby')
        self.assertFalse(store.latest_quotes(self.event_id)['K01']['available'])
        self.assertEqual(workflow.book('cake', 'K03')['primary'], 'K03')
        self.assertFalse(any(b['role'] == 'backup' and b['status'] == 'standby' and b['vendor_id'] == 'K03' for b in store.bookings(self.event_id)))

    def test_callback_remains_unconfirmed_and_can_be_retried(self):
        self.begin()
        confirmations.finish(self.confirmation_id, 'callback', 'Call after 6 pm', call_id='synthetic-call')
        retry_id = confirmations.request(self.event_id, 'cake')
        self.assertNotEqual(retry_id, self.confirmation_id)
        self.assertEqual(store.confirmation(retry_id)['status'], 'pending')

    def test_restart_does_not_silently_confirm_or_lose_the_approval(self):
        self.begin()
        confirmations.recover_interrupted()
        self.assertEqual(store.confirmation(self.confirmation_id)['status'], 'interrupted')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['vendor_id'], 'K01')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'confirmation_failed')
        self.assertEqual(store.checkins(self.event_id), [])

    def test_late_old_call_cannot_confirm_a_retry(self):
        questions = self.begin()
        confirmations.finish(self.confirmation_id, 'failed', 'Disconnected', call_id='synthetic-call')
        retry_id = confirmations.request(self.event_id, 'cake')
        with self.assertRaises(ValueError):
            self.answer(questions, 0)
        confirmations.finish(self.confirmation_id, 'declined', 'Late callback', call_id='synthetic-call')
        self.assertEqual(store.confirmation(retry_id)['status'], 'pending')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_confirmation')

    def test_a_quote_change_invalidates_the_in_flight_confirmation(self):
        questions = self.begin()
        workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'price', 'price_amount': 2800, 'price_unit': 'total', 'notes': 'New rate'}, 'Base price 2800 in total')
        with self.assertRaises(ValueError):
            self.answer(questions, 0)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_changes')

    def test_revised_quote_waits_for_family_approval_and_an_entire_new_confirmation(self):
        self.begin()
        confirmations.change(self.confirmation_id, 'synthetic-call', 'Transport costs 200 now', event_change=False)
        corrected = checked_quote({**self.quote, 'transport_charge': 200}, 'cake')
        update = workflow.record_quote(self.event_id, 'K01', 'synthetic-call', corrected)
        self.assertEqual(store.latest_quotes(self.event_id)['K01']['transport_charge'], 100)
        result = workflow.decide_update(self.event_id, update['update_id'], True)
        self.assertNotEqual(result['confirmation_id'], self.confirmation_id)
        self.assertEqual(store.latest_quotes(self.event_id)['K01']['transport_charge'], 200)
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_confirmation')
        self.assertEqual(store.checkins(self.event_id), [])
        finish_confirmation(result['confirmation_id'])
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    def test_date_change_is_not_applied_to_the_family_function(self):
        self.begin()
        update_id = confirmations.change(self.confirmation_id, 'synthetic-call', 'Please move to another date', event_change=True)
        with self.assertRaises(ValueError):
            workflow.decide_update(self.event_id, update_id, True)
        self.assertEqual(store.event(self.event_id)[1]['date'], self.brief['date'])
        retry = workflow.decide_update(self.event_id, update_id, False)
        self.assertIsNotNone(retry['confirmation_id'])

    def test_cancel_unconfirmed_selection_supersedes_the_call(self):
        questions = self.begin()
        confirmations.cancel_selection(self.event_id, 'cake')
        with self.assertRaises(ValueError):
            self.answer(questions, 0)
        self.assertEqual(workflow.book('cake', 'K03')['primary'], 'K03')

    def test_confirmed_booking_cannot_be_cancelled_through_selection_endpoint(self):
        finish_confirmation(self.confirmation_id)
        with self.assertRaises(ValueError):
            confirmations.cancel_selection(self.event_id, 'cake')

    def test_later_vendor_changes_revoke_confirmation_without_changing_agreed_price(self):
        finish_confirmation(self.confirmation_id)
        workflow.record_vendor_update(self.event_id, 'K01', {'kind': 'price', 'price_amount': 2800, 'price_unit': 'total', 'notes': 'New rate'}, 'Base price 2800 in total')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'awaiting_changes')
        self.assertEqual(store.latest_quotes(self.event_id)['K01']['price_amount'], 2400)
        self.assertEqual(store.checkins(self.event_id), [])
        self.assertEqual(store.confirmation(self.confirmation_id)['status'], 'confirmed')  # truthful history retained

    def test_confirmation_snapshot_stays_immutable_when_live_quote_changes(self):
        before = store.confirmation(self.confirmation_id)['terms']
        store.save_quote(self.event_id, 'K01', None, checked_quote({**self.quote, 'price_amount': 3000}, 'cake'))
        self.assertEqual(store.confirmation(self.confirmation_id)['terms'], before)
        with self.assertRaises(ValueError):
            confirmations.claim(self.confirmation_id)

    def test_declining_an_old_attempt_cannot_undo_an_already_confirmed_booking(self):
        finish_confirmation(self.confirmation_id)
        confirmations.finish(self.confirmation_id, 'declined', 'Late failed response')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'active')

    def test_missing_price_proof_blocks_confirmation_too(self):
        store.save_quote(self.event_id, 'K01', None, {**self.quote, 'unverified': ['advance_amount']})
        with self.assertRaises(ValueError):
            confirmations.claim(self.confirmation_id)

    def test_final_confirmation_transaction_rolls_back_on_checkin_storage_failure(self):
        questions = self.begin()
        for index, question in enumerate(questions):
            store.add_confirmation_answer(self.confirmation_id, 'synthetic-call', index, {'step': question['key'], 'answer': 'yes', 'user_turn': index + 1})
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            store.complete_confirmation(self.confirmation_id, 'synthetic-call', len(questions), [('Invalid due time', None)], 'Confirmed')
        self.assertEqual(store.confirmation(self.confirmation_id)['status'], 'calling')
        self.assertEqual(confirmations.primary(self.event_id, 'cake')['status'], 'confirming')
        self.assertEqual(store.checkins(self.event_id), [])

    def test_native_readbacks_cover_approved_units_charges_and_service(self):
        snapshot = store.confirmation(self.confirmation_id)['terms']
        for language in ('en-IN', 'hi-IN', 'kn-IN', 'te-IN', 'ta-IN'):
            questions = confirmation_rules.questions(snapshot, language)
            self.assertTrue(all(q['text'].count('?') == 1 for q in questions))
            text = ' '.join(q['text'] for q in questions)
            self.assertIn('2400', text)
            self.assertIn('500', text)
            self.assertIn('Eggless chocolate cake', text)
            self.assertEqual(questions[-1]['key'], 'booking')
