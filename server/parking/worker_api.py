# server/parking/worker_api.py
"""Worker endpoints. Router-level auth is the device token only: no session
can reach these routes and no device token can reach a human route.

ninja marks its views csrf_exempt at the Django layer, so bearer-authenticated
POSTs are not blocked by CsrfViewMiddleware; session CSRF is re-applied by the
session auth classes, which never appear here.
"""
from __future__ import annotations
import logging


from decimal import Decimal
from typing import Literal
from uuid import UUID

from django.utils import timezone
from ninja import Router, Schema
from pydantic import AwareDatetime, Field, model_validator

from config.errors import ApiError
from parking.ingest import MAX_BATCH, IncomingEvent, ingest_events
from parking.models import Camera, CameraOutage
from parking.security import device_token
from parking.worker_config import build_config, resync_slot_ids
from parking.schemas import ReferenceFrameOut
from config.throttles import (worker_config_throttle, worker_events_throttle, worker_heartbeat_throttle)

worker_router = Router(tags=["worker"], auth=device_token)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- config ----

class ModelOut(Schema):
    version: str
    url: str
    sha256: str
    input_size: list[int] = Field(description="[height, width]")


class SmoothingOut(Schema):
    window: int
    min_agreement: int


class SamplingOut(Schema):
    target_fps: float


class SlotOut(Schema):
    id: UUID
    label: str
    polygon: list[list[float]]


class WorkerConfigOut(Schema):
    camera_id: UUID
    lot_name: str
    source_uri: str
    reference_frame: ReferenceFrameOut
    model: ModelOut
    smoothing: SmoothingOut
    sampling: SamplingOut
    slots: list[SlotOut]
    config_version: str


# ---------------------------------------------------------------- events ----
class EventIn(Schema):
    id: UUID
    slot_id: UUID
    previous_status: Literal["VACANT", "OCCUPIED", "UNKNOWN"]
    status: Literal["VACANT", "OCCUPIED"]
    confidence: float = Field(ge=0, le=1)
    observed_at: AwareDatetime
    model_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _is_a_transition(self):
        if self.status == self.previous_status:
            raise ValueError("an event must change status")
        return self


class EventBatchIn(Schema):
    camera_id: UUID
    events: list[EventIn]


class RejectedOut(Schema):
    id: UUID
    reason: str


class EventBatchOut(Schema):
    accepted: int
    duplicates: int
    rejected: list[RejectedOut]
    previous_status_mismatches: int


# ------------------------------------------------------------- heartbeat ----
class HeartbeatIn(Schema):
    camera_id: UUID
    fps: float = Field(ge=0, lt=1000)
    frames_processed: int = Field(ge=0)
    queue_depth: int = Field(ge=0)
    model_version: str = Field(min_length=1, max_length=64)
    uptime_seconds: float = Field(ge=0)


class HeartbeatOut(Schema):
    ok: bool
    config_version: str
    resync_slot_ids: list[UUID]


# ---------------------------------------------------------------- routes ----
def _require_own_camera(request, camera_id: UUID) -> Camera:
    camera = request.auth.camera
    if camera_id != camera.pk:
        raise ApiError(403, "camera_mismatch",
                       "This token belongs to a different camera.",
                       {"token_camera_id": str(camera.pk)})
    return camera


@worker_router.get("/config", response=WorkerConfigOut, throttle=[worker_config_throttle])
def worker_config(request):
    return build_config(request.auth.camera)


@worker_router.post("/events", response=EventBatchOut, throttle=[worker_events_throttle])
def post_events(request, payload: EventBatchIn):
    camera = _require_own_camera(request, payload.camera_id)
    if len(payload.events) > MAX_BATCH:
        raise ApiError(413, "batch_too_large", f"At most {MAX_BATCH} events per batch.",
                       {"received": len(payload.events), "limit": MAX_BATCH})
    result = ingest_events(camera, [
        IncomingEvent(
            id=e.id, slot_id=e.slot_id,
            previous_status=e.previous_status, status=e.status,
            confidence=Decimal(str(round(e.confidence, 3))),
            observed_at=e.observed_at, model_version=e.model_version,
        )
        for e in payload.events
    ])
    return {
        "accepted": result.accepted,
        "duplicates": result.duplicates,
        "rejected": result.rejected,
        "previous_status_mismatches": result.previous_status_mismatches,
    }


@worker_router.post("/heartbeat", response=HeartbeatOut, throttle=[worker_heartbeat_throttle])
def heartbeat(request, payload: HeartbeatIn):
    """Records liveness first, so a camera counts as alive even when its
    config cannot be built. frames_processed, queue_depth, uptime_seconds and
    model_version are validated but not persisted.

    Closing the outage here rather than in the sweep is what makes its end
    exact: the sweep would only know recovery to within its own interval."""
    camera = _require_own_camera(request, payload.camera_id)
    now = timezone.now()
    (Camera.objects.for_org(camera.org_id).filter(pk=camera.pk)
     .update(last_seen_at=now, last_fps=Decimal(str(round(payload.fps, 2)))))
    reopened = (CameraOutage.objects.for_org(camera.org_id)
                .filter(camera=camera, ended_at__isnull=True).update(ended_at=now))
    if reopened:
        logger.info("camera back online camera=%s", camera.pk)
    return {
        "ok": True,
        "config_version": build_config(camera)["config_version"],
        "resync_slot_ids": resync_slot_ids(camera),
    }