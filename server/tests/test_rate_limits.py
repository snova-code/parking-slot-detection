# server/tests/test_rate_limits.py
"""Rate limits and the concurrent-stream cap."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from accounts.models import User
from tests.factories import PASSWORD, ApiClient, event_payload, worker_for
from tests.test_stream import open_stream

PHONE = "+9779812345678"


@pytest.fixture(autouse=True)
def _empty_counters(redis_bus):
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tight(monkeypatch):
    """Throttles read their rate at import, so the instances are patched
    directly rather than sending sixty real requests."""
    from config import throttles

    for throttle in (throttles.worker_events_throttle, throttles.login_throttle):
        monkeypatch.setattr(throttle, "num_requests", 2)
    return throttles


def post_event(worker, camera, slot):
    return worker.post("/worker/events", {"camera_id": str(camera.pk),
                                          "events": [event_payload(slot)]})


def test_a_camera_that_floods_is_throttled(make_tree, tight):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    assert post_event(worker, a.camera, a.slot).status_code == 200
    assert post_event(worker, a.camera, a.slot).status_code == 200

    refused = post_event(worker, a.camera, a.slot)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"
    assert refused["Retry-After"]


def test_one_camera_cannot_spend_anothers_allowance(make_tree, tight):
    from parking.models import Camera, ParkingSlot
    from tests.factories import SQUARE, camera_fields

    a = make_tree("alpha")
    sibling = Camera.objects.for_org(a.org).create(**camera_fields(a.lot, name="sibling"))
    sibling_slot = ParkingSlot.objects.for_org(a.org).create(
        camera=sibling, label="S-01", polygon=SQUARE)

    busy = worker_for(a.camera)
    for _ in range(3):
        post_event(busy, a.camera, a.slot)

    quiet = worker_for(sibling)
    assert post_event(quiet, sibling, sibling_slot).status_code == 200


def test_login_attempts_are_throttled_by_address(db, tight):
    User.objects.create_user(PHONE, PASSWORD)
    api = ApiClient()
    for _ in range(2):
        api.post("/auth/login", {"phone_number": PHONE, "password": "wrong-password-x"})

    refused = api.post("/auth/login", {"phone_number": PHONE, "password": PASSWORD})
    assert refused.status_code == 429

@pytest.mark.django_db(transaction=True)
def test_a_sixth_stream_is_refused(make_tree, as_owner, redis_bus, sse_settings, settings):
    settings.SSE_MAX_CONNECTIONS_PER_USER = 2
    a = make_tree("alpha")
    api = as_owner(a.org)
    with open_stream(api, a.lot) as first, open_stream(api, a.lot) as second:
        # The slot is released in the generator's finally, and a generator that
        # was never advanced does not run its body on close. Reading the
        # snapshot starts it, which is also what a real EventSource does.
        next_event(first)
        next_event(second)
        refused = api.get(f"/lots/{a.lot.pk}/stream")
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "too_many_streams"

    assert api.get(f"/lots/{a.lot.pk}/stream").status_code == 200