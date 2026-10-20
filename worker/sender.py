# worker/sender.py
"""Draining the outbox.

A batch is removed from the queue only after the server has answered. A
timeout leaves it queued and it is sent again; the id travels with the event,
so the server's ON CONFLICT DO NOTHING turns the resend into a no-op.

Rejected events are acknowledged rather than retried. A rejection means the
server will never accept this event — an unknown slot, a deactivated one, a
timestamp from the future — and leaving it in place would block every event
behind it forever.

Backoff is per-failure and capped. The worker keeps observing while the server
is away; only the sending pauses.
"""
from __future__ import annotations

import logging
import time

from worker.api_client import ApiClient, ApiError, AuthRejected
from worker.queue import EventQueue

logger = logging.getLogger(__name__)

BATCH_LIMIT = 500          # 04_API_CONTRACT.md
BACKOFF_SECONDS = (1, 2, 5, 15, 30, 60)


class Sender:
    def __init__(self, client: ApiClient, queue: EventQueue, camera_id) -> None:
        self.client = client
        self.queue = queue
        self.camera_id = camera_id
        self.failures = 0
        self.blocked_until = 0.0
        self.sent = 0

    @property
    def backoff(self) -> float:
        if not self.failures:
            return 0.0
        return BACKOFF_SECONDS[min(self.failures - 1, len(BACKOFF_SECONDS) - 1)]

    def flush(self, now: float | None = None) -> int:
        """Sends as much of the queue as the server will take. Returns the
        number of events acknowledged."""
        now = now if now is not None else time.monotonic()
        if now < self.blocked_until:
            return 0

        acknowledged = 0
        while True:
            batch = self.queue.peek(BATCH_LIMIT)
            if not batch:
                return acknowledged
            try:
                result = self.client.post_events(
                    self.camera_id, [event.payload for event in batch])
            except AuthRejected:
                raise
            except ApiError as exc:
                self.failures += 1
                self.blocked_until = now + self.backoff
                logger.warning("send failed (%s); %d event(s) queued, retrying in %.0fs",
                               exc, self.queue.depth(), self.backoff)
                return acknowledged

            self.queue.acknowledge(batch)
            acknowledged += len(batch)
            self.sent += result.accepted
            self.failures = 0
            self.blocked_until = 0.0

            if result.rejected:
                for rejection in result.rejected[:5]:
                    logger.warning("server rejected event %s: %s",
                                   rejection.get("id"), rejection.get("reason"))
            if result.previous_status_mismatches:
                logger.warning("%d event(s) disagreed with the server's state — "
                               "events were probably lost earlier",
                               result.previous_status_mismatches)
            if len(batch) < BATCH_LIMIT:
                return acknowledged