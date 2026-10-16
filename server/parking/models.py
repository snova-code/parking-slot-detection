# server/parking/models.py
"""Parking domain. Every model here is tenant-owned (tenancy/scoping.py).

Constraints Django can express live in Meta beside the fields they govern.
Composite tenant-consistency FKs, the polygon shape check and the append-only
trigger are in migration 0002, because the ORM cannot express them.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import F, Q

from tenancy.scoping import TenantModel


class SlotStatus(models.TextChoices):
    VACANT = "VACANT"
    OCCUPIED = "OCCUPIED"
    UNKNOWN = "UNKNOWN"


class ObservedStatus(models.TextChoices):
    """What a camera can report. UNKNOWN is a server judgement about a stale
    camera, never an observation, so an event can never carry it as `status`."""
    VACANT = "VACANT"
    OCCUPIED = "OCCUPIED"


class SlotType(models.TextChoices):
    STANDARD = "STANDARD"
    ACCESSIBLE = "ACCESSIBLE"
    EV = "EV"


def _unit_interval(field: str, nullable: bool = False) -> Q:
    bounded = Q(**{f"{field}__gte": 0, f"{field}__lte": 1})
    return Q(**{f"{field}__isnull": True}) | bounded if nullable else bounded


class ParkingLot(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    street = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=255, blank=True, default="")
    district = models.CharField(max_length=255, blank=True, default="")
    country = models.CharField(max_length=255, blank=True, default="")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    timezone = models.CharField(max_length=64, default="Asia/Kathmandu")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "parking_lot"
        constraints = [
            models.UniqueConstraint(fields=["org", "name"],
                                    name="parking_lot_unique_name_per_org"),
            # Redundant as uniqueness (id is the PK). It exists because a
            # composite FK can only reference a unique constraint.
            models.UniqueConstraint(fields=["id", "org"], name="parking_lot_id_org"),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name="parking_lot_coordinates_paired"),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True)
                | Q(latitude__gte=-90, latitude__lte=90),
                name="parking_lot_latitude_range"),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True)
                | Q(longitude__gte=-180, longitude__lte=180),
                name="parking_lot_longitude_range"),
        ]

    def __str__(self) -> str:
        return self.name


class Camera(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lot = models.ForeignKey(ParkingLot, on_delete=models.CASCADE, related_name="cameras")
    name = models.CharField(max_length=255)
    source_uri = models.TextField()
    # SHA-256 hex of the device token. Tokens are 40 random characters, so a
    # fast hash is correct and allows lookup by hash; slow password hashes are
    # for low-entropy secrets. Unique across all orgs because it IS the lookup.
    device_token_hash = models.CharField(max_length=128, unique=True)
    frame_width = models.IntegerField()
    frame_height = models.IntegerField()
    model_version = models.CharField(max_length=64)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_fps = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "camera"
        constraints = [
            models.UniqueConstraint(fields=["id", "org"], name="camera_id_org"),
            models.CheckConstraint(condition=Q(frame_width__gt=0, frame_height__gt=0),
                                   name="camera_frame_positive"),
            models.CheckConstraint(condition=Q(last_fps__isnull=True) | Q(last_fps__gte=0),
                                   name="camera_fps_non_negative"),
        ]
        indexes = [
            models.Index(fields=["org", "last_seen_at"], name="camera_org_last_seen"),
        ]

    def __str__(self) -> str:
        return self.name


class ParkingSlot(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="slots")
    label = models.CharField(max_length=32)
    # [[x, y] x 4], clockwise from top-left, in the camera's reference-frame
    # pixels. Shape is enforced in migration 0002; geometry by the API.
    polygon = models.JSONField()
    slot_type = models.CharField(max_length=16, choices=SlotType, default=SlotType.STANDARD)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "parking_slot"
        constraints = [
            # Partial: slots are soft-deactivated, never deleted, so an inactive
            # "A-01" must not block a new one. Also serves the active-slots-by-
            # camera lookup the data model asked a separate index for.
            models.UniqueConstraint(fields=["camera", "label"],
                                    condition=Q(is_active=True),
                                    name="parking_slot_unique_active_label"),
            models.UniqueConstraint(fields=["id", "org"], name="parking_slot_id_org"),
            models.UniqueConstraint(fields=["id", "camera"], name="parking_slot_id_camera"),
            models.CheckConstraint(condition=Q(slot_type__in=SlotType.values),
                                   name="parking_slot_type_valid"),
        ]

    def __str__(self) -> str:
        return self.label


class SlotState(TenantModel):
    """Denormalised current status: the live view is one indexed read instead
    of a window function over the event table."""

    slot = models.OneToOneField(ParkingSlot, on_delete=models.CASCADE,
                                primary_key=True, related_name="state")
    status = models.CharField(max_length=16, choices=SlotStatus)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    since = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "slot_state"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=SlotStatus.values),
                                   name="slot_state_status_valid"),
            models.CheckConstraint(condition=_unit_interval("confidence", nullable=True),
                                   name="slot_state_confidence_range"),
        ]
        indexes = [
            models.Index(fields=["org", "status"], name="slot_state_org_status"),
        ]


class OccupancyEvent(TenantModel):
    """Append-only source of truth. UPDATE is rejected by a trigger; DELETE is
    allowed only so that org, camera and slot deletion can cascade."""

    # No default: the worker generates this UUID, and uniqueness on it is what
    # makes a retried POST a no-op.
    id = models.UUIDField(primary_key=True, editable=False)
    # The (org, -observed_at) index below serves org lookups. A second,
    # single-column index would only slow inserts on the busiest table.
    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE,
                            db_index=False)
    # Covered by the (slot, -observed_at) index for the same reason.
    slot = models.ForeignKey(ParkingSlot, on_delete=models.CASCADE,
                             related_name="events", db_index=False)
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="events")
    previous_status = models.CharField(max_length=16, choices=SlotStatus)
    status = models.CharField(max_length=16, choices=ObservedStatus)
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    observed_at = models.DateTimeField()     # worker clock
    received_at = models.DateTimeField(auto_now_add=True)   # server clock
    model_version = models.CharField(max_length=64)

    class Meta:
        db_table = "occupancy_event"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=ObservedStatus.values),
                                   name="event_status_observable"),
            models.CheckConstraint(condition=Q(previous_status__in=SlotStatus.values),
                                   name="event_previous_status_valid"),
            # The worker emits on transition only. An event that changes
            # nothing is a worker bug and is refused at the door.
            models.CheckConstraint(condition=~Q(status=F("previous_status")),
                                   name="event_is_a_transition"),
            models.CheckConstraint(condition=_unit_interval("confidence"),
                                   name="event_confidence_range"),
        ]
        indexes = [
            models.Index(fields=["slot", "-observed_at"], name="event_slot_observed"),
            models.Index(fields=["org", "-observed_at"], name="event_org_observed"),
        ]