# server/parking/analytics.py
"""Occupancy rate, dwell time and turnover, derived from OccupancyEvent.

One interval model, folded once. All three figures are readings of the same
intervals, so they cannot disagree about what an occupancy was.

An interval is a pair of consecutive events for one slot, ordered by the
worker's clock, carrying the status observed at its start.

Unobserved time is a recorded CameraOutage: opened by the stale sweep at the
camera's last heartbeat, closed by its next one. Outage windows are subtracted
from intervals, and a dwell sample whose occupancy overlaps one is dropped —
the car that left may not be the car that arrived.

Residual error: a worker restart shorter than CAMERA_STALE_SECONDS never
becomes an outage, so up to that much unobserved time counts as observed. That
is bounded and stated. The alternative — treating every resync as evidence of
an unbounded gap — discarded hours of good observation to hide seconds of bad.

`coverage` reports how much of each bucket survived, so a rate can be read
against the evidence behind it.

Scale: the requested range is loaded into memory and folded once. A 50-slot
lot produces a few hundred events a day, so a month is thousands of rows and
the (slot_id, observed_at DESC) index makes the read cheap. At ten times this
size the same logic belongs in SQL window functions.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, floor
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone

from config.errors import ApiError
from parking.models import CameraOutage, OccupancyEvent, ParkingLot, ParkingSlot, SlotStatus

MAX_BUCKETS = 1000
BUCKET_STEP = {"hour": timedelta(hours=1), "day": timedelta(days=1)}
EVENT_FIELDS = ("slot_id", "status", "observed_at", "previous_status")

Window = tuple[datetime, datetime]


@dataclass(frozen=True)
class Interval:
    slot_id: UUID
    status: str
    start: datetime
    end: datetime


def lot_timezone(lot: ParkingLot) -> ZoneInfo:
    try:
        return ZoneInfo(lot.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApiError(422, "invalid_lot_timezone",
                       "This lot's timezone is not a known IANA name.",
                       {"timezone": lot.timezone}) from exc


def bucket_boundaries(start: datetime, end: datetime, bucket: str,
                      tz: ZoneInfo) -> list[Window]:
    """Aligned to local time: a 'day' is a day where the lot is. Kathmandu is
    +05:45, and UTC-aligned days would be meaningless on the chart."""
    local = start.astimezone(tz)
    if bucket == "hour":
        cursor = local.replace(minute=0, second=0, microsecond=0)
    else:
        cursor = local.replace(hour=0, minute=0, second=0, microsecond=0)

    boundaries: list[Window] = []
    while cursor < end:
        if bucket == "hour":
            following = cursor + BUCKET_STEP["hour"]
        else:
            # Wall-clock arithmetic, so a DST day is 23 or 25 hours. Ambiguous
            # local times resolve to fold=0; Asia/Kathmandu has no DST.
            following = (cursor.replace(tzinfo=None) + BUCKET_STEP["day"]).replace(tzinfo=tz)
        boundaries.append((cursor, following))
        cursor = following
        if len(boundaries) > MAX_BUCKETS:
            raise ApiError(422, "range_too_large",
                           f"That range needs more than {MAX_BUCKETS} buckets.",
                           {"bucket": bucket, "limit": MAX_BUCKETS})
    return boundaries


def _collect_events(lot: ParkingLot, slot_ids: list[UUID],
                    start: datetime, end: datetime) -> dict[UUID, list[tuple]]:
    """Per slot: the last event before the range (to know the status at its
    start), everything inside it, and the first event after it (to close the
    final interval). Three queries, not one per slot."""
    scoped = OccupancyEvent.objects.for_org(lot.org_id).filter(slot_id__in=slot_ids)
    before = (scoped.filter(observed_at__lt=start)
              .order_by("slot_id", "-observed_at").distinct("slot_id")
              .values_list(*EVENT_FIELDS))
    inside = (scoped.filter(observed_at__gte=start, observed_at__lt=end)
              .order_by("slot_id", "observed_at").values_list(*EVENT_FIELDS))
    after = (scoped.filter(observed_at__gte=end)
             .order_by("slot_id", "observed_at").distinct("slot_id")
             .values_list(*EVENT_FIELDS))

    events: dict[UUID, list[tuple]] = {slot_id: [] for slot_id in slot_ids}
    for rows in (before, inside, after):
        for row in rows:
            events[row[0]].append(row)
    return events


def _outage_windows(lot: ParkingLot, start: datetime,
                    horizon: datetime) -> dict[UUID, list[Window]]:
    rows = (CameraOutage.objects.for_org(lot.org_id)
            .filter(camera__lot=lot, started_at__lt=horizon)
            .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=start))
            .order_by("camera_id", "started_at")
            .values_list("camera_id", "started_at", "ended_at"))
    windows: dict[UUID, list[Window]] = defaultdict(list)
    for camera_id, began, ended in rows:
        windows[camera_id].append((began, ended or horizon))
    return windows


def _build_intervals(events: dict[UUID, list[tuple]],
                     start: datetime, horizon: datetime) -> list[Interval]:
    intervals: list[Interval] = []
    for slot_id, rows in events.items():
        for index, (_, status, observed_at, _) in enumerate(rows):
            closes_at = rows[index + 1][2] if index + 1 < len(rows) else horizon
            lower, upper = max(observed_at, start), min(closes_at, horizon)
            if upper > lower:
                intervals.append(Interval(slot_id, status, lower, upper))
    return intervals


def _subtract(span: Window, windows: list[Window]) -> list[Window]:
    """The parts of `span` that no outage covers."""
    pieces = [span]
    for began, ended in windows:
        remaining: list[Window] = []
        for lower, upper in pieces:
            if ended <= lower or began >= upper:
                remaining.append((lower, upper))
                continue
            if lower < began:
                remaining.append((lower, began))
            if ended < upper:
                remaining.append((ended, upper))
        pieces = remaining
    return pieces


def _overlaps(windows: list[Window], begin: datetime, finish: datetime) -> bool:
    return any(began < finish and ended > begin for began, ended in windows)


def _dwell_samples(events: dict[UUID, list[tuple]], camera_of: dict[UUID, UUID],
                   windows: dict[UUID, list[Window]],
                   start: datetime, end: datetime) -> tuple[list[float], int]:
    samples: list[float] = []
    arrivals = 0
    for slot_id, rows in events.items():
        camera_windows = windows.get(camera_of[slot_id], [])
        for index, (_, status, observed_at, previous_status) in enumerate(rows):
            is_arrival = (previous_status == SlotStatus.VACANT
                          and status == SlotStatus.OCCUPIED)
            if not (is_arrival and start <= observed_at < end):
                continue
            arrivals += 1
            for _, later_status, later_at, _ in rows[index + 1:]:
                if later_status == SlotStatus.VACANT:
                    if not _overlaps(camera_windows, observed_at, later_at):
                        samples.append((later_at - observed_at).total_seconds() / 60)
                    break
    return samples, arrivals


def _percentile(ordered: list[float], quantile: float) -> float | None:
    """Linear interpolation between order statistics — the same definition as
    Postgres percentile_cont, so moving this into SQL would not move the
    numbers."""
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    low, high = floor(position), ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def lot_analytics(lot: ParkingLot, start: datetime, end: datetime, bucket: str) -> dict:
    if start >= end:
        raise ApiError(422, "invalid_range", "`from` must be before `to`.")

    tz = lot_timezone(lot)
    boundaries = bucket_boundaries(start, end, bucket, tz)
    horizon = min(end, timezone.now())

    slots = list(ParkingSlot.objects.for_org(lot.org_id)
                 .filter(camera__lot=lot, camera__is_active=True, is_active=True)
                 .values_list("pk", "camera_id"))
    camera_of = dict(slots)
    slot_ids = [slot_id for slot_id, _ in slots]

    occupied_seconds = [0.0] * len(boundaries)
    observed_seconds = [0.0] * len(boundaries)
    bucket_starts = [lower for lower, _ in boundaries]
    dwell: list[float] = []
    arrivals = 0
    observed_total = 0.0

    if slot_ids:
        events = _collect_events(lot, slot_ids, start, end)
        windows = _outage_windows(lot, start, horizon)
        dwell, arrivals = _dwell_samples(events, camera_of, windows, start, end)

        for interval in _build_intervals(events, start, horizon):
            camera_windows = windows.get(camera_of[interval.slot_id], [])
            for lower, upper in _subtract((interval.start, interval.end), camera_windows):
                observed_total += (upper - lower).total_seconds()
                index = max(bisect_right(bucket_starts, lower) - 1, 0)
                while index < len(boundaries) and boundaries[index][0] < upper:
                    piece_lower = max(lower, boundaries[index][0])
                    piece_upper = min(upper, boundaries[index][1])
                    if piece_upper > piece_lower:
                        seconds = (piece_upper - piece_lower).total_seconds()
                        observed_seconds[index] += seconds
                        if interval.status == SlotStatus.OCCUPIED:
                            occupied_seconds[index] += seconds
                    index += 1

    rates = []
    for index, (lower, upper) in enumerate(boundaries):
        watched = observed_seconds[index]
        capacity = len(slot_ids) * (upper - lower).total_seconds()
        rates.append({
            "bucket": lower,
            "rate": _round(occupied_seconds[index] / watched, 4) if watched else None,
            "coverage": _round(min(watched / capacity, 1.0), 4) if capacity else None,
        })

    observed_slot_days = observed_total / 86400
    dwell.sort()
    return {
        "lot_id": lot.pk,
        "timezone": lot.timezone,
        "bucket": bucket,
        "slots_counted": len(slot_ids),
        "observed_slot_days": round(observed_slot_days, 4),
        "occupancy_rate": rates,
        "dwell_minutes": {
            "p50": _round(_percentile(dwell, 0.5), 1),
            "p90": _round(_percentile(dwell, 0.9), 1),
            "mean": _round(sum(dwell) / len(dwell), 1) if dwell else None,
            "samples": len(dwell),
        },
        "turnover_per_slot_per_day": (
            round(arrivals / observed_slot_days, 3) if observed_slot_days > 0 else None
        ),
    }