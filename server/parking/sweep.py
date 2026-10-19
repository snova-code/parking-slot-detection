# server/parking/sweep.py
"""Declaring a camera stale.

A camera that stops heartbeating otherwise leaves its slots advertising
whatever they last reported. v1 had no way to say "we don't know", so a dead
camera showed stale vacancy indefinitely. This marks those slots UNKNOWN,
records the outage window, and publishes the change so open streams see it.

Camera.unscoped is correct here and nowhere near a view: this is a background
process acting for every organization at once.

Locks are taken in the same order as ingest — the camera row, then its slot
states — so a sweep and a batch arriving together cannot deadlock. Staleness
is re-checked under the lock, because a heartbeat may have landed between the
query and the transaction.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from parking.models import Camera, CameraOutage, SlotState, SlotStatus
from parking.pubsub import publish_slot_states
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

logger = logging.getLogger(__name__)


def sweep_stale_cameras(now=None) -> list[dict]:
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.CAMERA_STALE_SECONDS)

    open_outage = CameraOutage.unscoped.filter(
        camera=OuterRef("pk"), ended_at__isnull=True)

    candidates = (
        Camera.unscoped.select_related("lot")
        .filter(is_active=True, lot__is_active=True,
                last_seen_at__isnull=False, last_seen_at__lt=cutoff)
        .annotate(has_open_outage=Exists(open_outage))
        .filter(has_open_outage=False)
        .order_by("last_seen_at")
    )

    declared: list[dict] = []
    for camera in candidates:
        try:
            with transaction.atomic():
                locked = (Camera.unscoped.select_for_update()
                          .filter(pk=camera.pk, last_seen_at__lt=cutoff).first())
                if locked is None:
                    continue        # heartbeat landed while we were looking

                CameraOutage.objects.for_org(locked.org_id).create(
                    camera=locked, started_at=locked.last_seen_at, detected_at=now)

                states = list(
                    SlotState.objects.for_org(locked.org_id)
                    .select_for_update(of=("self",))
                    .filter(slot__camera=locked, slot__is_active=True)
                    .exclude(status=SlotStatus.UNKNOWN)
                )
                for state in states:
                    state.status = SlotStatus.UNKNOWN
                    state.since = now
                    state.updated_at = now
                    # last_observed_at is deliberately untouched: it anchors
                    # ingest ordering on the worker's clock, and nothing was
                    # observed here.
                if states:
                    (SlotState.objects.for_org(locked.org_id)
                     .bulk_update(states, ["status", "since", "updated_at"]))
                    publish_slot_states(camera, states)

            declared.append({"camera_id": locked.pk, "lot": camera.lot.name,
                             "since": locked.last_seen_at, "slots": len(states)})
            logger.warning("camera stale camera=%s lot=%s last_seen=%s slots=%d",
                           locked.pk, camera.lot.name, locked.last_seen_at, len(states))
        except IntegrityError:
            # Another sweeper won the race for this camera's open outage.
            logger.info("outage already open camera=%s", camera.pk)
    return declared