# server/tests/test_slot_bulk_write.py
"""POST /cameras/{id}/slots:bulk."""
from __future__ import annotations

from accounts.models import Role
from parking.models import OccupancyEvent, ParkingSlot
from tests.factories import make_event
from tests.test_analytics import ktm

BAY = [[100, 100], [200, 100], [200, 180], [100, 180]]
OTHER_BAY = [[300, 100], [400, 100], [400, 180], [300, 180]]


def version_of(api, lot, camera) -> str:
    body = api.get(f"/lots/{lot.pk}/slots").json()
    return next(c["slots_version"] for c in body["cameras"] if c["id"] == str(camera.pk))


def bulk(api, camera, slots, version, frame=None):
    return api.post(f"/cameras/{camera.pk}/slots:bulk", {
        "slots_version": version,
        "reference_frame": frame or {"width": 1280, "height": 720},
        "slots": slots,
    })


def slot(label, polygon=None, slot_type="STANDARD") -> dict:
    return {"label": label, "polygon": polygon or BAY, "slot_type": slot_type}


def test_bulk_write_creates_updates_and_deactivates(make_tree, as_owner):
    a = make_tree("alpha")                     # starts with A-01
    api = as_owner(a.org)
    body = bulk(api, a.camera, [slot("A-01"), slot("A-02", OTHER_BAY)],
                version_of(api, a.lot, a.camera)).json()
    assert (body["created"], body["updated"], body["deactivated"]) == (1, 1, 0)

    body = bulk(api, a.camera, [slot("A-02", OTHER_BAY)], body["slots_version"]).json()
    assert (body["created"], body["updated"], body["deactivated"]) == (0, 0, 1)
    assert [s["label"] for s in body["slots"]] == ["A-02"]


def test_resending_the_same_set_changes_nothing(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    first = bulk(api, a.camera, [slot("A-01")], version_of(api, a.lot, a.camera)).json()
    again = bulk(api, a.camera, [slot("A-01")], first["slots_version"]).json()
    assert (again["created"], again["updated"], again["deactivated"]) == (0, 0, 0)
    assert again["slots_version"] == first["slots_version"]


def test_removing_a_slot_keeps_its_history(make_tree, as_owner):
    a = make_tree("alpha")
    make_event(a.slot, a.camera, "VACANT", "OCCUPIED", ktm(10, 10))
    api = as_owner(a.org)
    bulk(api, a.camera, [], version_of(api, a.lot, a.camera))

    removed = ParkingSlot.unscoped.get(pk=a.slot.pk)
    assert removed.is_active is False
    assert OccupancyEvent.unscoped.filter(slot=a.slot).count() == 1


def test_a_reused_label_becomes_a_new_slot(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    emptied = bulk(api, a.camera, [], version_of(api, a.lot, a.camera)).json()
    body = bulk(api, a.camera, [slot("A-01", OTHER_BAY)], emptied["slots_version"]).json()

    assert body["slots"][0]["id"] != str(a.slot.pk)
    assert ParkingSlot.unscoped.filter(camera=a.camera, label="A-01").count() == 2


def test_a_stale_version_is_refused(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    stale = version_of(api, a.lot, a.camera)
    bulk(api, a.camera, [slot("A-01", OTHER_BAY)], stale)

    response = bulk(api, a.camera, [slot("A-99")], stale)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "slots_version_conflict"
    assert error["detail"]["current_slots_version"] == version_of(api, a.lot, a.camera)
    assert not ParkingSlot.unscoped.filter(label="A-99").exists()


def test_corners_are_stored_clockwise_from_the_nearest_top_left(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    scrambled = [[200, 180], [100, 180], [100, 100], [200, 100]]   # CCW from bottom-right
    body = bulk(api, a.camera, [slot("A-01", scrambled)],
                version_of(api, a.lot, a.camera)).json()
    assert body["slots"][0]["polygon"] == [[100.0, 100.0], [200.0, 100.0],
                                           [200.0, 180.0], [100.0, 180.0]]


def test_unusable_geometry_is_refused(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    current = version_of(api, a.lot, a.camera)

    outside = bulk(api, a.camera, [slot("A-01", [[100, 100], [2000, 100],
                                                 [2000, 180], [100, 180]])], current)
    assert outside.status_code == 422
    assert outside.json()["error"]["detail"]["label"] == "A-01"

    tiny = bulk(api, a.camera, [slot("A-01", [[100, 100], [101, 100],
                                              [101, 101], [100, 101]])], current)
    assert tiny.status_code == 422


def test_duplicate_labels_are_refused(make_tree, as_owner):
    a = make_tree("alpha")
    api = as_owner(a.org)
    response = bulk(api, a.camera, [slot("A-01"), slot("A-01", OTHER_BAY)],
                    version_of(api, a.lot, a.camera))
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["labels"] == ["A-01"]


def test_the_reference_frame_moves_with_the_slots(make_tree, as_owner, worker_settings):
    a = make_tree("alpha")
    api = as_owner(a.org)
    body = bulk(api, a.camera, [slot("A-01")], version_of(api, a.lot, a.camera),
                frame={"width": 1920, "height": 1080}).json()
    assert body["reference_frame"] == {"width": 1920, "height": 1080}

    camera = api.get(f"/lots/{a.lot.pk}/slots").json()["cameras"][0]
    assert camera["reference_frame"] == {"width": 1920, "height": 1080}


def test_operators_cannot_annotate(make_tree, as_owner):
    a = make_tree("alpha")
    owner = as_owner(a.org)
    current = version_of(owner, a.lot, a.camera)
    operator = as_owner(a.org, phone="+9779822222222", role=Role.OPERATOR)
    assert bulk(operator, a.camera, [slot("A-01")], current).status_code == 403


def test_another_orgs_camera_is_not_found(make_tree, as_owner):
    a, b = make_tree("alpha"), make_tree("beta")
    api = as_owner(a.org)
    response = bulk(api, b.camera, [slot("A-01")], "whatever")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"