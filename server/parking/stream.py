# server/parking/stream.py
"""Server-Sent Events for one lot's live slot states.

A connection looks like:

    event: snapshot     every active slot's current state, sent immediately
    event: slot_update  one per committed ingest batch
    event: heartbeat    every SSE_HEARTBEAT_SECONDS
    event: closing      the server is ending the stream, with a reason

Reconnection re-snapshots rather than replaying: pub/sub keeps no history,
EventSource reconnects by itself, and a snapshot is a complete resync. The
snapshot carries statuses only — polygons come from GET /lots/{id}/slots and
do not change while a stream is open without a new annotation.

Access is re-checked on every heartbeat. "Role is read per request" is easy to
honour for a request lasting milliseconds; a stream lasts half an hour, and a
revoked member must stop receiving data inside it.

Each connection occupies a worker thread for its whole life, so capacity is
threads: run gunicorn with `--worker-class gthread --threads N`. An async ASGI
stream would lift that limit and is not built here.
"""
from __future__ import annotations

import json
import logging
import time

import redis
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from accounts.models import Membership
from parking.models import ParkingLot, ParkingSlot, SlotStatus
from parking.pubsub import subscription

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0


def _frame(event: str, payload: str) -> bytes:
    # json.dumps never emits a raw newline, so a single data: line is always
    # a complete, valid SSE frame.
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _json_frame(event: str, data: dict) -> bytes:
    return _frame(event, json.dumps(data, default=str))


def snapshot(lot: ParkingLot) -> dict:
    slots = (ParkingSlot.objects.for_org(lot.org_id)
             .filter(camera__lot=lot, camera__is_active=True, is_active=True)
             .select_related("state").order_by("label"))
    entries = []
    for slot in slots:
        state = getattr(slot, "state", None)
        entries.append({
            "slot_id": str(slot.pk),
            "status": state.status if state else SlotStatus.UNKNOWN,
            "confidence": float(state.confidence) if state and state.confidence is not None else None,
            "since": state.since.isoformat() if state else None,
            "at": state.last_observed_at.isoformat()
                 if state and state.last_observed_at else None,
        })
    return {"lot_id": str(lot.pk), "slots": entries}


def _still_permitted(lot: ParkingLot, user) -> bool:
    return (
        Membership.objects.for_org(lot.org_id).filter(user=user).exists()
        and ParkingLot.objects.for_org(lot.org_id)
        .filter(pk=lot.pk, is_active=True).exists()
    )


def sse_stream(lot: ParkingLot, user):
    heartbeat_every = settings.SSE_HEARTBEAT_SECONDS
    deadline = time.monotonic() + settings.SSE_MAX_CONNECTION_SECONDS
    next_heartbeat = time.monotonic() + heartbeat_every

    with subscription(lot.pk) as pubsub:
        yield _json_frame("snapshot", snapshot(lot))

        while True:
            if time.monotonic() >= deadline:
                yield _json_frame("closing", {"reason": "max_connection_seconds"})
                return
            try:
                message = pubsub.get_message(timeout=POLL_SECONDS)
            except redis.RedisError:
                logger.warning("stream lost the bus lot=%s", lot.pk, exc_info=True)
                yield _json_frame("closing", {"reason": "bus_unavailable"})
                return

            if message and message.get("type") == "message":
                # Forwarded verbatim: re-encoding could only introduce a
                # difference between what was published and what is delivered.
                yield _frame("slot_update", message["data"])
                continue

            if time.monotonic() >= next_heartbeat:
                next_heartbeat = time.monotonic() + heartbeat_every
                close_old_connections()
                if not _still_permitted(lot, user):
                    yield _json_frame("closing", {"reason": "access_revoked"})
                    return
                yield _json_frame("heartbeat", {"at": timezone.now()})