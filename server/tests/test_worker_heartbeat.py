# server/tests/test_worker_heartbeat.py
"""POST /worker/heartbeat."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from parking.models import Camera, ParkingSlot, SlotState
from tests.factories import SQUARE, camera_fields, worker_for


def beat(worker, camera, **overrides):
    body = {"camera_id": str(camera.pk), "fps": 1.94, "frames_processed": 3480,
            "queue_depth": 0, "model_version": "test-model", "uptime_seconds": 1740,
            **overrides}
    return worker.post("/worker/heartbeat", body)


def test_heartbeat_records_liveness(make_tree, worker_settings):
    a = make_tree("alpha")
    assert beat(worker_for(a.camera), a.camera).status_code == 200
    camera = Camera.unscoped.get(pk=a.camera.pk)
    assert timezone.now() - camera.last_seen_at < timedelta(seconds=5)
    assert camera.last_fps == Decimal("1.94")


def test_heartbeat_config_version_matches_config(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    assert (beat(worker, a.camera).json()["config_version"]
            == worker.get("/worker/config").json()["config_version"])


def test_resync_lists_active_slots_without_a_current_observation(make_tree, worker_settings):
    a = make_tree("alpha")                                   # A-01: never observed
    scoped = ParkingSlot.objects.for_org(a.org)
    known = scoped.create(camera=a.camera, label="B-01", polygon=SQUARE)
    unknown = scoped.create(camera=a.camera, label="C-01", polygon=SQUARE)
    scoped.create(camera=a.camera, label="D-01", polygon=SQUARE, is_active=False)
    states = SlotState.objects.for_org(a.org)
    states.create(slot=known, status="OCCUPIED", since=timezone.now())
    states.create(slot=unknown, status="UNKNOWN", since=timezone.now())

    body = beat(worker_for(a.camera), a.camera).json()
    assert body["resync_slot_ids"] == [str(a.slot.pk), str(unknown.pk)]


def test_heartbeat_for_another_camera_is_403(make_tree, worker_settings):
    a = make_tree("alpha")
    sibling = Camera.objects.for_org(a.org).create(**camera_fields(a.lot, name="sibling"))
    response = beat(worker_for(a.camera), sibling)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "camera_mismatch"


def test_negative_fps_is_422(make_tree, worker_settings):
    a = make_tree("alpha")
    assert beat(worker_for(a.camera), a.camera, fps=-1).status_code == 422