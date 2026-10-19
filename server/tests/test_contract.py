# server/tests/test_contract.py
"""The four contract tests named in docs/04_API_CONTRACT.md.

They duplicate coverage that exists elsewhere, on purpose. A reviewer asked
"how do you know tenants are isolated?" should be able to open one file and
read the answer, rather than being pointed at six. Each test exercises the
claim through HTTP, the way a client would meet it.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from accounts.models import Role
from parking.models import Camera, CameraOutage, OccupancyEvent, ParkingSlot, SlotState
from parking.sweep import sweep_stale_cameras
from tests.factories import SQUARE, camera_fields, event_payload, worker_for

ALPHA_OWNER = "+9779811111111"
BETA_OWNER = "+9779822222222"


def slot_status(api, lot, label: str) -> str:
    body = api.get(f"/lots/{lot.pk}/slots").json()
    for camera in body["cameras"]:
        for slot in camera["slots"]:
            if slot["label"] == label:
                return slot["status"]
    raise AssertionError(f"slot {label} not in the response")


# ---------------------------------------------------------------------------
# 1. Org A cannot read org B's lots, slots, or events (expect 404)
# ---------------------------------------------------------------------------
def test_contract_1_organizations_cannot_read_each_other(make_tree, as_owner):
    alpha, beta = make_tree("alpha"), make_tree("beta")
    api = as_owner(alpha.org, phone=ALPHA_OWNER)

    listed = api.get("/lots").json()
    assert [lot["name"] for lot in listed] == ["alpha lot"]

    window = ("from=2026-09-10T00:00:00%2B05:45&to=2026-09-11T00:00:00%2B05:45")
    for path in (f"/lots/{beta.lot.pk}/slots",
                 f"/lots/{beta.lot.pk}/analytics?{window}",
                 f"/lots/{beta.lot.pk}/stream"):
        response = api.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] in {"lot_not_found", "not_found"}

    for path, body in ((f"/lots/{beta.lot.pk}/cameras", {}),
                       (f"/cameras/{beta.camera.pk}/token:rotate", None)):
        assert api.post(path, body).status_code in (404, 422)
    assert (Camera.unscoped.get(pk=beta.camera.pk).device_token_hash
            == beta.camera.device_token_hash)


def test_contract_1_events_do_not_cross_organizations(make_tree, as_owner):
    """There is no endpoint that reads events directly; analytics is the read
    path, so that is where the isolation has to hold."""
    alpha, beta = make_tree("alpha"), make_tree("beta")
    worker_for(beta.camera).post("/worker/events", {
        "camera_id": str(beta.camera.pk),
        "events": [event_payload(beta.slot)],
    })
    api = as_owner(alpha.org, phone=ALPHA_OWNER)
    body = api.get(f"/lots/{alpha.lot.pk}/analytics"
                   "?from=2026-09-10T00:00:00%2B05:45"
                   "&to=2026-09-11T00:00:00%2B05:45").json()
    assert body["dwell_minutes"]["samples"] == 0
    assert body["observed_slot_days"] == 0.0


# ---------------------------------------------------------------------------
# 2. Posting the same event id twice results in one row
# ---------------------------------------------------------------------------
def test_contract_2_a_retried_event_is_stored_once(make_tree):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    batch = {"camera_id": str(a.camera.pk), "events": [event_payload(a.slot)]}

    first = worker.post("/worker/events", batch).json()
    second = worker.post("/worker/events", batch).json()

    assert (first["accepted"], first["duplicates"]) == (1, 0)
    assert (second["accepted"], second["duplicates"]) == (0, 1)
    assert OccupancyEvent.unscoped.filter(slot=a.slot).count() == 1


# ---------------------------------------------------------------------------
# 3. A device token for camera X cannot post events for camera Y
# ---------------------------------------------------------------------------
def test_contract_3_a_token_is_bound_to_its_camera(make_tree):
    a = make_tree("alpha")
    other = Camera.objects.for_org(a.org).create(**camera_fields(a.lot, name="other"))
    other_slot = ParkingSlot.objects.for_org(a.org).create(
        camera=other, label="O-01", polygon=SQUARE)
    worker = worker_for(a.camera)

    claimed = worker.post("/worker/events", {
        "camera_id": str(other.pk), "events": [event_payload(other_slot)]})
    assert claimed.status_code == 403
    assert claimed.json()["error"]["code"] == "camera_mismatch"

    # Nor by keeping its own camera_id and naming the other camera's slot.
    smuggled = worker.post("/worker/events", {
        "camera_id": str(a.camera.pk), "events": [event_payload(other_slot)]}).json()
    assert smuggled["accepted"] == 0
    assert smuggled["rejected"][0]["reason"] == "unknown_slot"

    assert not OccupancyEvent.unscoped.exists()
    assert worker.get("/worker/config").status_code in (200, 503)


# ---------------------------------------------------------------------------
# 4. A camera with no heartbeat for 90s has its slots set to UNKNOWN
# ---------------------------------------------------------------------------
def test_contract_4_a_silent_camera_stops_advertising_vacancy(
        make_tree, as_owner, worker_settings, settings):
    settings.CAMERA_STALE_SECONDS = 90
    a = make_tree("alpha")
    api = as_owner(a.org, phone=ALPHA_OWNER)
    worker = worker_for(a.camera)

    worker.post("/worker/heartbeat", {
        "camera_id": str(a.camera.pk), "fps": 2.0, "frames_processed": 10,
        "queue_depth": 0, "model_version": "test-model", "uptime_seconds": 30})
    worker.post("/worker/events", {
        "camera_id": str(a.camera.pk),
        "events": [event_payload(a.slot, previous_status="OCCUPIED", status="VACANT")]})
    assert slot_status(api, a.lot, "A-01") == "VACANT"

    Camera.unscoped.filter(pk=a.camera.pk).update(
        last_seen_at=timezone.now() - timedelta(seconds=91))
    sweep_stale_cameras()

    # v1's failure: a dead camera kept advertising the vacancy it saw last.
    assert slot_status(api, a.lot, "A-01") == "UNKNOWN"
    assert CameraOutage.unscoped.filter(camera=a.camera, ended_at__isnull=True).exists()

    summary = api.get("/lots").json()[0]
    assert (summary["vacant"], summary["unknown"]) == (0, 1)
