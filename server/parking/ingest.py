# server/parking/ingest.py
"""The server side of POST /worker/events.

Rules, all enforced here:

  Idempotent by event id. A retried batch inserts nothing and changes no
  state. A repeated id within one batch counts the same way.

  Structural errors reject the whole batch (422, before this runs).
  Referential errors reject only the event: a slot that is not this camera's,
  a deactivated slot, a timestamp from the future. "Not this camera's" is one
  reason whether the slot belongs to another camera, another organization, or
  nobody — nothing about other tenants leaks through the reason.

  Ingestion for one camera is serialised by a row lock on that camera;
  different cameras proceed in parallel. Under the lock, "which ids are new"
  and "what is each slot's state" cannot race.

  Slot state follows the newest observation by worker clock, not arrival
  order. `since` moves only when the status changes, so re-announcing an
  unchanged slot does not reset dwell time. UNKNOWN is replaced by the next
  observation newer than the last one applied.

  previous_status is the worker's claim. When it names a known state that
  disagrees with the server's, the event is stored anyway and the gap is
  counted and logged — it usually means an event was lost. UNKNOWN as
  previous_status is never a mismatch: the worker is saying it has no prior.

Known limitation: state comparison assumes the worker clock does not step
backwards. After a backwards step, events are still stored, but state stops
advancing until the clock passes the last observation applied.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from parking.models import Camera, OccupancyEvent, ParkingSlot, SlotState, SlotStatus
from parking.pubsub import publish
from parking.pubsub import publish_slot_states

logger = logging.getLogger(__name__)

MAX_BATCH = 500
MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class IncomingEvent:
    id: UUID
    slot_id: UUID
    previous_status: str
    status: str
    confidence: Decimal
    observed_at: datetime
    model_version: str


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict] = field(default_factory=list)
    previous_status_mismatches: int = 0
    changed_slot_ids: list[UUID] = field(default_factory=list)


def ingest_events(camera: Camera, events: list[IncomingEvent]) -> IngestResult:
    result = IngestResult()
    now = timezone.now()
    org_id = camera.org_id

    with transaction.atomic():
        Camera.objects.for_org(org_id).select_for_update().get(pk=camera.pk)

        slot_is_active = dict(
            ParkingSlot.objects.for_org(org_id)
            .filter(camera=camera, pk__in={e.slot_id for e in events})
            .values_list("pk", "is_active")
        )
        # Scoped to this camera. Ids are worker-generated UUID4s; a clash with
        # another camera's event is not a retry, so it is not counted as one —
        # it fails the insert loudly instead.
        already_stored = set(
            OccupancyEvent.objects.for_org(org_id)
            .filter(camera=camera, pk__in={e.id for e in events})
            .values_list("pk", flat=True)
        )

        fresh: list[IncomingEvent] = []
        seen: set[UUID] = set()
        for event in events:
            if event.id in already_stored or event.id in seen:
                result.duplicates += 1
                continue
            seen.add(event.id)
            reason = _rejection(event, slot_is_active, now)
            if reason:
                result.rejected.append({"id": event.id, "reason": reason})
            else:
                fresh.append(event)

        OccupancyEvent.objects.for_org(org_id).bulk_create([
            OccupancyEvent(
                id=e.id, slot_id=e.slot_id, camera_id=camera.pk,
                previous_status=e.previous_status, status=e.status,
                confidence=e.confidence, observed_at=e.observed_at,
                model_version=e.model_version,
            )
            for e in fresh
        ])
        result.accepted = len(fresh)
        _apply_to_states(camera, fresh, now, result)

    return result


def _rejection(event: IncomingEvent, slot_is_active: dict, now: datetime) -> str | None:
    active = slot_is_active.get(event.slot_id)
    if active is None:
        return "unknown_slot"
    if not active:
        return "slot_inactive"
    if event.observed_at > now + MAX_CLOCK_SKEW:
        return "observed_in_future"
    return None


def _apply_to_states(camera: Camera, fresh: list[IncomingEvent],
                     now: datetime, result: IngestResult) -> None:
    if not fresh:
        return
    scoped = SlotState.objects.for_org(camera.org_id)
    slot_ids = {e.slot_id for e in fresh}

    # A slot gets a state row the first time it is observed. Nothing else
    # creates rows for this camera's slots while the camera lock is held.
    existing = set(scoped.filter(slot_id__in=slot_ids).values_list("slot_id", flat=True))
    scoped.bulk_create([SlotState(slot_id=slot_id, status=SlotStatus.UNKNOWN, since=now)
                        for slot_id in slot_ids - existing])
    states = {s.slot_id: s for s in scoped.filter(slot_id__in=slot_ids)}

    changed: list[SlotState] = []
    ordered = sorted(fresh, key=lambda e: (e.slot_id, e.observed_at))
    for slot_id, slot_events in groupby(ordered, key=lambda e: e.slot_id):
        state = states[slot_id]
        before = (state.status, state.confidence, state.since, state.last_observed_at)
        for event in slot_events:
            if state.last_observed_at is not None and event.observed_at <= state.last_observed_at:
                continue    # older than what is already known: history only
            if (event.previous_status != SlotStatus.UNKNOWN
                    and state.status != SlotStatus.UNKNOWN
                    and event.previous_status != state.status):
                result.previous_status_mismatches += 1
                logger.warning(
                    "previous_status mismatch camera=%s slot=%s server=%s worker=%s event=%s",
                    camera.pk, slot_id, state.status, event.previous_status, event.id)
            if event.status != state.status:
                state.status = event.status
                state.since = event.observed_at
            state.confidence = event.confidence
            state.last_observed_at = event.observed_at
        if (state.status, state.confidence, state.since, state.last_observed_at) != before:
            state.updated_at = now
            changed.append(state)

    if changed:
        scoped.bulk_update(changed, ["status", "confidence", "since",
                                     "last_observed_at", "updated_at"])
        publish_slot_states(camera, changed)
    result.changed_slot_ids = sorted(s.slot_id for s in changed)

