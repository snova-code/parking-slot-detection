# server/tests/conftest.py
from __future__ import annotations

from types import SimpleNamespace

import pytest

from accounts.models import Organization
from parking.models import Camera, ParkingLot, ParkingSlot
from tests.factories import SQUARE, camera_fields


@pytest.fixture
def make_tree(db):
    """One organization with a lot, a camera and a slot, built through the
    scoped managers exactly as application code would."""

    def build(slug: str) -> SimpleNamespace:
        org = Organization.objects.create(name=slug.title(), slug=slug)
        lot = ParkingLot.objects.for_org(org).create(name=f"{slug} lot")
        camera = Camera.objects.for_org(org).create(**camera_fields(lot))
        slot = ParkingSlot.objects.for_org(org).create(
            camera=camera, label="A-01", polygon=SQUARE)
        return SimpleNamespace(org=org, lot=lot, camera=camera, slot=slot)

    return build