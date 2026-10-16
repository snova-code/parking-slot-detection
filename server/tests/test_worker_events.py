# server/tests/test_worker_events.py
"""POST /worker/events: idempotency, rejection, and state derivation."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from parking.models import Camera, OccupancyEvent, ParkingSlot, SlotState
from tests.factories import SQUARE, camera_fields, event_payload, worker_for

BASE = timezone.now() - timedelta(hours=1)


def at(minutes: int):
    return BASE + timedelta(minutes=minutes)


def post(worker, camera, events):
    return worker.post("/worker/events", {"camera_id": str(camera.pk), "events": events})


def state_of(slot) -> SlotState | None:
    return SlotState.unscoped.filter(slot=slot).first()


def transition(slot, previous, status, minutes, **extra) -> dict:
    return event_payload(slot, previous_status=previous, status=status,
                         observed_at=at(minutes).isoformat(), **extra)


# ------------------------------------------------------------ happy path ----
def test_event_is_stored_and_drives_state(make_tree, worker_settings):
    a = make_tree("alpha")
    response = post(worker_for(a.camera), a.camera,
                    [transition(a.slot, "VACANT", "OCCUPIED", 10)])
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0, "rejected": [],
                               "previous_status_mismatches": 0}
    state = state_of(a.slot)
    assert (state.status, state.since, state.last_observed_at) == ("OCCUPIED", at(10), at(10))
    event = OccupancyEvent.unscoped.get(slot=a.slot)
    assert event.received_at is not None and event.camera_id == a.camera.pk


def test_bearer_posts_pass_django_csrf(make_tree, worker_settings):
    """WorkerClient runs with enforce_csrf_checks=True."""
    a = make_tree("alpha")
    assert post(worker_for(a.camera), a.camera, [event_payload(a.slot)]).status_code == 200


# ------------------------------------------------------------ idempotency ----
def test_retrying_a_batch_changes_nothing(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    batch = [transition(a.slot, "VACANT", "OCCUPIED", 10)]
    post(worker, a.camera, batch)
    before = state_of(a.slot).updated_at

    retry = post(worker, a.camera, batch).json()
    assert (retry["accepted"], retry["duplicates"]) == (0, 1)
    assert OccupancyEvent.unscoped.filter(slot=a.slot).count() == 1
    assert state_of(a.slot).updated_at == before


def test_repeated_id_within_a_batch_counts_once(make_tree, worker_settings):
    a = make_tree("alpha")
    event = event_payload(a.slot)
    body = post(worker_for(a.camera), a.camera, [event, event]).json()
    assert (body["accepted"], body["duplicates"]) == (1, 1)


# --------------------------------------------------------------- rejection ----
def test_token_for_one_camera_cannot_post_for_another(make_tree, worker_settings):
    """Contract test #3."""
    a = make_tree("alpha")
    sibling = Camera.objects.for_org(a.org).create(**camera_fields(a.lot, name="sibling"))
    response = post(worker_for(a.camera), sibling, [event_payload(a.slot)])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "camera_mismatch"
    assert not OccupancyEvent.unscoped.exists()


def test_slots_outside_this_camera_are_rejected_identically(make_tree, worker_settings):
    a, b = make_tree("alpha"), make_tree("beta")
    sibling = Camera.objects.for_org(a.org).create(**camera_fields(a.lot, name="sibling"))
    sibling_slot = ParkingSlot.objects.for_org(a.org).create(
        camera=sibling, label="S-01", polygon=SQUARE)
    events = [event_payload(sibling_slot), event_payload(b.slot),
              event_payload(a.slot, slot_id="00000000-0000-4000-8000-000000000000")]

    body = post(worker_for(a.camera), a.camera, events).json()
    assert body["accepted"] == 0
    assert [r["reason"] for r in body["rejected"]] == ["unknown_slot"] * 3


def test_deactivated_slot_is_rejected(make_tree, worker_settings):
    a = make_tree("alpha")
    ParkingSlot.objects.for_org(a.org).filter(pk=a.slot.pk).update(is_active=False)
    body = post(worker_for(a.camera), a.camera, [event_payload(a.slot)]).json()
    assert body["rejected"][0]["reason"] == "slot_inactive"


def test_timestamps_beyond_clock_skew_are_rejected(make_tree, worker_settings):
    a = make_tree("alpha")
    soon = (timezone.now() + timedelta(minutes=1)).isoformat()
    later = (timezone.now() + timedelta(minutes=10)).isoformat()
    body = post(worker_for(a.camera), a.camera, [
        event_payload(a.slot, observed_at=soon),
        event_payload(a.slot, observed_at=later,
                      previous_status="OCCUPIED", status="VACANT"),
    ]).json()
    assert body["accepted"] == 1
    assert body["rejected"][0]["reason"] == "observed_in_future"


def test_oversized_batch_is_413(make_tree, worker_settings):
    a = make_tree("alpha")
    response = post(worker_for(a.camera), a.camera,
                    [event_payload(a.slot) for _ in range(501)])
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "batch_too_large"


@pytest.mark.parametrize("overrides", [
    {"status": "UNKNOWN"},
    {"previous_status": "OCCUPIED", "status": "OCCUPIED"},
    {"observed_at": "2026-01-01T10:00:00"},
    {"confidence": 1.5},
], ids=["unknown-status", "not-a-transition", "no-utc-offset", "confidence-above-one"])
def test_structural_errors_reject_the_whole_batch(make_tree, worker_settings, overrides):
    a = make_tree("alpha")
    response = post(worker_for(a.camera), a.camera,
                    [event_payload(a.slot), event_payload(a.slot, **overrides)])
    assert response.status_code == 422
    assert not OccupancyEvent.unscoped.exists()


# --------------------------------------------------------- state rules ----
def test_state_follows_newest_observation_not_arrival_order(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    post(worker, a.camera, [transition(a.slot, "VACANT", "OCCUPIED", 20)])
    post(worker, a.camera, [transition(a.slot, "OCCUPIED", "VACANT", 10)])

    state = state_of(a.slot)
    assert (state.status, state.since) == ("OCCUPIED", at(20))
    assert OccupancyEvent.unscoped.filter(slot=a.slot).count() == 2


def test_shuffled_batch_resolves_in_time_order(make_tree, worker_settings):
    a = make_tree("alpha")
    e1 = transition(a.slot, "VACANT", "OCCUPIED", 10)
    e2 = transition(a.slot, "OCCUPIED", "VACANT", 20)
    e3 = transition(a.slot, "VACANT", "OCCUPIED", 30)
    body = post(worker_for(a.camera), a.camera, [e3, e1, e2]).json()

    assert body["previous_status_mismatches"] == 0
    state = state_of(a.slot)
    assert (state.status, state.since, state.last_observed_at) == ("OCCUPIED", at(30), at(30))


def test_unknown_is_replaced_only_by_a_newer_observation(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    post(worker, a.camera, [transition(a.slot, "VACANT", "OCCUPIED", 10)])
    SlotState.unscoped.filter(slot=a.slot).update(status="UNKNOWN", since=timezone.now())

    post(worker, a.camera, [transition(a.slot, "OCCUPIED", "VACANT", 5)])
    assert state_of(a.slot).status == "UNKNOWN"

    post(worker, a.camera, [transition(a.slot, "UNKNOWN", "VACANT", 20)])
    state = state_of(a.slot)
    assert (state.status, state.since) == ("VACANT", at(20))


def test_reannouncing_an_unchanged_slot_keeps_dwell_time(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    post(worker, a.camera, [transition(a.slot, "VACANT", "OCCUPIED", 10)])
    body = post(worker, a.camera, [transition(a.slot, "UNKNOWN", "OCCUPIED", 20)]).json()

    assert body["previous_status_mismatches"] == 0
    state = state_of(a.slot)
    assert (state.since, state.last_observed_at) == (at(10), at(20))


def test_disagreeing_previous_status_is_stored_and_counted(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    post(worker, a.camera, [transition(a.slot, "VACANT", "OCCUPIED", 10)])
    body = post(worker, a.camera, [transition(a.slot, "VACANT", "OCCUPIED", 20)]).json()

    assert (body["accepted"], body["previous_status_mismatches"]) == (1, 1)
    assert OccupancyEvent.unscoped.filter(slot=a.slot).count() == 2
    assert state_of(a.slot).since == at(10)