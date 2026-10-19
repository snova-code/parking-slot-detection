# server/tests/test_schema_constraints.py
"""Database constraints, proven by violating them.

Each test attempts something the schema must forbid and expects the database —
not Python — to refuse. Where the scoping manager's own guards would get in the
way, rows go through `unscoped`, so the constraint under test is the only thing
standing between the bad row and the table.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import Membership, User
from parking.models import (
    Camera, OccupancyEvent, ParkingLot, ParkingSlot, SlotState,
)
from tests.factories import SQUARE, camera_fields, event_fields, expect_integrity_error


# ------------------------------------------------ tenant consistency ----
class TestTenantConsistency:
    def test_camera_cannot_point_at_another_orgs_lot(self, make_tree):
        a, b = make_tree("alpha"), make_tree("beta")
        expect_integrity_error(
            lambda: Camera.unscoped.create(org=a.org, **camera_fields(b.lot)))

    def test_slot_cannot_point_at_another_orgs_camera(self, make_tree):
        a, b = make_tree("alpha"), make_tree("beta")
        expect_integrity_error(lambda: ParkingSlot.unscoped.create(
            org=a.org, camera=b.camera, label="X-01", polygon=SQUARE))

    def test_slot_state_cannot_disagree_with_its_slot(self, make_tree):
        a, b = make_tree("alpha"), make_tree("beta")
        expect_integrity_error(lambda: SlotState.unscoped.create(
            org=a.org, slot=b.slot, status="VACANT", since=timezone.now()))

    def test_event_cannot_claim_another_orgs_camera(self, make_tree):
        a, b = make_tree("alpha"), make_tree("beta")
        expect_integrity_error(lambda: OccupancyEvent.unscoped.create(
            org=a.org, **event_fields(a.slot, b.camera)))

    def test_event_camera_must_own_the_slot(self, make_tree):
        """Same org, wrong camera: the database-level backstop for
        'a token for camera X cannot post events for camera Y'."""
        a = make_tree("alpha")
        other = Camera.objects.for_org(a.org).create(
            **camera_fields(a.lot, name="second"))
        expect_integrity_error(lambda: OccupancyEvent.objects.for_org(a.org).create(
            **event_fields(a.slot, other)))

    def test_consistent_rows_are_accepted(self, make_tree):
        a = make_tree("alpha")
        SlotState.objects.for_org(a.org).create(
            slot=a.slot, status="VACANT", since=timezone.now())
        OccupancyEvent.objects.for_org(a.org).create(**event_fields(a.slot, a.camera))


# ------------------------------------------------------------ polygon ----
VALID_POLYGONS = {
    "integer square": SQUARE,
    "float quad": [[10.5, 3.25], [60.0, 2.0], [61.5, 44.0], [9.0, 45.5]],
    "touches origin": [[0, 0], [1, 0], [1, 1], [0, 1]],
}

INVALID_POLYGONS = {
    "three points": [[0, 0], [1, 0], [1, 1]],
    "five points": [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
    "empty": [],
    "flat list of eight": [0, 0, 1, 0, 1, 1, 0, 1],
    "four scalars": [1, 2, 3, 4],
    "three coordinates": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
    "string coordinate": [["0", 0], [1, 0], [1, 1], [0, 1]],
    "null coordinate": [[None, 0], [1, 0], [1, 1], [0, 1]],
    "negative coordinate": [[-1, 0], [1, 0], [1, 1], [0, 1]],
    "object not array": {"points": SQUARE},
}


@pytest.mark.parametrize("polygon", VALID_POLYGONS.values(), ids=list(VALID_POLYGONS))
def test_valid_polygons_are_accepted(make_tree, polygon):
    a = make_tree("alpha")
    ParkingSlot.objects.for_org(a.org).create(camera=a.camera, label="B-01", polygon=polygon)


@pytest.mark.parametrize("polygon", INVALID_POLYGONS.values(), ids=list(INVALID_POLYGONS))
def test_malformed_polygons_fail_at_insert(make_tree, polygon):
    a = make_tree("alpha")
    expect_integrity_error(lambda: ParkingSlot.objects.for_org(a.org).create(
        camera=a.camera, label="B-01", polygon=polygon))


# ------------------------------------------------- enums and ranges ----
def test_a_third_role_is_rejected(make_tree):
    a = make_tree("alpha")
    user = User.objects.create_user(phone_number="+9779800000001")
    expect_integrity_error(lambda: Membership.objects.for_org(a.org).create(
        user=user, role="SUPERADMIN"))


@pytest.mark.parametrize("phone", ["9812345678", "+977 9812345678", "+0123456789", "+12"])
def test_phone_numbers_must_be_e164(db, phone):
    expect_integrity_error(lambda: User.objects.create_user(phone_number=phone))


def test_unknown_slot_type_is_rejected(make_tree):
    a = make_tree("alpha")
    expect_integrity_error(lambda: ParkingSlot.objects.for_org(a.org).create(
        camera=a.camera, label="B-01", polygon=SQUARE, slot_type="COMPACT"))


def test_unknown_slot_state_is_rejected(make_tree):
    a = make_tree("alpha")
    expect_integrity_error(lambda: SlotState.objects.for_org(a.org).create(
        slot=a.slot, status="PARKED", since=timezone.now()))


@pytest.mark.parametrize("overrides", [
    {"status": "UNKNOWN"},
    {"previous_status": "OCCUPIED", "status": "OCCUPIED"},
    {"confidence": Decimal("1.500")},
    {"confidence": Decimal("-0.100")},
], ids=["unknown-is-not-observable", "not-a-transition",
        "confidence-above-one", "confidence-below-zero"])
def test_invalid_events_are_rejected(make_tree, overrides):
    a = make_tree("alpha")
    expect_integrity_error(lambda: OccupancyEvent.objects.for_org(a.org).create(
        **event_fields(a.slot, a.camera, **overrides)))


def test_event_may_follow_an_unknown_state(make_tree):
    a = make_tree("alpha")
    OccupancyEvent.objects.for_org(a.org).create(
        **event_fields(a.slot, a.camera, previous_status="UNKNOWN"))


def test_camera_frame_must_be_positive(make_tree):
    a = make_tree("alpha")
    expect_integrity_error(lambda: Camera.objects.for_org(a.org).create(
        **camera_fields(a.lot, name="bad", frame_width=0)))


@pytest.mark.parametrize("coords", [
    {"latitude": Decimal("91"), "longitude": Decimal("85")},
    {"latitude": Decimal("27.7"), "longitude": None},
], ids=["latitude-out-of-range", "latitude-without-longitude"])
def test_lot_coordinates_are_checked(make_tree, coords):
    a = make_tree("alpha")
    expect_integrity_error(lambda: ParkingLot.objects.for_org(a.org).create(
        name="Elsewhere", **coords))


# ---------------------------------------------------------- uniqueness ----
def test_membership_is_unique_per_org(make_tree):
    a = make_tree("alpha")
    user = User.objects.create_user(phone_number="+9779800000001")
    Membership.objects.for_org(a.org).create(user=user, role="OWNER")
    expect_integrity_error(lambda: Membership.objects.for_org(a.org).create(
        user=user, role="OPERATOR"))


def test_lot_names_are_unique_within_an_org_only(make_tree):
    a, b = make_tree("alpha"), make_tree("beta")
    ParkingLot.objects.for_org(b.org).create(name="alpha lot")
    expect_integrity_error(lambda: ParkingLot.objects.for_org(a.org).create(
        name="alpha lot"))


def test_active_slot_labels_are_unique_per_camera(make_tree):
    a = make_tree("alpha")
    expect_integrity_error(lambda: ParkingSlot.objects.for_org(a.org).create(
        camera=a.camera, label="A-01", polygon=SQUARE))


def test_label_is_reusable_after_the_old_slot_is_deactivated(make_tree):
    a = make_tree("alpha")
    ParkingSlot.objects.for_org(a.org).filter(pk=a.slot.pk).update(is_active=False)
    ParkingSlot.objects.for_org(a.org).create(camera=a.camera, label="A-01", polygon=SQUARE)


def test_device_token_hash_is_globally_unique(make_tree):
    a, b = make_tree("alpha"), make_tree("beta")
    expect_integrity_error(lambda: Camera.objects.for_org(b.org).create(
        **camera_fields(b.lot, name="clone", device_token_hash=a.camera.device_token_hash)))


# --------------------------------------------------- event semantics ----
def test_events_are_append_only(make_tree):
    a = make_tree("alpha")
    scoped = OccupancyEvent.objects.for_org(a.org)
    event = scoped.create(**event_fields(a.slot, a.camera))

    expect_integrity_error(
        lambda: scoped.filter(pk=event.pk).update(confidence=Decimal("0.500")))

    event.confidence = Decimal("0.500")
    expect_integrity_error(event.save)


def test_duplicate_event_id_is_a_noop(make_tree):
    """The data-layer half of contract test #2: a retried batch cannot
    double-count."""
    a = make_tree("alpha")
    fields = event_fields(a.slot, a.camera)
    scoped = OccupancyEvent.objects.for_org(a.org)
    scoped.bulk_create([OccupancyEvent(**fields)], ignore_conflicts=True)
    scoped.bulk_create([OccupancyEvent(**fields)], ignore_conflicts=True)
    assert scoped.filter(pk=fields["id"]).count() == 1


# ----------------------------------------------------------- cascade ----
def test_deleting_an_org_cascades_through_every_table(make_tree):
    """Immediate composite FKs must not break ORM cascade ordering, and the
    append-only trigger must not block deletion."""
    trees = [make_tree("alpha"), make_tree("beta")]
    for index, tree in enumerate(trees, start=1):
        SlotState.objects.for_org(tree.org).create(
            slot=tree.slot, status="OCCUPIED", since=timezone.now())
        OccupancyEvent.objects.for_org(tree.org).create(
            **event_fields(tree.slot, tree.camera))
        user = User.objects.create_user(phone_number=f"+97798000000{index:02d}")
        Membership.objects.for_org(tree.org).create(user=user, role="OWNER")

    gone, kept = trees[0].org.pk, trees[1].org.pk
    trees[0].org.delete()

    for model in (Membership, ParkingLot, Camera, ParkingSlot, SlotState, OccupancyEvent):
        assert not model.unscoped.filter(org_id=gone).exists(), model.__name__
        assert model.unscoped.filter(org_id=kept).exists(), model.__name__