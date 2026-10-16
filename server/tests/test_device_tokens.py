# server/tests/test_device_tokens.py
"""Device token issue, lookup and rotation."""
from __future__ import annotations

import hashlib

import pytest

from accounts.models import Role
from parking.models import Camera, ParkingLot
from parking.security import device_token
from parking.tokens import TOKEN_PATTERN, generate_device_token, hash_device_token
from tests.factories import ApiClient, bearer, give_token

OWNER = "+9779811111111"


def test_generated_tokens_have_the_documented_shape():
    token, digest = generate_device_token()
    assert TOKEN_PATTERN.fullmatch(token)
    assert len(token) == len("apms_dev_") + 40
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert generate_device_token()[0] != token


def test_valid_token_resolves_to_its_camera(make_tree):
    a = make_tree("alpha")
    context = device_token(bearer(give_token(a.camera)))
    assert context.camera.pk == a.camera.pk
    assert context.org_id == a.org.pk


@pytest.mark.parametrize("presented", [
    "", "apms_dev_short", "apms_usr_" + "a" * 40, "apms_dev_" + "x" * 40,
], ids=["empty", "too-short", "wrong-prefix", "well-formed-but-unknown"])
def test_malformed_or_unknown_tokens_are_refused(make_tree, presented):
    make_tree("alpha")
    assert device_token(bearer(presented)) is None


def test_inactive_camera_is_refused(make_tree):
    a = make_tree("alpha")
    token = give_token(a.camera)
    Camera.unscoped.filter(pk=a.camera.pk).update(is_active=False)
    assert device_token(bearer(token)) is None


def test_camera_on_an_inactive_lot_is_refused(make_tree):
    a = make_tree("alpha")
    token = give_token(a.camera)
    ParkingLot.unscoped.filter(pk=a.lot.pk).update(is_active=False)
    assert device_token(bearer(token)) is None


def test_rotation_issues_a_working_token_and_kills_the_old_one(make_tree, make_member):
    a = make_tree("alpha")
    old = give_token(a.camera)
    make_member(a.org, Role.OWNER, OWNER)
    api = ApiClient()
    api.login(OWNER)

    response = api.post(f"/cameras/{a.camera.pk}/token:rotate")
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    new = response.json()["device_token"]

    assert device_token(bearer(old)) is None
    assert device_token(bearer(new)).camera.pk == a.camera.pk
    stored = Camera.unscoped.get(pk=a.camera.pk).device_token_hash
    assert stored == hash_device_token(new)
    assert new not in stored