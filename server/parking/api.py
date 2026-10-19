# server/parking/api.py
"""Endpoints for humans. Auth is inherited from the API default (a member of
the active organization); only the owner-restricted route says otherwise.
OPERATOR is read-only, which is exactly what these reads are.
"""
from __future__ import annotations
import redis

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from django.db.models import Count, Max, Q
from django.http import HttpResponse
from ninja import Query, Router, Schema

from accounts.security import owner_only
from config.errors import ApiError
from parking.analytics import lot_analytics
from parking.models import Camera, ParkingLot, ParkingSlot, SlotStatus
from parking.schemas import ReferenceFrameOut
from parking.tokens import generate_device_token

from django.db import IntegrityError, transaction
from django.utils import timezone
from pydantic import AwareDatetime, Field, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from parking.geometry import canonical_polygon
from parking.models import SlotType
from parking.versioning import slots_version
from parking.worker_config import load_model_card

from django.http import StreamingHttpResponse

from parking.pubsub import client as redis_client
from parking.stream import sse_stream
from urllib.parse import urlencode
from parking import presence


cameras_router = Router(tags=["cameras"])
lots_router = Router(tags=["lots"])


# --------------------------------------------------------------- cameras ----
class RotatedTokenOut(Schema):
    camera_id: UUID
    device_token: str


@cameras_router.post("/{camera_id}/token:rotate", response=RotatedTokenOut,
                     auth=owner_only)
def rotate_device_token(request, camera_id: UUID, response: HttpResponse):
    """Replace a camera's device token. The old token stops working at once —
    no overlap window, because rotation is usually a response to a leak. The
    new token appears in this response only."""
    token, digest = generate_device_token()
    updated = (Camera.objects.for_org(request.auth.org)
               .filter(pk=camera_id).update(device_token_hash=digest))
    if not updated:
        # Same answer for another org's camera and a missing one.
        raise ApiError(404, "camera_not_found", "No such camera.")
    response["Cache-Control"] = "no-store"
    return {"camera_id": camera_id, "device_token": token}


# ------------------------------------------------------------------ lots ----
class LotSummaryOut(Schema):
    id: UUID
    name: str
    timezone: str
    total_slots: int
    vacant: int
    occupied: int
    unknown: int
    last_updated: datetime | None


class SlotStateOut(Schema):
    id: UUID
    label: str
    slot_type: str
    polygon: list[list[float]]
    status: str
    confidence: float | None
    since: datetime | None
    last_observed_at: datetime | None


class CameraSlotsOut(Schema):
    id: UUID
    name: str
    reference_frame: ReferenceFrameOut
    slots_version: str
    last_seen_at: datetime | None
    slots: list[SlotStateOut]


class LotSlotsOut(Schema):
    lot_id: UUID
    lot_name: str
    timezone: str
    cameras: list[CameraSlotsOut]


class OccupancyBucketOut(Schema):
    bucket: datetime
    rate: float | None
    coverage: float | None


class DwellOut(Schema):
    p50: float | None
    p90: float | None
    mean: float | None
    samples: int


class AnalyticsOut(Schema):
    lot_id: UUID
    timezone: str
    bucket: str
    slots_counted: int
    observed_slot_days: float
    occupancy_rate: list[OccupancyBucketOut]
    dwell_minutes: DwellOut
    turnover_per_slot_per_day: float | None


class AnalyticsQuery(Schema):
    start: AwareDatetime = Field(alias="from")
    end: AwareDatetime = Field(alias="to")
    bucket: Literal["hour", "day"] = "hour"


def _get_lot(request, lot_id: UUID) -> ParkingLot:
    lot = ParkingLot.objects.for_org(request.auth.org).filter(pk=lot_id).first()
    if lot is None:
        raise ApiError(404, "lot_not_found", "No such lot.")
    return lot


@lots_router.get("", response=list[LotSummaryOut])
def list_lots(request):
    """`unknown` is computed as the remainder, so the three counts always sum
    to `total_slots` — a slot that has never been observed has no state row
    and is, correctly, not known."""
    active_slot = Q(cameras__is_active=True, cameras__slots__is_active=True)
    lots = (
        ParkingLot.objects.for_org(request.auth.org)
        .filter(is_active=True)
        .annotate(
            total_slots=Count("cameras__slots", filter=active_slot, distinct=True),
            vacant=Count("cameras__slots", distinct=True, filter=active_slot
                         & Q(cameras__slots__state__status=SlotStatus.VACANT)),
            occupied=Count("cameras__slots", distinct=True, filter=active_slot
                           & Q(cameras__slots__state__status=SlotStatus.OCCUPIED)),
            last_updated=Max("cameras__slots__state__updated_at", filter=active_slot),
        )
        .order_by("name")
    )
    return [
        {
            "id": lot.pk, "name": lot.name, "timezone": lot.timezone,
            "total_slots": lot.total_slots, "vacant": lot.vacant,
            "occupied": lot.occupied,
            "unknown": lot.total_slots - lot.vacant - lot.occupied,
            "last_updated": lot.last_updated,
        }
        for lot in lots
    ]


@lots_router.get("/{lot_id}/slots", response=LotSlotsOut)
def lot_slots(request, lot_id: UUID):
    """Slots are grouped under the camera they belong to, so each polygon
    travels with the reference frame it was drawn against. v1 scaled by x2 in
    one path and x1.8/x1.9 in another; here there is nothing to mismatch."""
    lot = _get_lot(request, lot_id)
    cameras = (Camera.objects.for_org(request.auth.org)
               .filter(lot=lot, is_active=True).order_by("name"))
    slots = (ParkingSlot.objects.for_org(request.auth.org)
             .filter(camera__lot=lot, camera__is_active=True, is_active=True)
             .select_related("state").order_by("label"))

    by_camera: dict[UUID, list[dict]] = {camera.pk: [] for camera in cameras}
    for slot in slots:
        state = getattr(slot, "state", None)
        by_camera[slot.camera_id].append({
            "id": slot.pk, "label": slot.label, "slot_type": slot.slot_type,
            "polygon": slot.polygon,
            "status": state.status if state else SlotStatus.UNKNOWN,
            "confidence": float(state.confidence) if state and state.confidence is not None else None,
            "since": state.since if state else None,
            "last_observed_at": state.last_observed_at if state else None,
        })

    return {
        "lot_id": lot.pk, "lot_name": lot.name, "timezone": lot.timezone,
        "cameras": [
            {
                "id": camera.pk, "name": camera.name,
                "reference_frame": {"width": camera.frame_width,
                                    "height": camera.frame_height},
                "last_seen_at": camera.last_seen_at,
                "slots": by_camera[camera.pk],
                "slots_version": slots_version(camera),
            }
            for camera in cameras
        ],
    }


@lots_router.get("/{lot_id}/analytics", response=AnalyticsOut)
def analytics(request, lot_id: UUID, params: Query[AnalyticsQuery]):
    lot = _get_lot(request, lot_id)
    return lot_analytics(lot, params.start, params.end, params.bucket)

# ------------------------------------------------------------- write DTOs ----
Point = Annotated[list[float], Field(min_length=2, max_length=2)]
Quad = Annotated[list[Point], Field(min_length=4, max_length=4)]
SOURCE_SCHEMES = ("file://", "rtsp://", "rtsps://", "http://", "https://")


class ReferenceFrameIn(Schema):
    width: int = Field(ge=1, le=10000)
    height: int = Field(ge=1, le=10000)


class LotIn(Schema):
    name: str = Field(min_length=1, max_length=255)
    street: str = Field("", max_length=255)
    city: str = Field("", max_length=255)
    district: str = Field("", max_length=255)
    country: str = Field("", max_length=255)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    timezone: str = "Asia/Kathmandu"

    @model_validator(mode="after")
    def _check(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be given together")
        try:
            # Validated here so analytics can trust it: bucket boundaries are
            # computed in this zone.
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{self.timezone!r} is not a known IANA timezone") from None
        return self


class LotOut(Schema):
    id: UUID
    name: str
    street: str
    city: str
    district: str
    country: str
    latitude: float | None
    longitude: float | None
    timezone: str
    created_at: datetime


class CameraIn(Schema):
    name: str = Field(min_length=1, max_length=255)
    source_uri: str = Field(min_length=1)
    reference_frame: ReferenceFrameIn
    model_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_source(self):
        if not self.source_uri.startswith(SOURCE_SCHEMES):
            raise ValueError(f"source_uri must start with one of {SOURCE_SCHEMES}")
        return self


class CameraCreatedOut(Schema):
    id: UUID
    lot_id: UUID
    name: str
    source_uri: str
    reference_frame: ReferenceFrameOut
    model_version: str
    slots_version: str
    device_token: str


class SlotIn(Schema):
    label: str = Field(min_length=1, max_length=32)
    polygon: Quad
    slot_type: Literal["STANDARD", "ACCESSIBLE", "EV"] = SlotType.STANDARD


class BulkSlotsIn(Schema):
    slots_version: str = Field(min_length=1, max_length=64)
    reference_frame: ReferenceFrameIn
    slots: Annotated[list[SlotIn], Field(max_length=500)]


class StoredSlotOut(Schema):
    id: UUID
    label: str
    slot_type: str
    polygon: list[list[float]]


class BulkSlotsOut(Schema):
    camera_id: UUID
    reference_frame: ReferenceFrameOut
    slots_version: str
    created: int
    updated: int
    deactivated: int
    slots: list[StoredSlotOut]


# ----------------------------------------------------------- write routes ----
@lots_router.post("", response=LotOut, auth=owner_only)
def create_lot(request, payload: LotIn):
    try:
        with transaction.atomic():
            lot = ParkingLot.objects.for_org(request.auth.org).create(
                name=payload.name, street=payload.street, city=payload.city,
                district=payload.district, country=payload.country,
                latitude=None if payload.latitude is None else Decimal(str(round(payload.latitude, 6))),
                longitude=None if payload.longitude is None else Decimal(str(round(payload.longitude, 6))),
                timezone=payload.timezone,
            )
    except IntegrityError as exc:
        raise ApiError(409, "lot_name_taken",
                       "This organization already has a lot with that name.") from exc
    return lot


@lots_router.post("/{lot_id}/cameras", response=CameraCreatedOut, auth=owner_only)
def create_camera(request, lot_id: UUID, payload: CameraIn, response: HttpResponse):
    """The device token appears in this response only; the server keeps its
    SHA-256. Lost it? POST /cameras/{id}/token:rotate."""
    lot = _get_lot(request, lot_id)
    try:
        # Fail now rather than at the worker's first config poll.
        load_model_card(payload.model_version)
    except ApiError as exc:
        raise ApiError(422, "unknown_model_version",
                       "No model card for that version on this server.",
                       exc.detail) from exc

    token, digest = generate_device_token()
    camera = Camera.objects.for_org(request.auth.org).create(
        lot=lot, name=payload.name, source_uri=payload.source_uri,
        device_token_hash=digest,
        frame_width=payload.reference_frame.width,
        frame_height=payload.reference_frame.height,
        model_version=payload.model_version,
    )
    response["Cache-Control"] = "no-store"
    return {
        "id": camera.pk, "lot_id": lot.pk, "name": camera.name,
        "source_uri": camera.source_uri,
        "reference_frame": {"width": camera.frame_width, "height": camera.frame_height},
        "model_version": camera.model_version,
        "slots_version": slots_version(camera),
        "device_token": token,
    }

@lots_router.get("/{lot_id}/stream")
def lot_stream(request, lot_id: UUID):
    """Live slot states as Server-Sent Events. See parking/stream.py for the
    frame sequence. Authenticated by session cookie, because EventSource
    cannot set an Authorization header; SameSite=Lax on the session cookie is
    what stops a cross-origin page opening one."""
    lot = _get_lot(request, lot_id)

    try:
        redis_client().ping()
        token = presence.acquire(request.auth.user.pk)
    except redis.RedisError as exc:
        raise ApiError(503, "bus_unavailable",
                       "Live updates are unavailable. Poll /slots instead.") from exc

    response = StreamingHttpResponse(
        sse_stream(lot, request.auth.user, token),
        content_type="text/event-stream",
    )

    response["Cache-Control"] = "no-cache, no-store"
    response["X-Accel-Buffering"] = "no"    # nginx must not buffer the stream
    return response

@cameras_router.post("/{camera_id}/slots:bulk", response=BulkSlotsOut, auth=owner_only)
def replace_slots(request, camera_id: UUID, payload: BulkSlotsIn):
    """Replaces the camera's slot set in one transaction.

    Slots absent from the payload are deactivated, never deleted: v1 ran
    DELETE on every restart and orphaned its whole event history. A label that
    was removed and is sent again becomes a NEW slot — the deactivated row
    keeps its events rather than having a re-drawn bay's events appended to it.
    """
    org = request.auth.org
    labels = [slot.label for slot in payload.slots]
    repeated = sorted({label for label in labels if labels.count(label) > 1})
    if repeated:
        raise ApiError(422, "duplicate_labels", "Each label may appear once.",
                       {"labels": repeated})

    frame = payload.reference_frame
    canonical = {slot.label: canonical_polygon(slot.polygon, slot.label,
                                               frame.width, frame.height)
                 for slot in payload.slots}
    incoming = {slot.label: slot for slot in payload.slots}
    now = timezone.now()

    with transaction.atomic():
        # The lock makes the version check meaningful: two owners saving at
        # once serialise, and the loser sees 409 instead of overwriting.
        camera = (Camera.objects.for_org(org).select_for_update()
                  .filter(pk=camera_id).first())
        if camera is None:
            raise ApiError(404, "camera_not_found", "No such camera.")

        current = slots_version(camera)
        if payload.slots_version != current:
            raise ApiError(409, "slots_version_conflict",
                           "This camera's slots changed since you loaded them.",
                           {"current_slots_version": current})

        scoped = ParkingSlot.objects.for_org(org)
        existing = {slot.label: slot
                    for slot in scoped.filter(camera=camera, is_active=True)}

        gone = [slot.pk for label, slot in existing.items() if label not in incoming]
        if gone:
            scoped.filter(pk__in=gone).update(is_active=False, updated_at=now)

        updated = 0
        for label, slot in existing.items():
            if label not in incoming:
                continue
            polygon, slot_type = canonical[label], incoming[label].slot_type
            if slot.polygon != polygon or slot.slot_type != slot_type:
                scoped.filter(pk=slot.pk).update(polygon=polygon, slot_type=slot_type,
                                                 updated_at=now)
                updated += 1

        fresh = [ParkingSlot(camera=camera, label=label, polygon=canonical[label],
                             slot_type=incoming[label].slot_type)
                 for label in incoming if label not in existing]
        if fresh:
            scoped.bulk_create(fresh)

        if (camera.frame_width, camera.frame_height) != (frame.width, frame.height):
            (Camera.objects.for_org(org).filter(pk=camera.pk)
             .update(frame_width=frame.width, frame_height=frame.height, updated_at=now))
            camera.frame_width, camera.frame_height = frame.width, frame.height

        stored = list(scoped.filter(camera=camera, is_active=True)
                      .order_by("label").values("id", "label", "slot_type", "polygon"))
        return {
            "camera_id": camera.pk,
            "reference_frame": {"width": camera.frame_width, "height": camera.frame_height},
            "slots_version": slots_version(camera),
            "created": len(fresh), "updated": updated, "deactivated": len(gone),
            "slots": stored,
        }