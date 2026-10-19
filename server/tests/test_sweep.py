# server/tests/test_sweep.py
"""The stale-camera sweep and the outage record it writes."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from parking.models import Camera, CameraOutage, ParkingLot, SlotState
from parking.sweep import sweep_stale_cameras
from tests.factories import worker_for
from tests.test_stream import assert_silent, next_event, open_stream


def seen(camera, seconds_ago: float):
    Camera.unscoped.filter(pk=camera.pk).update(
        last_seen_at=timezone.now() - timedelta(seconds=seconds_ago))


def occupied(org, slot):
    return SlotState.objects.for_org(org).create(
        slot=slot, status="OCCUPIED", since=timezone.now(),
        last_observed_at=timezone.now())


def test_a_silent_camera_loses_its_slots_and_gains_an_outage(make_tree, settings):
    settings.CAMERA_STALE_SECONDS = 90
    a = make_tree("alpha")
    state = occupied(a.org, a.slot)
    seen(a.camera, 200)

    declared = sweep_stale_cameras()
    assert [entry["camera_id"] for entry in declared] == [a.camera.pk]

    state.refresh_from_db()
    assert state.status == "UNKNOWN"
    # The worker-clock anchor survives, or ingest ordering would break.
    assert state.last_observed_at is not None

    record = CameraOutage.unscoped.get(camera=a.camera)
    assert record.ended_at is None
    assert record.started_at == Camera.unscoped.get(pk=a.camera.pk).last_seen_at


def test_sweeping_twice_declares_once(make_tree, settings):
    settings.CAMERA_STALE_SECONDS = 90
    a = make_tree("alpha")
    occupied(a.org, a.slot)
    seen(a.camera, 200)

    sweep_stale_cameras()
    assert sweep_stale_cameras() == []
    assert CameraOutage.unscoped.filter(camera=a.camera).count() == 1


def test_live_recently_seen_and_never_seen_cameras_are_left_alone(make_tree, settings):
    settings.CAMERA_STALE_SECONDS = 90
    a = make_tree("alpha")
    seen(a.camera, 10)
    assert sweep_stale_cameras() == []

    Camera.unscoped.filter(pk=a.camera.pk).update(last_seen_at=None)
    assert sweep_stale_cameras() == []
    assert not CameraOutage.unscoped.exists()


def test_deactivated_cameras_and_lots_are_skipped(make_tree, settings):
    settings.CAMERA_STALE_SECONDS = 90
    a, b = make_tree("alpha"), make_tree("beta")
    seen(a.camera, 200)
    seen(b.camera, 200)
    Camera.unscoped.filter(pk=a.camera.pk).update(is_active=False)
    ParkingLot.unscoped.filter(pk=b.lot.pk).update(is_active=False)

    assert sweep_stale_cameras() == []


def test_a_heartbeat_closes_the_outage(make_tree, settings, worker_settings):
    settings.CAMERA_STALE_SECONDS = 90
    a = make_tree("alpha")
    occupied(a.org, a.slot)
    seen(a.camera, 200)
    sweep_stale_cameras()

    worker = worker_for(a.camera)
    body = worker.post("/worker/heartbeat", {
        "camera_id": str(a.camera.pk), "fps": 2.0, "frames_processed": 1,
        "queue_depth": 0, "model_version": "test-model", "uptime_seconds": 5,
    }).json()

    record = CameraOutage.unscoped.get(camera=a.camera)
    assert record.ended_at is not None
    # The slots are UNKNOWN, so the worker is told to re-announce them.
    assert body["resync_slot_ids"] == [str(a.slot.pk)]


def test_going_stale_reaches_an_open_stream(make_tree, as_owner, redis_bus,
                                            sse_settings, settings,
                                            django_capture_on_commit_callbacks):
    settings.CAMERA_STALE_SECONDS = 90
    a = make_tree("alpha")
    occupied(a.org, a.slot)
    seen(a.camera, 200)

    with open_stream(as_owner(a.org), a.lot) as frames:
        assert next_event(frames)[0] == "snapshot"
        with django_capture_on_commit_callbacks(execute=True):
            sweep_stale_cameras()
        event, data = next_event(frames, skip=("heartbeat",))

    assert event == "slot_update"
    assert data["slots"][0]["status"] == "UNKNOWN"