"""Synthetic completed read-backs for tests that need an already-confirmed booking."""

import confirmation_rules
import confirmations
import store
import workflow


def finish_confirmation(confirmation_id):
    record = confirmations.claim(confirmation_id)
    if record is None:
        record = store.confirmation(confirmation_id)
    call_id = f"synthetic-confirmation-{confirmation_id}"
    confirmations.bind(confirmation_id, call_id, vendor_id=record['vendor_id'], event_id=record['event_id'])
    questions = confirmation_rules.questions(record['terms'], store.vendor(record['vendor_id'])['language'])
    for index, question in enumerate(questions):
        confirmations.accept_answer(confirmation_id, call_id, index, 'yes', user_turn=index + 1,
            ready_turn=index, question=question['text'], tts_context=f"synthetic-tts-{index}")


def book_confirmed(category, vendor_id=None):
    result = workflow.book(category, vendor_id)
    finish_confirmation(result['confirmation_id'])
    return result
