"""Durable per-check-in attempts shared by automatic dispatch, manual calls and voice results."""

import asyncio
from datetime import datetime, timedelta

from loguru import logger

import checkin_policy
import confirmations
import store


def eligible(checkin_id: int) -> tuple[dict, dict, dict]:
    checkin = store.checkin(checkin_id)
    event_id, brief = store.event(checkin['event_id'])
    current_event = store.current_event()
    primary = confirmations.primary(event_id, checkin['category'])
    confirmation = confirmations.summary(event_id, checkin['category'])
    if not current_event or current_event[0] != event_id:
        raise ValueError('Function is no longer the current function')
    if not primary or primary['status'] != 'active' or primary['vendor_id'] != checkin['vendor_id'] or not confirmation or confirmation['status'] != 'confirmed':
        raise ValueError('The vendor is no longer the active confirmed booking')
    if checkin.get('confirmation_id') not in (None, confirmation['id']):
        raise ValueError('This check-in belongs to an older booking confirmation')
    confirmations.current(confirmation['id'])
    return checkin, brief, confirmation


def enqueue(checkin_id: int, *, manual: bool = False, at: datetime | None = None, origin: str | None = None) -> int:
    at = checkin_policy.venue_time(at or checkin_policy.now())
    checkin, brief, confirmation = eligible(checkin_id)
    if checkin['status'] != 'pending' or checkin.get('dispatch_state', 'scheduled') != 'scheduled':
        raise ValueError('This check-in is already queued, running or completed')
    if not checkin_policy.due(checkin, brief, at, manual=manual):
        raise ValueError('This check-in is not due or its useful window has passed')
    if len(store.checkin_attempts(checkin_id)) >= checkin_policy.MAX_ATTEMPTS:
        raise ValueError('Check-in retry limit reached')
    expiry = checkin_policy.expires_at(brief, checkin['category'], checkin['label'], checkin['due_at'])
    attempt_id = store.enqueue_checkin(checkin_id, confirmation['id'], expiry.isoformat(timespec='seconds'), origin or ('manual' if manual else 'auto'), at.isoformat(timespec='seconds'))
    if attempt_id is None:
        raise ValueError('Another caller already queued this check-in')
    return attempt_id


def validate_attempt(attempt_id: int, *, check_time: bool = False, at: datetime | None = None) -> tuple[dict, dict]:
    attempt = store.checkin_attempt(attempt_id)
    checkin, brief, _ = eligible(attempt['checkin_id'])
    if attempt['status'] not in ('queued', 'running') or checkin['status'] != 'pending' or checkin['current_attempt_id'] != attempt_id:
        raise ValueError('This check-in attempt is stale or completed')
    if check_time and not checkin_policy.due(checkin, brief, at or checkin_policy.now(), manual=attempt['origin'] != 'auto'):
        raise ValueError('The check-in window passed while waiting for the call channel')
    return attempt, checkin


def bind(attempt_id: int, checkin_id: int, call_id: str, vendor_id: str | None, event_id: int | None) -> dict:
    attempt, checkin = validate_attempt(attempt_id)
    if attempt['status'] != 'running' or checkin['id'] != checkin_id or vendor_id not in (None, checkin['vendor_id']) or event_id not in (None, checkin['event_id']):
        raise ValueError('Wrong call, check-in, vendor or function')
    if not store.bind_checkin_attempt(attempt_id, call_id):
        raise ValueError('This check-in attempt belongs to another call')
    return checkin


def skip(checkin_id: int, reason: str, at: datetime | None = None) -> None:
    at = checkin_policy.venue_time(at or checkin_policy.now())
    checkin = store.checkin(checkin_id)
    if store.skip_checkin(checkin_id, reason, at.isoformat(timespec='seconds')):
        store.add_alert(checkin['event_id'], 'warn', f"Check-in '{checkin['label']}' skipped: {reason}. This is not a vendor no-answer.")


def fail(attempt_id: int, reason: str, *, status: str = 'failed', at: datetime | None = None, callback_at: datetime | None = None) -> None:
    at = checkin_policy.venue_time(at or checkin_policy.now())
    attempt = store.checkin_attempt(attempt_id)
    if attempt['status'] not in ('queued', 'running'):
        return
    try:
        checkin, brief, _ = eligible(attempt['checkin_id'])
        if checkin['current_attempt_id'] != attempt_id:
            raise ValueError('A newer attempt owns this check-in')
    except (KeyError, LookupError, ValueError):
        store.release_checkin_attempt(attempt_id, 'stale', reason, at.isoformat(timespec='seconds'), None)
        return
    retry = checkin_policy.retry_at(len(store.checkin_attempts(checkin['id'])), at)
    if status == 'callback':
        retry = checkin_policy.venue_time(callback_at) if callback_at else at + timedelta(minutes=5)
    expiry = checkin_policy.venue_time(checkin['expires_at'])
    retryable = len(store.checkin_attempts(checkin['id'])) < checkin_policy.MAX_ATTEMPTS and at < retry < expiry
    store.release_checkin_attempt(attempt_id, status, reason, at.isoformat(timespec='seconds'), retry.isoformat(timespec='seconds') if retryable else None)
    enquiry = store.enquiry(checkin['event_id'], checkin['vendor_id'])
    if attempt.get('call_id') and enquiry and enquiry.get('call_id') == attempt['call_id'] and enquiry['status'] == 'in_call':
        store.set_enquiry(checkin['event_id'], checkin['vendor_id'], checkin['category'], 'confirmed', note=reason)
    if not retryable:
        skip(checkin['id'], reason + '; no useful retry remains', at)
    else:
        store.add_alert(checkin['event_id'], 'warn', f"Check-in '{checkin['label']}' will retry after a call/system interruption. No vendor no-answer was recorded.")


async def run_attempt(attempt_id: int, *, clock=checkin_policy.now) -> str:
    import channels
    import enquiries
    import workflow
    try:
        _, checkin = validate_attempt(attempt_id, check_time=True, at=clock())
    except (KeyError, LookupError, ValueError) as error:
        fail(attempt_id, str(error), status='stale', at=clock())
        return 'stale'
    if not store.claim_checkin_attempt(attempt_id, checkin_policy.venue_time(clock()).isoformat(timespec='seconds')):
        return 'already_started'
    vendor = store.vendor(checkin['vendor_id'])
    _, brief = store.event(checkin['event_id'])
    def before_dial():
        try:
            validate_attempt(attempt_id, check_time=True, at=clock())
            return True
        except (KeyError, LookupError, ValueError):
            return False
    try:
        outcome = await channels.call(vendor, {'mode': 'checkin', 'vendor_id': vendor['id'], 'event_id': checkin['event_id'],
            'checkin_id': checkin['id'], 'checkin_attempt_id': attempt_id}, enquiries.caller_info(brief, vendor, 'checkin'), before_dial=before_dial)
        if outcome.get('call_id') and store.checkin_attempt(attempt_id)['status'] == 'running':
            store.bind_checkin_attempt(attempt_id, outcome['call_id'])
        if store.checkin_attempt(attempt_id)['status'] != 'running':
            return store.checkin_attempt(attempt_id)['status']
        if outcome['result'] in ('no_answer', 'declined', 'unreachable'):
            workflow.mark_unreachable(checkin['id'], attempt_id=attempt_id)
        elif outcome['result'] == 'stale':
            fail(attempt_id, 'Booking or useful window changed before dial', status='stale', at=clock())
        else:
            fail(attempt_id, 'Call ended without a saved check-in result' if outcome['result'] == 'answered' else 'Call channel busy or failed', at=clock())
        enquiries.queue_pending_confirmations(checkin['event_id'])
        return store.checkin_attempt(attempt_id)['status']
    except asyncio.CancelledError:
        fail(attempt_id, 'Server stopped during check-in', status='interrupted', at=clock())
        raise
    except (KeyError, LookupError, ValueError):
        fail(attempt_id, 'Check-in booking or attempt was superseded', status='stale', at=clock())
        return 'stale'
    except Exception:
        logger.error('Check-in attempt {} failed', attempt_id)
        fail(attempt_id, 'Check-in service/channel failure', at=clock())
        return 'failed'


def recover(at: datetime | None = None) -> None:
    with store.connect() as conn:
        ids = [row['id'] for row in conn.execute("SELECT id FROM checkin_attempts WHERE status = 'running'").fetchall()]
    for attempt_id in ids:
        fail(attempt_id, 'Server restarted during check-in', status='interrupted', at=at)
