# server/tests/test_worker_config.py
"""GET /worker/config."""
from __future__ import annotations

from django.test import Client

from accounts.models import Role
from parking.models import Camera, ParkingSlot
from tests.factories import SQUARE, ApiClient, camera_fields, worker_for


def test_config_serves_only_this_cameras_active_slots(make_tree, worker_settings):
    a, b = make_tree("alpha"), make_tree("beta")
    scoped = ParkingSlot.objects.for_org(a.org)
    scoped.create(camera=a.camera, label="A-02", polygon=SQUARE)
    scoped.create(camera=a.camera, label="A-03", polygon=SQUARE, is_active=False)
    sibling = Camera.objects.for_org(a.org).create(**camera_fields(a.lot, name="sibling"))
    scoped.create(camera=sibling, label="S-01", polygon=SQUARE)

    body = worker_for(a.camera).get("/worker/config").json()
    assert [s["label"] for s in body["slots"]] == ["A-01", "A-02"]
    assert body["camera_id"] == str(a.camera.pk)
    assert body["reference_frame"] == {"width": 1280, "height": 720}


def test_config_carries_model_facts_from_the_card(make_tree, worker_settings):
    body = worker_for(make_tree("alpha").camera).get("/worker/config").json()
    assert body["model"] == {
        "version": "test-model",
        "url": "https://models.example/test-model.onnx",
        "sha256": "ab" * 32,
        "input_size": [96, 96],
    }
    assert body["smoothing"] == {"window": 9, "min_agreement": 6}
    assert body["sampling"] == {"target_fps": 2.0}


def test_config_version_changes_exactly_when_content_does(make_tree, worker_settings):
    a = make_tree("alpha")
    worker = worker_for(a.camera)

    def version() -> str:
        return worker.get("/worker/config").json()["config_version"]

    first = version()
    assert version() == first

    slots = ParkingSlot.objects.for_org(a.org).filter(pk=a.slot.pk)
    slots.update(polygon=[[11, 10], [50, 10], [50, 40], [10, 40]])
    moved = version()
    assert moved != first

    worker_settings.SMOOTHING_WINDOW = 15
    retuned = version()
    assert retuned != moved

    slots.update(is_active=False)
    assert version() != retuned


def test_missing_model_card_is_503(make_tree, worker_settings):
    a = make_tree("alpha")
    Camera.objects.for_org(a.org).filter(pk=a.camera.pk).update(model_version="no-such-model")
    response = worker_for(a.camera).get("/worker/config")
    assert response.status_code == 503
    assert response.json()["error"]["detail"]["reason"] == "no model card"


def test_path_like_model_version_is_never_read(make_tree, worker_settings):
    a = make_tree("alpha")
    Camera.objects.for_org(a.org).filter(pk=a.camera.pk).update(
        model_version="../../etc/passwd")
    response = worker_for(a.camera).get("/worker/config")
    assert response.status_code == 503
    assert response.json()["error"]["detail"]["reason"] == "invalid version string"


def test_worker_routes_require_a_token(db):
    assert Client().get("/api/v1/worker/config").status_code == 401


def test_session_does_not_authenticate_worker_routes(make_tree, make_member, worker_settings):
    """The other half of 'never accept one credential on the other's routes'."""
    a = make_tree("alpha")
    make_member(a.org, Role.OWNER, "+9779811111111")
    api = ApiClient()
    api.login("+9779811111111")
    assert api.get("/worker/config").status_code == 401