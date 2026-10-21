# server/tests/test_live_view_contract.py
"""What the live page depends on, asserted server-side.

The page itself is verified by hand — there is no headless browser in this
project. What can be pinned down cheaply is the shape of the data it consumes
and the coordinate space it assumes, so a change there fails here rather than
in a browser nobody is watching.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings

from parking.models import ParkingSlot, SlotState
from tests.factories import SQUARE, event_payload, worker_for
from tests.test_stream import next_event, open_stream


def test_slots_response_carries_everything_the_overlay_needs(make_tree, as_owner):
    a = make_tree("alpha")
    body = as_owner(a.org).get(f"/lots/{a.lot.pk}/slots").json()
    camera = body["cameras"][0]
    assert set(camera) >= {"id", "name", "reference_frame", "slots_version",
                           "last_seen_at", "slots"}
    slot = camera["slots"][0]
    assert set(slot) >= {"id", "label", "slot_type", "polygon", "status"}
    assert len(slot["polygon"]) == 4


def test_polygons_are_in_the_cameras_own_reference_frame(make_tree, as_owner):
    """The overlay's viewBox is the camera's reference frame, so a polygon
    must never exceed it."""
    a = make_tree("alpha")
    camera = as_owner(a.org).get(f"/lots/{a.lot.pk}/slots").json()["cameras"][0]
    frame = camera["reference_frame"]
    for slot in camera["slots"]:
        for x, y in slot["polygon"]:
            assert 0 <= x <= frame["width"]
            assert 0 <= y <= frame["height"]


def test_snapshot_covers_every_slot_the_overlay_drew(make_tree, as_owner,
                                                     redis_bus, sse_settings):
    """Geometry comes from /slots and statuses from the snapshot; a slot in
    one and not the other would render as permanently unknown."""
    a = make_tree("alpha")
    ParkingSlot.objects.for_org(a.org).create(
        camera=a.camera, label="A-02", polygon=SQUARE)
    api = as_owner(a.org)

    drawn = {slot["id"] for camera in api.get(f"/lots/{a.lot.pk}/slots").json()["cameras"]
             for slot in camera["slots"]}
    with open_stream(api, a.lot) as frames:
        event, data = next_event(frames)
    assert event == "snapshot"
    assert {slot["slot_id"] for slot in data["slots"]} == drawn


def test_update_frames_use_the_same_keys_as_the_snapshot(
        make_tree, as_owner, redis_bus, sse_settings,
        django_capture_on_commit_callbacks):
    a = make_tree("alpha")
    with open_stream(as_owner(a.org), a.lot) as frames:
        snapshot = next_event(frames)[1]
        with django_capture_on_commit_callbacks(execute=True):
            worker_for(a.camera).post("/worker/events", {
                "camera_id": str(a.camera.pk),
                "events": [event_payload(a.slot)]})
        update = next_event(frames, skip=("heartbeat",))[1]

    assert set(update["slots"][0]) == set(snapshot["slots"][0])


def test_unobserved_slots_report_unknown_not_a_stale_reading(make_tree, as_owner):
    """The status the page must render differently from a real reading."""
    a = make_tree("alpha")
    from django.utils import timezone

    SlotState.objects.for_org(a.org).create(
        slot=a.slot, status="UNKNOWN", since=timezone.now())
    camera = as_owner(a.org).get(f"/lots/{a.lot.pk}/slots").json()["cameras"][0]
    assert camera["slots"][0]["status"] == "UNKNOWN"


def test_the_pages_share_one_scaling_module():
    """Both pages must import scalePolygon rather than each doing the maths —
    the v1 bug was exactly two code paths disagreeing about scale."""
    static_root = Path(settings.STATICFILES_DIRS[0])
    for name in ("annotate.js", "lot.js"):
        source = (static_root / name).read_text(encoding="utf-8")
        assert "scalePolygon" in source, name
        assert "polygons.js" in source, name