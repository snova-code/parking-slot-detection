# server/tests/test_lot_and_camera_writes.py
"""POST /lots and POST /lots/{id}/cameras."""
from __future__ import annotations

from accounts.models import Role
from parking.models import Camera, ParkingLot
from parking.security import device_token
from tests.factories import bearer

OPERATOR = "+9779822222222"


def lot_payload(**overrides) -> dict:
    return {"name": "Labim Mall", "city": "Lalitpur", **overrides}


def camera_payload(**overrides) -> dict:
    return {"name": "North deck", "source_uri": "file:///data/demo.mp4",
            "reference_frame": {"width": 1280, "height": 720},
            "model_version": "test-model", **overrides}


def test_creating_a_lot_makes_it_readable(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    created = api.post("/lots", lot_payload())
    assert created.status_code == 200
    assert created.json()["timezone"] == "Asia/Kathmandu"
    assert "Labim Mall" in [lot["name"] for lot in api.get("/lots").json()]


def test_duplicate_lot_name_within_an_org_is_409(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    api.post("/lots", lot_payload())
    repeated = api.post("/lots", lot_payload())
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "lot_name_taken"


def test_the_same_lot_name_is_free_in_another_org(make_tree, as_owner):
    a, b = make_tree("alpha"), make_tree("beta")
    assert as_owner(a.org).post("/lots", lot_payload()).status_code == 200
    assert as_owner(b.org, phone="+9779813333333").post(
        "/lots", lot_payload()).status_code == 200


def test_unusable_lot_fields_are_422(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    assert api.post("/lots", lot_payload(timezone="Mars/Olympus")).status_code == 422
    assert api.post("/lots", lot_payload(latitude=27.7)).status_code == 422


def test_operators_cannot_write(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org, phone=OPERATOR, role=Role.OPERATOR)
    response = api.post("/lots", lot_payload())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_created_camera_token_authenticates_that_camera(make_tree, as_owner, worker_settings):
    a = make_tree("alpha")
    api = as_owner(a.org)
    response = api.post(f"/lots/{a.lot.pk}/cameras", camera_payload())
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"

    body = response.json()
    context = device_token(bearer(body["device_token"]))
    assert str(context.camera.pk) == body["id"]
    assert Camera.unscoped.get(pk=body["id"]).device_token_hash != body["device_token"]


def test_camera_with_no_model_card_is_refused(make_tree, as_owner, worker_settings):
    a = make_tree("alpha")
    response = as_owner(a.org).post(f"/lots/{a.lot.pk}/cameras",
                                    camera_payload(model_version="no-such-model"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model_version"
    assert Camera.objects.for_org(a.org).filter(name="North deck").count() == 0


def test_unusable_source_uri_is_422(make_tree, as_owner, worker_settings):
    a = make_tree("alpha")
    assert as_owner(a.org).post(
        f"/lots/{a.lot.pk}/cameras",
        camera_payload(source_uri="/data/demo.mp4")).status_code == 422


def test_cannot_add_a_camera_to_another_orgs_lot(make_tree, as_owner, worker_settings):
    a, b = make_tree("alpha"), make_tree("beta")
    response = as_owner(a.org).post(f"/lots/{b.lot.pk}/cameras", camera_payload())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "lot_not_found"