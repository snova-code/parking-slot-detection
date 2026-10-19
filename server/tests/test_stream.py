# server/tests/test_stream.py
"""GET /lots/{id}/stream — Server-Sent Events over Redis pub/sub."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

import pytest

from accounts.models import Membership, Role
from parking.models import Camera, OccupancyEvent, ParkingLot, ParkingSlot
from tests.factories import SQUARE, camera_fields, event_payload, worker_for

OPERATOR = "+9779822222222"

@contextmanager
def open_stream(api, lot):
    """StreamingHttpResponse.streaming_content is a map() over the underlying
    generator, so it has no close(); closing the response closes the generator
    and releases the Redis subscription."""
    response = api.get(f"/lots/{lot.pk}/stream")
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "text/event-stream"
    try:
        yield response.streaming_content
    finally:
        response.close()


def next_event(frames, skip=(), timeout=10.0):
    deadline = time.monotonic() + timeout
    for chunk in frames:
        lines = chunk.decode().strip().split("\n")
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        if event not in skip:
            return event, data
        if time.monotonic() > deadline:
            break
    raise AssertionError("no matching SSE frame arrived")


def assert_silent(frames, seconds=2.0):
    deadline = time.monotonic() + seconds
    for chunk in frames:
        event = chunk.decode().split("\n", 1)[0].removeprefix("event: ")
        assert event == "heartbeat", f"unexpected {event} frame"
        if time.monotonic() > deadline:
            return


def send_occupied(worker, camera, slot, capture):
    with capture(execute=True):
        response = worker.post("/worker/events", {
            "camera_id": str(camera.pk),
            "events": [event_payload(slot, previous_status="VACANT", status="OCCUPIED")],
        })
    assert response.status_code == 200, response.content


def test_stream_opens_with_a_snapshot(make_tree, as_owner, redis_bus, sse_settings):
    a = make_tree("alpha")
    with open_stream(as_owner(a.org), a.lot) as frames:
        event, data = next_event(frames)
    assert event == "snapshot"
    assert data["slots"] == [{"slot_id": str(a.slot.pk), "status": "UNKNOWN",
                              "confidence": None, "since": None, "at": None}]


def test_a_committed_batch_arrives_as_one_update(
        make_tree, as_owner, redis_bus, sse_settings,
        django_capture_on_commit_callbacks):
    a = make_tree("alpha")
    with open_stream(as_owner(a.org), a.lot) as frames:
        assert next_event(frames)[0] == "snapshot"
        send_occupied(worker_for(a.camera), a.camera, a.slot,
                      django_capture_on_commit_callbacks)

        event, data = next_event(frames, skip=("heartbeat",))
    assert event == "slot_update"
    assert data["camera_id"] == str(a.camera.pk)
    assert [s["slot_id"] for s in data["slots"]] == [str(a.slot.pk)]
    assert data["slots"][0]["status"] == "OCCUPIED"


@pytest.mark.django_db(transaction=True)
def test_updates_stay_on_their_own_lot(make_tree, as_owner, redis_bus, sse_settings,
                                       django_capture_on_commit_callbacks):
    a = make_tree("alpha")
    other_lot = ParkingLot.objects.for_org(a.org).create(name="second")
    other_camera = Camera.objects.for_org(a.org).create(
        **camera_fields(other_lot, name="second-cam"))
    other_slot = ParkingSlot.objects.for_org(a.org).create(
        camera=other_camera, label="B-01", polygon=SQUARE)

    with open_stream(as_owner(a.org), a.lot) as frames:
        assert next_event(frames)[0] == "snapshot"
        send_occupied(worker_for(other_camera), other_camera, other_slot,
                      django_capture_on_commit_callbacks)
        assert_silent(frames)


@pytest.mark.django_db(transaction=True)
def test_heartbeats_keep_the_stream_open(make_tree, as_owner, redis_bus, sse_settings):
    a = make_tree("alpha")
    with open_stream(as_owner(a.org), a.lot) as frames:
        assert next_event(frames)[0] == "snapshot"
        event, data = next_event(frames)
        assert event == "heartbeat"
        assert "at" in data


@pytest.mark.django_db(transaction=True)
def test_stream_closes_when_membership_is_revoked(make_tree, as_owner, redis_bus,
                                                  sse_settings):
    a = make_tree("alpha")
    api = as_owner(a.org)
    with open_stream(api, a.lot) as frames:
        assert next_event(frames)[0] == "snapshot"
        Membership.objects.for_org(a.org).all().delete()
        event, data = next_event(frames)
    assert (event, data["reason"]) == ("closing", "access_revoked")


def test_operators_may_watch(make_tree, as_owner, redis_bus, sse_settings):
    a = make_tree("alpha")
    api = as_owner(a.org, phone=OPERATOR, role=Role.OPERATOR)
    with open_stream(api, a.lot) as frames:
        assert next_event(frames)[0] == "snapshot"


def test_another_orgs_lot_cannot_be_watched(make_tree, as_owner, redis_bus, sse_settings):
    a, b = make_tree("alpha"), make_tree("beta")
    response = as_owner(a.org).get(f"/lots/{b.lot.pk}/stream")
    assert response.status_code == 404


# ------------------------------------------------------ the bus is down ----
@pytest.fixture
def dead_bus(settings):
    from parking import pubsub

    pubsub.client.cache_clear()
    settings.REDIS_URL = "redis://127.0.0.1:6399/0"    # nothing listens here
    yield
    pubsub.client.cache_clear()


def test_ingestion_survives_a_dead_bus(make_tree, dead_bus,
                                       django_capture_on_commit_callbacks):
    a = make_tree("alpha")
    send_occupied(worker_for(a.camera), a.camera, a.slot,
                  django_capture_on_commit_callbacks)
    assert OccupancyEvent.unscoped.filter(slot=a.slot).count() == 1


def test_stream_refuses_cleanly_when_the_bus_is_down(make_tree, as_owner, dead_bus):
    a = make_tree("alpha")
    response = as_owner(a.org).get(f"/lots/{a.lot.pk}/stream")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "bus_unavailable"