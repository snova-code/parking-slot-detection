# server/tests/test_commands.py
"""Bootstrap commands."""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from accounts.models import Membership, Organization, Role, User
from parking.models import Camera, ParkingLot
from parking.security import device_token
from parking.tokens import hash_device_token
from tests.factories import bearer

STRONG = "Parking-Occupancy-Bench-7"


def run(command: str, /, **options) -> tuple[str, str]:
    """Call a management command, returning (stdout, stderr).

    `command` is positional-only. Both bootstrap commands take their own
    --name option, and a normal parameter here shadows it: run("create_camera",
    name="North") binds "create_camera" to the parameter and then sees name in
    the kwargs, raising TypeError before the body runs.
    """
    out, err = StringIO(), StringIO()
    call_command(command, stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


def org_options(**overrides) -> dict:
    return {"slug": "labim", "name": "Labim Mall", "owner_phone": "9812345678",
            "password_env": "APMS_OWNER_PASSWORD", **overrides}


def test_create_org_bootstraps_an_owner(db, monkeypatch):
    monkeypatch.setenv("APMS_OWNER_PASSWORD", STRONG)
    run("create_org", **org_options())
    org = Organization.objects.get(slug="labim")
    user = User.objects.get(phone_number="+9779812345678")
    assert user.check_password(STRONG)
    assert Membership.objects.for_org(org).get(user=user).role == Role.OWNER


def test_create_org_refuses_a_weak_password_and_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("APMS_OWNER_PASSWORD", "12345678")
    with pytest.raises(CommandError):
        run("create_org", **org_options())
    assert not Organization.objects.filter(slug="labim").exists()
    assert not User.objects.exists()


def test_create_org_refuses_a_duplicate_slug(db, monkeypatch):
    monkeypatch.setenv("APMS_OWNER_PASSWORD", STRONG)
    run("create_org", **org_options())
    with pytest.raises(CommandError):
        run("create_org", **org_options(owner_phone="9800000001"))


def test_existing_user_joins_without_touching_their_password(db, monkeypatch):
    user = User.objects.create_user("+9779812345678", STRONG)

    def no_prompt(*_):
        raise AssertionError("must not prompt for an existing user's password")

    monkeypatch.setattr("accounts.management.commands.create_org.getpass", no_prompt)
    run("create_org", **org_options(password_env=None))
    user.refresh_from_db()
    assert user.check_password(STRONG)


def test_create_camera_prints_only_the_token_on_stdout(make_tree):
    a = make_tree("alpha")
    out, err = run("create_camera", org="alpha", lot="North deck", name="North",
                   source_uri="file:///data/demo.mp4", frame="1280x720",
                   model_version="mnv3s-test")
    token = out.strip()
    context = device_token(bearer(token))
    assert context is not None and context.camera.name == "North"
    assert (Camera.unscoped.get(pk=context.camera.pk).device_token_hash
            == hash_device_token(token))
    assert token not in err
    assert ParkingLot.objects.for_org(a.org).filter(name="North deck").exists()


def test_create_camera_reuses_an_existing_lot(make_tree):
    a = make_tree("alpha")
    run("create_camera", org="alpha", lot="alpha lot", name="Second",
        source_uri="file:///data/demo.mp4", frame="1280x720", model_version="m")
    assert ParkingLot.objects.for_org(a.org).filter(name="alpha lot").count() == 1


@pytest.mark.parametrize("frame", ["1280", "0x720", "wide", "1280x-1"])
def test_create_camera_rejects_bad_frames(make_tree, frame):
    make_tree("alpha")
    with pytest.raises(CommandError):
        run("create_camera", org="alpha", lot="L", name="N",
            source_uri="file:///x.mp4", frame=frame, model_version="m")