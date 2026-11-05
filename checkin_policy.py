"""Pure scheduling policy. Venue-local times are IST, regardless of the server's timezone."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import rules

IST = ZoneInfo('Asia/Kolkata')
MAX_ATTEMPTS = 3


def venue_time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def now() -> datetime:
    return datetime.now(IST)


def expires_at(brief: dict, category: str, label: str, due_at: str) -> datetime:
    due = venue_time(due_at)
    grace = timedelta(hours=2) if label.startswith(('D-3', 'D-1')) else timedelta(minutes=10 if label.startswith('Final') else 30)
    arrival = venue_time(rules.event_start(brief) - rules.ARRIVAL_LEAD[category])
    return min(due + grace, arrival)


def retry_at(attempt_number: int, at: datetime) -> datetime:
    return venue_time(at) + timedelta(seconds=min(60 * 2 ** max(0, attempt_number - 1), 300))


def due(checkin: dict, brief: dict, at: datetime, *, manual: bool = False) -> bool:
    at = venue_time(at)
    expiry = venue_time(checkin['expires_at']) if checkin.get('expires_at') else expires_at(brief, checkin['category'], checkin['label'], checkin['due_at'])
    if at >= expiry:
        return False
    next_try = checkin.get('next_attempt_at')
    if next_try and at < venue_time(next_try):
        return False
    return manual or at >= venue_time(checkin['due_at'])
