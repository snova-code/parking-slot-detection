# server/tests/test_analytics.py
"""GET /lots/{id}/analytics: interval semantics, coverage, dwell, turnover."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from accounts.models import Role
from parking.models import ParkingSlot, SlotState
from tests.factories import SQUARE, ApiClient, make_event
from urllib.parse import urlencode


KTM = ZoneInfo("Asia/Kathmandu")
OWNER = "+9779811111111"


def ktm(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=KTM)


@pytest.fixture
def api(make_tree, make_member):
    def build(tree):
        make_member(tree.org, Role.OWNER, OWNER)
        client = ApiClient()
        client.login(OWNER)
        return client

    return build


def fetch(client, lot, start, end, bucket="hour"):
    """Query params are urlencoded: isoformat() emits '+05:45' for Kathmandu,
    and a raw '+' in a query string decodes as a space."""
    query = urlencode({"from": start.isoformat(), "to": end.isoformat(),
                       "bucket": bucket})
    response = client.get(f"/lots/{lot.pk}/analytics?{query}")
    assert response.status_code == 200, response.content
    return response.json()


def test_occupancy_rate_is_time_weighted(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "OCCUPIED", "VACANT", ktm(10, 8))
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    make_event(a.slot, a.camera, "OCCUPIED", "VACANT", ktm(10, 10, 30))

    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 12))
    assert [b["rate"] for b in body["occupancy_rate"]] == [0.0, 0.5, 0.0]
    assert [b["coverage"] for b in body["occupancy_rate"]] == [1.0, 1.0, 1.0]


def test_time_before_the_first_event_is_not_covered(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))

    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 11))
    first, second = body["occupancy_rate"]
    assert (first["rate"], first["coverage"]) == (None, 0.0)
    assert (second["rate"], second["coverage"]) == (1.0, 1.0)


def test_interval_closed_by_a_resync_is_excluded(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    make_event(a.slot, a.camera, "UNKNOWN", "VACANT", ktm(10, 11, 30))

    body = fetch(api(a), a.lot, ktm(10, 10), ktm(10, 12))
    ten, eleven = body["occupancy_rate"]
    assert (ten["rate"], ten["coverage"]) == (None, 0.0)
    assert (eleven["rate"], eleven["coverage"]) == (0.0, 0.5)


def test_dwell_uses_arrivals_and_drops_interrupted_occupancies(make_tree, api):
    a = make_tree("alpha")
    second = ParkingSlot.objects.for_org(a.org).create(
        camera=a.camera, label="A-02", polygon=SQUARE)

    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    make_event(a.slot, a.camera, "OCCUPIED", "VACANT", ktm(10, 10, 45))
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 11))
    make_event(a.slot, a.camera, "OCCUPIED", "VACANT", ktm(10, 12, 30))
    # Interrupted by a resync: arrival known, departure not the same car.
    make_event(second, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    make_event(second, a.camera, "UNKNOWN", "OCCUPIED", ktm(10, 11))
    make_event(second, a.camera, "OCCUPIED", "VACANT", ktm(10, 13))

    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 14))
    assert body["dwell_minutes"] == {"p50": 67.5, "p90": 85.5, "mean": 67.5, "samples": 2}


def test_open_occupancy_is_not_a_dwell_sample(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 11))
    assert body["dwell_minutes"]["samples"] == 0


def test_reannouncement_is_not_an_arrival(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    make_event(a.slot, a.camera, "UNKNOWN", "OCCUPIED", ktm(10, 11))
    make_event(a.slot, a.camera, "OCCUPIED", "VACANT", ktm(10, 12))

    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 13))
    assert body["dwell_minutes"]["samples"] == 0          # interrupted
    # One arrival, not two, and it is divided by observed time only.
    expected = 1 / body["observed_slot_days"]
    assert body["turnover_per_slot_per_day"] == pytest.approx(round(expected, 3))


def test_day_buckets_align_to_the_lots_local_midnight(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    body = fetch(api(a), a.lot, ktm(10, 0), ktm(12, 0), bucket="day")
    assert [b["bucket"] for b in body["occupancy_rate"]] == [
        ktm(10, 0).isoformat(), ktm(11, 0).isoformat()]


def test_slot_the_server_has_given_up_on_is_uncovered(make_tree, api):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    SlotState.objects.for_org(a.org).create(
        slot=a.slot, status="UNKNOWN", since=timezone.now())

    body = fetch(api(a), a.lot, ktm(10, 10), ktm(10, 12))
    assert [b["coverage"] for b in body["occupancy_rate"]] == [0.0, 0.0]


def test_events_from_another_org_never_appear(make_tree, api):
    a, b = make_tree("alpha"), make_tree("beta")
    make_event(b.slot, b.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    body = fetch(api(a), a.lot, ktm(10, 9), ktm(10, 12))
    assert body["dwell_minutes"]["samples"] == 0
    assert body["observed_slot_days"] == 0.0


def test_ranges_are_validated(make_tree, api):
    a = make_tree("alpha")
    client = api(a)
    backwards = client.get(
        f"/lots/{a.lot.pk}/analytics?from={ktm(11, 0).isoformat()}"
        f"&to={ktm(10, 0).isoformat()}")
    assert backwards.status_code == 422
    assert backwards.json()["error"]["code"] == "invalid_range"

    huge = client.get(
        f"/lots/{a.lot.pk}/analytics?from={ktm(10, 0).isoformat()}"
        f"&to={(ktm(10, 0) + timedelta(days=60)).isoformat()}&bucket=hour")
    assert huge.status_code == 422
    assert huge.json()["error"]["code"] == "range_too_large"