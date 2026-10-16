# server/tests/conftest.py
from __future__ import annotations

from types import SimpleNamespace

import pytest

from accounts.models import Membership, Organization, User
from parking.models import Camera, ParkingLot, ParkingSlot
from tests.factories import PASSWORD, SQUARE, camera_fields


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    """Test-only. PBKDF2 is slow on purpose; across every login in the suite
    it would dominate the run. Production hashing is untouched."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


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


@pytest.fixture
def make_member(db):
    def build(org, role, phone: str) -> User:
        user = User.objects.filter(phone_number=phone).first()
        if user is None:
            user = User.objects.create_user(phone, PASSWORD)
        Membership.objects.for_org(org).create(user=user, role=role)
        return user

    return build