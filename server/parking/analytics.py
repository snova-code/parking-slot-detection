# server/parking/analytics.py
"""Occupancy rate, dwell time and turnover, derived from OccupancyEvent.

One interval model, folded once. All three figures are readings of the same
intervals, so they cannot disagree about what an occupancy was.

An interval is a pair of consecutive events for one slot, ordered by the
worker's clock, carrying the status observed at its start.

Observed and unobserved. An interval is unobserved when the event that closes
it claims previous_status UNKNOWN. The worker only claims that after a resync
or a restart, and the server only asks for a resync when it had lost the
camera — so the claim marks an interval containing a gap of unknown length.
Unobserved time is excluded from both sides of every ratio; `coverage` reports
how much of each bucket survived.

This is conservative: a worker restart after three quiet hours discards three
hours. Recording camera outages explicitly (Task 2.6) would let this subtract
only the outage window. Until then the number is honest but pessimistic, and
coverage shows it.

Dwell and turnover count an occupancy only from an event claiming a VACANT
predecessor — a real arrival. Re-announcements claim UNKNOWN, so they never
inflate either. A dwell sample is dropped when a resync falls between arrival
and departure: the car may not be the same car.

Scale: the requested range is loaded into memory. A 50-slot lot produces a few
hundred events a day, so a month is thousands of rows and the
(slot_id, observed_at DESC) index makes the read cheap. At ten times this size
the same interval logic belongs in SQL window functions. That is a limitation
of this implementation, not of the schema.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, floor
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from config.errors import ApiError
from parking.models import OccupancyEvent, ParkingLot, ParkingSlot, SlotState, SlotStatus

MAX_BUCKETS = 1000
BUCKET_STEP = {"hour": timedelta(hours=1), "day": timedelta(days=1)}

# (slot_id, status, observed_at, previous_status)
EVENT_FIELDS = ("slot_id", "status", "observed_at", "previous_status")


@dataclass(frozen=True)
class Interval:
    slot_id: UUID
    status: str
    start: datetime
    end: datetime
    observed: bool

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def lot_timezone(lot: ParkingLot) -> ZoneInfo:
    try:
        return ZoneInfo(lot.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApiError(422, "invalid_lot_timezone",
                       "This lot's timezone is not a known IANA name.",
                       {"timezone": lot.timezone}) from exc


def bucket_boundaries(start: datetime, end: datetime, bucket: str,
                      tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Buckets are aligned to local time, so a 'day' is a day where the lot
    is, not a day in UTC. Kathmandu is +05:45; UTC-aligned days would be
    meaningless to anyone reading the chart."""
    local = start.astimezone(tz)
    if bucket == "hour":
        cursor = local.replace(minute=0, second=0, microsecond=0)
    else:
        cursor = local.replace(hour=0, minute=0, second=0, microsecond=0)

    boundaries: list[tuple[datetime, datetime]] = []
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
    final interval honestly). Three queries, not one per slot."""
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


def _build_intervals(events: dict[UUID, list[tuple]], unknown_now: set[UUID],
                     start: datetime, horizon: datetime) -> list[Interval]:
    intervals: list[Interval] = []
    for slot_id, rows in events.items():
        for index, (_, status, observed_at, _) in enumerate(rows):
            if index + 1 < len(rows):
                closes_at = rows[index + 1][2]
                observed = rows[index + 1][3] != SlotStatus.UNKNOWN
            else:
                # Nothing after this event: it still stands, unless the server
                # has since given up on the slot.
                closes_at = horizon
                observed = slot_id not in unknown_now
            lower, upper = max(observed_at, start), min(closes_at, horizon)
            if upper > lower:
                intervals.append(Interval(slot_id, status, lower, upper, observed))
    return intervals


def _dwell_samples(events: dict[UUID, list[tuple]],
                   start: datetime, end: datetime) -> tuple[list[float], int]:
    samples: list[float] = []
    arrivals = 0
    for rows in events.values():
        for index, (_, status, observed_at, previous_status) in enumerate(rows):
            is_arrival = (previous_status == SlotStatus.VACANT
                          and status == SlotStatus.OCCUPIED)
            if not (is_arrival and start <= observed_at < end):
                continue
            arrivals += 1
            interrupted = False
            for _, later_status, later_at, later_previous in rows[index + 1:]:
                if later_previous == SlotStatus.UNKNOWN:
                    interrupted = True
                if later_status == SlotStatus.VACANT:
                    if not interrupted:
                        samples.append((later_at - observed_at).total_seconds() / 60)
                    break
    return samples, arrivals


def _percentile(ordered: list[float], quantile: float) -> float | None:
    """Linear interpolation between order statistics — the same definition as
    Postgres percentile_cont, so moving this into SQL later would not move the
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

    slot_ids = list(
        ParkingSlot.objects.for_org(lot.org_id)
        .filter(camera__lot=lot, camera__is_active=True, is_active=True)
        .values_list("pk", flat=True)
    )

    occupied_seconds = [0.0] * len(boundaries)
    observed_seconds = [0.0] * len(boundaries)
    dwell: list[float] = []
    arrivals = 0
    observed_total = 0.0

    if slot_ids:
        events = _collect_events(lot, slot_ids, start, end)
        unknown_now = set(
            SlotState.objects.for_org(lot.org_id)
            .filter(slot_id__in=slot_ids, status=SlotStatus.UNKNOWN)
            .values_list("slot_id", flat=True)
        )
        intervals = _build_intervals(events, unknown_now, start, horizon)
        dwell, arrivals = _dwell_samples(events, start, end)

        bucket_starts = [lower for lower, _ in boundaries]
        for interval in intervals:
            if not interval.observed:
                continue
            observed_total += interval.seconds
            index = max(bisect_right(bucket_starts, interval.start) - 1, 0)
            while index < len(boundaries) and boundaries[index][0] < interval.end:
                lower = max(interval.start, boundaries[index][0])
                upper = min(interval.end, boundaries[index][1])
                if upper > lower:
                    seconds = (upper - lower).total_seconds()
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