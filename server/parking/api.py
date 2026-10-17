# server/parking/api.py
"""Endpoints for humans. Auth is inherited from the API default (a member of
the active organization); only the owner-restricted route says otherwise.
OPERATOR is read-only, which is exactly what these reads are.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from django.db.models import Count, Max, Q
from django.http import HttpResponse
from ninja import Query, Router, Schema
from pydantic import AwareDatetime, Field

from accounts.security import owner_only
from config.errors import ApiError
from parking.analytics import lot_analytics
from parking.models import Camera, ParkingLot, ParkingSlot, SlotStatus
from parking.schemas import ReferenceFrameOut
from parking.tokens import generate_device_token

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
            }
            for camera in cameras
        ],
    }


@lots_router.get("/{lot_id}/analytics", response=AnalyticsOut)
def analytics(request, lot_id: UUID, params: Query[AnalyticsQuery]):
    lot = _get_lot(request, lot_id)
    return lot_analytics(lot, params.start, params.end, params.bucket)