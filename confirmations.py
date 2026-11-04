"""Booking-confirmation state transitions. Approval reserves a vendor; spoken agreement activates it.

Each attempt has an immutable snapshot and ID. Old calls, duplicate answers and changed quotes
cannot confirm a newer selection. Background calling lives in enquiries.py, not in these actions.
"""

from typing import Any

import confirmation_rules
import rules
import store


def primary(event_id: int, category: str) -> dict[str, Any] | None:
    return next((b for b in store.bookings(event_id) if b['category'] == category and b['role'] == 'primary'), None)


def current(confirmation_id: int) -> dict[str, Any]:
    record = store.confirmation(confirmation_id)
    latest = store.latest_confirmation(record['event_id'], record['category'])
    booking = primary(record['event_id'], record['category'])
    if not latest or latest['id'] != confirmation_id or not booking or booking['vendor_id'] != record['vendor_id'] or booking['status'] not in confirmation_rules.RESERVED:
        raise ValueError('This booking confirmation is no longer current')
    _, brief = store.event(record['event_id'])
    if any(u['vendor_id'] == record['vendor_id'] and u['status'] == 'pending' for u in store.updates(record['event_id'])):
        raise ValueError('Vendor changes are awaiting family review')
    quote = store.latest_quotes(record['event_id']).get(record['vendor_id'])
    if not quote or confirmation_rules.terms(brief, quote, record['category']) != record['terms']:
        raise ValueError('The approved terms changed; a new family approval and confirmation are required')
    if any(f['level'] == 'block' for f in rules.quote_flags(quote, store.vendor(record['vendor_id']), brief)):
        raise ValueError('The quote does not pass price safeguards')
    return record


def request(event_id: int, category: str) -> int:
    booking = primary(event_id, category)
    if not booking or booking['status'] not in confirmation_rules.RESERVED:
        raise ValueError('No approved vendor to confirm')
    vendor_id = booking['vendor_id']
    if any(u['vendor_id'] == vendor_id and u['status'] == 'pending' for u in store.updates(event_id)):
        raise ValueError('Review the vendor changes before requesting confirmation')
    _, brief = store.event(event_id)
    quote = store.latest_quotes(event_id).get(vendor_id)
    if not quote or any(f['level'] == 'block' for f in rules.quote_flags(quote, store.vendor(vendor_id), brief)):
        raise ValueError('Reconfirm the prices before booking confirmation')
    latest = store.latest_confirmation(event_id, category)
    snapshot = confirmation_rules.terms(brief, quote, category)
    if latest and latest['vendor_id'] == vendor_id and latest['terms'] == snapshot:
        if latest['status'] == 'calling':
            raise ValueError('A confirmation call is already queued or in progress')
        if latest['status'] in ('pending', 'confirmed'):
            return latest['id']
    confirmation_id = store.add_confirmation(event_id, category, vendor_id, snapshot, quote)
    store.set_booking(event_id, category, 'primary', vendor_id, 'awaiting_confirmation')
    store.set_enquiry(event_id, vendor_id, category, 'awaiting_confirmation')
    store.clear_pending_checkins(event_id, category)
    store.add_alert(event_id, 'info', f"{store.vendor(vendor_id)['name']} approved as {category}; vendor booking confirmation is pending.")
    return confirmation_id


def claim(confirmation_id: int) -> dict[str, Any] | None:
    record = current(confirmation_id)
    if not store.claim_confirmation(confirmation_id):
        return None
    store.set_booking(record['event_id'], record['category'], 'primary', record['vendor_id'], 'confirming')
    store.set_enquiry(record['event_id'], record['vendor_id'], record['category'], 'confirming')
    return store.confirmation(confirmation_id)


def bind(confirmation_id: int, call_id: str, *, vendor_id: str | None, event_id: int | None) -> dict[str, Any]:
    record = current(confirmation_id)
    if record['status'] != 'calling' or record['vendor_id'] != vendor_id or record['event_id'] != event_id:
        raise ValueError('Wrong vendor, function or confirmation state')
    if record['call_id'] and record['call_id'] != call_id:
        raise ValueError('This confirmation belongs to another call')
    store.set_confirmation(confirmation_id, 'calling', call_id=call_id)
    return record


def accept_answer(confirmation_id: int, call_id: str, index: int, text: str, *, user_turn: int,
                  ready_turn: int, question: str, tts_context: str | None) -> bool:
    record = current(confirmation_id)
    if record['status'] != 'calling' or record['call_id'] != call_id:
        raise ValueError('This confirmation belongs to another or completed call')
    questions = confirmation_rules.questions(record['terms'], store.vendor(record['vendor_id'])['language'])
    if index != len(record['answers']) or index >= len(questions) or question != questions[index]['text']:
        raise ValueError('Wrong or repeated confirmation step')
    if not tts_context or user_turn <= ready_turn or not confirmation_rules.agreed(text):
        raise ValueError('Wait for the approved question to finish and obtain a clear spoken yes; conditional or unrelated replies do not confirm a booking')
    if record['answers'] and user_turn <= record['answers'][-1]['user_turn']:
        raise ValueError('One vendor answer cannot confirm two different questions')
    evidence = {'step': questions[index]['key'], 'question': question, 'answer': text[:600],
                'user_turn': user_turn, 'tts_context': tts_context, 'call_id': call_id}
    if not store.add_confirmation_answer(confirmation_id, call_id, index, evidence):
        raise ValueError('This confirmation step was already answered or superseded')
    if index + 1 < len(questions):
        return False
    # Recheck all guards at the final transition, not merely at call start.
    current(confirmation_id)
    _, brief = store.event(record['event_id'])
    message = f"{store.vendor(record['vendor_id'])['name']} confirmed the {record['category']} booking and agreed terms."
    if not store.complete_confirmation(confirmation_id, call_id, len(questions), rules.checkin_schedule(brief, record['category']), message):
        raise ValueError('This confirmation was superseded before booking activation')
    return True


def finish(confirmation_id: int, status: str, reason: str, *, call_id: str | None = None) -> None:
    record = store.confirmation(confirmation_id)
    latest = store.latest_confirmation(record['event_id'], record['category'])
    if not latest or latest['id'] != confirmation_id or record['status'] not in ('pending', 'calling'):
        return  # late call completion must not overwrite a newer approval or successful result
    booking = primary(record['event_id'], record['category'])
    if not booking or booking['vendor_id'] != record['vendor_id']:
        return
    if call_id and record['call_id'] and record['call_id'] != call_id:
        raise ValueError('Wrong call for this confirmation')
    if status not in ('no_answer', 'failed', 'interrupted', 'callback', 'declined'):
        raise ValueError('Invalid confirmation result')
    store.set_confirmation(confirmation_id, status, call_id=call_id, reason=reason)
    declined = status == 'declined'
    store.set_booking(record['event_id'], record['category'], 'primary', record['vendor_id'], 'declined' if declined else 'confirmation_failed')
    store.set_enquiry(record['event_id'], record['vendor_id'], record['category'], 'declined' if declined else 'awaiting_confirmation', note=reason)
    store.clear_pending_checkins(record['event_id'], record['category'])
    if declined:
        quote = store.latest_quotes(record['event_id']).get(record['vendor_id'])
        if quote:
            store.save_quote(record['event_id'], record['vendor_id'], call_id, {**quote, 'available': False})
    store.add_alert(record['event_id'], 'warn', f"{store.vendor(record['vendor_id'])['name']}: booking not confirmed ({reason}).")


def hold(event_id: int, vendor_id: str, reason: str) -> None:
    vendor = store.vendor(vendor_id)
    booking = primary(event_id, vendor['category'])
    if not booking or booking['vendor_id'] != vendor_id or booking['status'] not in confirmation_rules.RESERVED:
        return
    latest = store.latest_confirmation(event_id, vendor['category'])
    if latest:
        if latest['status'] == 'confirmed':
            latest = store.confirmation(store.add_confirmation(event_id, vendor['category'], vendor_id, latest['terms'], latest['approved_quote']))
        store.set_confirmation(latest['id'], 'changed', reason=reason)
    store.set_booking(event_id, vendor['category'], 'primary', vendor_id, 'awaiting_changes')
    store.set_enquiry(event_id, vendor_id, vendor['category'], 'awaiting_changes', note=reason)
    store.clear_pending_checkins(event_id, vendor['category'])


def change(confirmation_id: int, call_id: str, reason: str, *, event_change: bool) -> int | None:
    record = current(confirmation_id)
    if record['status'] not in ('calling', 'confirmed') or record['call_id'] != call_id or not reason.strip():
        raise ValueError('The change must belong to the current call and include details')
    hold(record['event_id'], record['vendor_id'], reason)
    if not event_change:
        return None  # the conversation must collect a full, price-checked quote next
    update_id = store.save_update(record['event_id'], record['vendor_id'], 'confirmation_change',
        {'notes': reason, 'confirmation_id': confirmation_id,
         'review_note': 'Date, time or venue changes cannot be applied to one vendor without rescheduling the family function. Reject to retry original terms, or cancel this selection.'}, 'pending')
    store.add_alert(record['event_id'], 'warn', f"{store.vendor(record['vendor_id'])['name']}: booking terms changed; family review is required.")
    return update_id


def resume_after_review(event_id: int, vendor_id: str) -> int | None:
    booking = primary(event_id, store.vendor(vendor_id)['category'])
    if booking and booking['vendor_id'] == vendor_id and booking['status'] in confirmation_rules.RESERVED:
        if not any(u['vendor_id'] == vendor_id and u['status'] == 'pending' for u in store.updates(event_id)):
            try:
                return request(event_id, booking['category'])
            except ValueError:
                store.set_booking(event_id, booking['category'], 'primary', vendor_id, 'confirmation_failed')
                store.set_enquiry(event_id, vendor_id, booking['category'], 'awaiting_confirmation', note='Recheck the prices before requesting booking confirmation')
    return None


def summary(event_id: int, category: str) -> dict[str, Any] | None:
    booking = primary(event_id, category)
    record = store.latest_confirmation(event_id, category)
    if not record or not booking or booking['vendor_id'] != record['vendor_id'] or booking['status'] not in confirmation_rules.RESERVED:
        return None
    return {key: record[key] for key in ('id', 'status', 'terms', 'reason', 'call_id', 'created_at', 'updated_at')}


def cancel_selection(event_id: int, category: str) -> None:
    booking = primary(event_id, category)
    if not booking or booking['status'] not in confirmation_rules.RESERVED or booking['status'] == 'active':
        raise ValueError('Only an unconfirmed selection can be cancelled here')
    latest = store.latest_confirmation(event_id, category)
    if latest:
        store.set_confirmation(latest['id'], 'superseded', reason='Family cancelled the unconfirmed selection')
    store.set_booking(event_id, category, 'primary', booking['vendor_id'], 'cancelled')
    store.set_enquiry(event_id, booking['vendor_id'], category, 'rejected')
    for update in store.updates(event_id):
        if update['vendor_id'] == booking['vendor_id'] and update['status'] == 'pending':
            store.resolve_update(update['id'], 'rejected')
    backup = next((b for b in store.bookings(event_id) if b['category'] == category and b['role'] == 'backup'), None)
    if backup and backup['status'] == 'standby':
        store.set_booking(event_id, category, 'backup', backup['vendor_id'], 'cancelled')
        store.set_enquiry(event_id, backup['vendor_id'], category, 'quoted')
    store.clear_pending_checkins(event_id, category)
    store.add_alert(event_id, 'info', f"Family cancelled the unconfirmed {category} selection; another quote can be chosen.")


def recover_interrupted() -> None:
    # No automatic redial on restart in this phase. Retain approval and offer a safe explicit retry.
    with store.connect() as conn:
        ids = [r['id'] for r in conn.execute("SELECT id FROM booking_confirmations WHERE status = 'calling'").fetchall()]
    for confirmation_id in ids:
        finish(confirmation_id, 'interrupted', 'Server restarted during confirmation')
