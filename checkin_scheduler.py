"""A single lightweight worker over durable SQLite check-ins. No external scheduler dependency."""

import asyncio
import os

from loguru import logger

import channels
import checkin_dispatch
import checkin_policy
import store


class CheckinScheduler:
    def __init__(self, *, clock=checkin_policy.now, poll_seconds: float = 5):
        self.clock = clock
        self.poll_seconds = max(0.1, poll_seconds)
        self.task: asyncio.Task | None = None

    async def tick(self) -> int | None:
        event = store.current_event()
        if not event:
            return None
        at = checkin_policy.venue_time(self.clock())
        candidate = None
        for checkin in store.checkins(event[0]):
            if checkin['status'] != 'pending' or checkin['dispatch_state'] == 'running':
                continue
            try:
                checkin_dispatch.eligible(checkin['id'])
            except (KeyError, LookupError, ValueError) as error:
                checkin_dispatch.skip(checkin['id'], str(error), at)
                continue
            expiry = checkin_policy.venue_time(checkin['expires_at']) if checkin.get('expires_at') else checkin_policy.expires_at(event[1], checkin['category'], checkin['label'], checkin['due_at'])
            if at >= expiry:
                checkin_dispatch.skip(checkin['id'], 'Useful call window passed', at)
                continue
            if candidate is None and (checkin['dispatch_state'] == 'queued' or checkin_policy.due(checkin, event[1], at)):
                candidate = checkin
        # Never build a backlog of already-expired dials while another AI call owns Gnani.
        if candidate is None or channels.CALL_LOCK.locked():
            return None
        try:
            attempt_id = candidate['current_attempt_id'] if candidate['dispatch_state'] == 'queued' else checkin_dispatch.enqueue(candidate['id'], at=at)
            await checkin_dispatch.run_attempt(attempt_id, clock=self.clock)
            return attempt_id
        except (KeyError, LookupError, ValueError):
            return None

    async def _run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error('Automatic check-in scheduler tick failed; will retry')
            await asyncio.sleep(self.poll_seconds)

    def start(self):
        checkin_dispatch.recover(self.clock())
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


def enabled() -> bool:
    return os.environ.get('AUTO_CHECKINS', '1').lower() not in ('0', 'false', 'off')
