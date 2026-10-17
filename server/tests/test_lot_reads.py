# server/tests/test_lot_reads.py
"""GET /lots and GET /lots/{id}/slots."""
from __future__ import annotations

from django.utils import timezone

from accounts.models import Role
from parking.models import Camera, ParkingLot, ParkingSlot, SlotState
from tests.factories import SQUARE, ApiClient, camera_fields

OWNER = "+9779811111111"
OPERATOR = "+9779822222222"


def logged_in(org, make_member, role=Role.OWNER, phone=OWNER) -> ApiClient:
    make_member(org, role, phone)
    api = ApiClient()
    api.login(phone)
    return api


def test_lot_counts_cover_every_active_slot(make_tree, make_member):
    a = make_tree("alpha")                                  # A-01, never observed
    slots = ParkingSlot.objects.for_org(a.org)
    vacant = slots.create(camera=a.camera, label="A-02", polygon=SQUARE)
    occupied = slots.create(camera=a.camera, label="A-03", polygon=SQUARE)
    stale = slots.create(camera=a.camera, label="A-04", polygon=SQUARE)
    slots.create(camera=a.camera, label="A-05", polygon=SQUARE, is_active=False)

    states = SlotState.objects.for_org(a.org)
    now = timezone.now()
    states.create(slot=vacant, status="VACANT", since=now)
    states.create(slot=occupied, status="OCCUPIED", since=now)
    states.create(slot=stale, status="UNKNOWN", since=now)

    [lot] = logged_in(a.org, make_member).get("/lots").json()
    assert (lot["total_slots"], lot["vacant"], lot["occupied"], lot["unknown"]) == (4, 1, 1, 2)
    assert lot["vacant"] + lot["occupied"] + lot["unknown"] == lot["total_slots"]
    assert lot["last_updated"] is not None


def test_lot_list_is_scoped_and_skips_inactive_lots(make_tree, make_member):
    a, b = make_tree("alpha"), make_tree("beta")
    ParkingLot.objects.for_org(a.org).create(name="closed", is_active=False)
    names = [lot["name"] for lot in logged_in(a.org, make_member).get("/lots").json()]
    assert names == ["alpha lot"]


def test_slots_are_grouped_under_their_own_camera(make_tree, make_member):
    a = make_tree("alpha")
    wide = Camera.objects.for_org(a.org).create(
        **camera_fields(a.lot, name="wide", frame_width=1920, frame_height=1080))
    ParkingSlot.objects.for_org(a.org).create(camera=wide, label="W-01", polygon=SQUARE)

    body = logged_in(a.org, make_member).get(f"/lots/{a.lot.pk}/slots").json()
    frames = {c["name"]: c["reference_frame"] for c in body["cameras"]}
    labels = {c["name"]: [s["label"] for s in c["slots"]] for c in body["cameras"]}
    assert frames == {"cam": {"width": 1280, "height": 720},
                      "wide": {"width": 1920, "height": 1080}}
    assert labels == {"cam": ["A-01"], "wide": ["W-01"]}


def test_slot_without_a_state_row_reads_as_unknown(make_tree, make_member):
    a = make_tree("alpha")
    body = logged_in(a.org, make_member).get(f"/lots/{a.lot.pk}/slots").json()
    slot = body["cameras"][0]["slots"][0]
    assert (slot["status"], slot["since"], slot["confidence"]) == ("UNKNOWN", None, None)
    assert slot["polygon"] == [[10.0, 10.0], [50.0, 10.0], [50.0, 40.0], [10.0, 40.0]]


def test_operators_can_read(make_tree, make_member):
    a = make_tree("alpha")
    api = logged_in(a.org, make_member, Role.OPERATOR, OPERATOR)
    assert api.get("/lots").status_code == 200
    assert api.get(f"/lots/{a.lot.pk}/slots").status_code == 200


def test_another_orgs_lot_is_not_found(make_tree, make_member):
    a, b = make_tree("alpha"), make_tree("beta")
    api = logged_in(a.org, make_member)
    response = api.get(f"/lots/{b.lot.pk}/slots")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "lot_not_found"