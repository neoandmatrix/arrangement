"""Selecting, securing and activating a backup. Standby consent is never a booked replacement."""

import os
from typing import Any

import confirmation_rules
import confirmations
import rules
import store


def selected(event_id: int, category: str) -> dict[str, Any] | None:
    return next((b for b in store.bookings(event_id) if b['category'] == category and b['role'] == 'backup'), None)


def _context(event_id: int, category: str) -> tuple[dict, dict, dict, dict]:
    primary = confirmations.primary(event_id, category)
    backup = selected(event_id, category)
    parent = store.latest_confirmation(event_id, category)
    if not primary or not backup or not parent or parent['vendor_id'] != primary['vendor_id'] or backup['vendor_id'] == primary['vendor_id']:
        raise ValueError('No valid primary approval and backup selection')
    _, brief = store.event(event_id)
    if confirmation_rules.terms(brief, parent['approved_quote'], category, parent['purpose']) != parent['terms']:
        raise ValueError('The family function changed; obtain a fresh primary approval first')
    return primary, backup, parent, brief


def _quote(event_id: int, vendor_id: str, brief: dict) -> dict:
    quote = store.latest_quotes(event_id).get(vendor_id)
    if any(u['vendor_id'] == vendor_id and u['status'] == 'pending' for u in store.updates(event_id)):
        raise ValueError('Review the backup changes first')
    if not quote or any(f['level'] == 'block' for f in rules.quote_flags(quote, store.vendor(vendor_id), brief)):
        raise ValueError('The backup quote must pass price safeguards')
    return quote


def _request(event_id: int, category: str, purpose: str, *, family_approved: bool = False) -> int:
    primary, backup, parent, brief = _context(event_id, category)
    if backup['status'] not in confirmation_rules.BACKUP_RESERVED:
        raise ValueError('The backup is not available for confirmation')
    quote = _quote(event_id, backup['vendor_id'], brief)
    snapshot = confirmation_rules.terms(brief, quote, category, purpose)
    latest = store.latest_confirmation(event_id, category, 'backup')
    if latest and latest['vendor_id'] == backup['vendor_id'] and latest['terms'] == snapshot and latest['parent_confirmation_id'] == parent['id']:
        if latest['status'] == 'calling':
            raise ValueError('The backup already has a confirmation call in progress')
        if latest['status'] in ('pending', 'confirmed'):
            return latest['id']
    confirmation_id = store.add_confirmation(event_id, category, backup['vendor_id'], snapshot, quote,
        role='backup', purpose=purpose, parent_confirmation_id=parent['id'], parent_vendor_id=primary['vendor_id'], family_approved=family_approved)
    status = 'awaiting_backup_confirmation' if purpose == 'standby' else 'awaiting_activation'
    store.set_booking(event_id, category, 'backup', backup['vendor_id'], status)
    store.set_enquiry(event_id, backup['vendor_id'], category, status)
    store.add_alert(event_id, 'info' if purpose == 'standby' else 'critical',
        f"{store.vendor(backup['vendor_id'])['name']}: {'standby availability confirmation queued; not booked and no advance is due' if purpose == 'standby' else 'replacement booking confirmation queued; not active yet'}.")
    return confirmation_id


def request_standby(event_id: int, category: str) -> int | None:
    primary, backup, parent, _ = _context(event_id, category)
    if primary['status'] != 'active' or parent['status'] != 'confirmed':
        raise ValueError('Confirm the primary booking before securing a standby')
    if backup['status'] not in confirmation_rules.BACKUP_RESERVED:
        return None
    return _request(event_id, category, 'standby')


# Owner's call (2026-11-09): no standby call after booking by default. STANDBY_CALLS=1 restores it.
STANDBY_CALLS = os.environ.get("STANDBY_CALLS", "0") == "1"


def after_primary_confirmed(event_id: int, category: str) -> int | None:
    """Keep the runner-up on file (default), or call them for standby consent (STANDBY_CALLS=1)."""
    if STANDBY_CALLS:
        return request_standby(event_id, category)
    keep_on_file(event_id, category)
    return None


def keep_on_file(event_id: int, category: str) -> None:
    """The runner-up stays our backup without a standby call; they are called only if the primary cancels.

    Owner's call (2026-11-09): no "please stay available" call after booking. The backup's quote is kept and
    the activation call (a real booking confirmation) is made only when the primary cancels.
    """
    primary, backup, parent, brief = _context(event_id, category)
    if primary['status'] != 'active' or parent['status'] != 'confirmed':
        raise ValueError('Confirm the primary booking before keeping a backup')
    if backup['status'] not in confirmation_rules.BACKUP_RESERVED:
        return
    _quote(event_id, backup['vendor_id'], brief)
    store.set_booking(event_id, category, 'backup', backup['vendor_id'], 'backup_on_file')
    store.set_enquiry(event_id, backup['vendor_id'], category, 'backup_on_file')
    store.add_alert(event_id, 'info', f"{store.vendor(backup['vendor_id'])['name']}: kept as the backup {category}. "
                    "Not booked and not called; Bandobast calls them only if the booked vendor cancels.")


def secured(event_id: int, category: str) -> bool:
    record = confirmations.summary(event_id, category, 'backup')
    if not record or record['status'] != 'confirmed' or record['purpose'] != 'standby':
        return False
    try:
        confirmations.current(record['id'])
        return True
    except (ValueError, LookupError):
        return False


def begin_replacement(event_id: int, category: str, failed_vendor_id: str, reason: str) -> int | None:
    primary = confirmations.primary(event_id, category)
    parent = store.latest_confirmation(event_id, category)
    if not primary or primary['vendor_id'] != failed_vendor_id or not parent or parent['vendor_id'] != failed_vendor_id:
        raise ValueError('This failure no longer belongs to the current primary selection')
    store.set_booking(event_id, category, 'primary', failed_vendor_id, 'replacement_pending')
    store.set_enquiry(event_id, failed_vendor_id, category, 'replacement_pending', note=reason)
    store.clear_pending_checkins(event_id, category)
    # A vendor who failed the required arrival/availability must not be selected again on the old quote.
    failed_quote = store.latest_quotes(event_id).get(failed_vendor_id)
    if failed_quote:
        store.save_quote(event_id, failed_vendor_id, None, {**failed_quote, 'available': False})
    backup = selected(event_id, category)
    if not backup or backup['status'] not in confirmation_rules.BACKUP_RESERVED:
        _needs_help(event_id, category, failed_vendor_id, reason + '; no eligible backup selected')
        return None
    _, brief = store.event(event_id)
    try:
        # An activation approval is itself pending; exclude it from generic quote-change checks.
        pending = [u for u in store.updates(event_id) if u['vendor_id'] == backup['vendor_id'] and u['status'] == 'pending']
        if any(u['kind'] != 'backup_activation' for u in pending):
            raise ValueError('Backup changes need family review')
        quote = store.latest_quotes(event_id).get(backup['vendor_id'])
        if not quote or any(f['level'] == 'block' for f in rules.quote_flags(quote, store.vendor(backup['vendor_id']), brief)):
            raise ValueError('Backup prices need rechecking')
        snapshot = confirmation_rules.terms(brief, quote, category, 'replacement')
        authorized = confirmation_rules.automatic_replacement_allowed(parent['approved_quote'], quote, brief, category)
        latest = store.latest_confirmation(event_id, category, 'backup')
        authorized |= bool(latest and latest['purpose'] == 'replacement' and latest['family_approved'] and latest['terms'] == snapshot
                           and latest['parent_confirmation_id'] == parent['id'])
        authorized |= any(u['kind'] == 'backup_activation' and u['status'] == 'approved' and u['vendor_id'] == backup['vendor_id']
            and u['data'].get('parent_confirmation_id') == parent['id'] and u['data'].get('terms') == snapshot for u in store.updates(event_id))
        if not authorized:
            if not any(u['kind'] == 'backup_activation' and u['data'].get('terms') == snapshot for u in pending):
                store.save_update(event_id, backup['vendor_id'], 'backup_activation', {'quote': quote, 'terms': snapshot,
                    'previous_quote': parent['approved_quote'],
                    'parent_confirmation_id': parent['id'], 'parent_vendor_id': failed_vendor_id,
                    'previous_price': parent['approved_quote'].get('price_amount'), 'previous_total': rules.landed_cost(parent['approved_quote'], brief),
                    'previous_advance': parent['approved_quote'].get('advance_amount'),
                    'notes': 'The backup would increase the approved total or advance. Review its terms before activation.'}, 'pending')
            old = store.latest_confirmation(event_id, category, 'backup')
            if old and old['status'] in ('pending', 'calling', 'changed'):
                store.set_confirmation(old['id'], 'superseded', reason='Activation needs family approval')
            store.set_booking(event_id, category, 'backup', backup['vendor_id'], 'activation_awaiting_approval')
            store.add_alert(event_id, 'critical', f"{store.vendor(failed_vendor_id)['name']}: {reason}. Backup activation needs family approval; no replacement is active.")
            return None
        return _request(event_id, category, 'replacement', family_approved=True)
    except ValueError as error:
        _needs_help(event_id, category, failed_vendor_id, f'{reason}; {error}')
        return None


def _needs_help(event_id: int, category: str, failed_vendor_id: str, reason: str) -> None:
    store.set_booking(event_id, category, 'primary', failed_vendor_id, 'needs_replacement')
    store.set_enquiry(event_id, failed_vendor_id, category, 'needs_replacement', note=reason)
    store.add_alert(event_id, 'critical', f"{store.vendor(failed_vendor_id)['name']} ({category}): {reason}. No confirmed replacement; please choose or contact another vendor.")


def decide_activation(event_id: int, update_id: int, approve: bool) -> int | None:
    update = store.update(update_id)
    if update['event_id'] != event_id or update['kind'] != 'backup_activation' or update['status'] != 'pending':
        raise ValueError('This backup activation is no longer pending')
    category = store.vendor(update['vendor_id'])['category']
    primary, backup, parent, brief = _context(event_id, category)
    if primary['status'] != 'replacement_pending' or backup['vendor_id'] != update['vendor_id'] or parent['id'] != update['data']['parent_confirmation_id']:
        raise ValueError('The primary or backup selection changed since this request')
    current_quote = store.latest_quotes(event_id).get(backup['vendor_id'])
    if not current_quote or confirmation_rules.terms(brief, current_quote, category, 'replacement') != update['data']['terms']:
        raise ValueError('The backup terms changed; request a fresh review')
    if approve and any(f['level'] == 'block' for f in rules.quote_flags(current_quote, store.vendor(backup['vendor_id']), brief)):
        raise ValueError('The backup quote does not pass price safeguards')
    store.resolve_update(update_id, 'approved' if approve else 'rejected')
    if approve:
        return begin_replacement(event_id, category, primary['vendor_id'], 'Family approved backup activation terms')
    store.set_booking(event_id, category, 'backup', backup['vendor_id'], 'cancelled')
    _needs_help(event_id, category, primary['vendor_id'], 'Family declined the backup activation terms')
    return None


def choose(event_id: int, category: str, vendor_id: str) -> int | None:
    primary = confirmations.primary(event_id, category)
    parent = store.latest_confirmation(event_id, category)
    _, brief = store.event(event_id)
    vendor = store.vendor(vendor_id)
    if not primary or not parent or parent['vendor_id'] != primary['vendor_id'] or vendor['category'] != category or vendor_id == primary['vendor_id']:
        raise ValueError('Choose another vendor from this service')
    if primary['status'] not in ('active', 'replacement_pending', 'needs_replacement'):
        raise ValueError('Confirm the primary first, or choose a replacement after failure')
    _quote(event_id, vendor_id, brief)
    old = selected(event_id, category)
    if old and old['vendor_id'] == vendor_id:
        return retry(event_id, category)
    if old and old['vendor_id'] != vendor_id and old['status'] in confirmation_rules.BACKUP_RESERVED:
        store.set_enquiry(event_id, old['vendor_id'], category, 'quoted', note='Family chose another backup')
        for update in store.updates(event_id):
            if update['vendor_id'] == old['vendor_id'] and update['kind'] == 'backup_activation' and update['status'] == 'pending':
                store.resolve_update(update['id'], 'superseded')
    latest = store.latest_confirmation(event_id, category, 'backup')
    if latest and latest['status'] in ('pending', 'calling', 'changed'):
        store.set_confirmation(latest['id'], 'superseded', reason='Family changed backup selection')
    store.set_booking(event_id, category, 'backup', vendor_id, 'awaiting_backup_confirmation')
    if primary['status'] == 'active':
        return after_primary_confirmed(event_id, category)
    return begin_replacement(event_id, category, primary['vendor_id'], 'Family chose a replacement candidate')


def retry(event_id: int, category: str) -> int | None:
    primary = confirmations.primary(event_id, category)
    if not primary:
        raise ValueError('No primary selection')
    if primary['status'] == 'active':
        return after_primary_confirmed(event_id, category)
    if primary['status'] in ('replacement_pending', 'needs_replacement'):
        return begin_replacement(event_id, category, primary['vendor_id'], 'Retrying backup activation')
    raise ValueError('The primary must be confirmed before standby confirmation')
