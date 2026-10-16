# server/tests/test_org_resolution.py
"""Session → organization → role, resolved on every request."""
from __future__ import annotations

import pytest

from accounts.models import Membership, Role
from accounts.security import OrgMember
from parking.models import Camera
from tests.factories import ApiClient

OWNER = "+9779811111111"
OPERATOR = "+9779822222222"


def rotate(api: ApiClient, camera):
    return api.post(f"/cameras/{camera.pk}/token:rotate")


def logged_in(phone: str) -> ApiClient:
    api = ApiClient()
    api.login(phone)
    return api


def test_single_membership_is_selected_automatically(make_tree, make_member):
    a = make_tree("alpha")
    make_member(a.org, Role.OWNER, OWNER)
    api = logged_in(OWNER)
    assert rotate(api, a.camera).status_code == 200
    assert api.get("/auth/me").json()["active_org_id"] == str(a.org.pk)


def test_several_memberships_require_an_explicit_choice(make_tree, make_member):
    a, b = make_tree("alpha"), make_tree("beta")
    make_member(a.org, Role.OWNER, OWNER)
    make_member(b.org, Role.OWNER, OWNER)
    api = logged_in(OWNER)

    refused = rotate(api, a.camera)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "org_selection_required"

    assert api.put("/auth/active-org", {"org_id": str(b.org.pk)}).status_code == 200
    assert rotate(api, b.camera).status_code == 200
    # The choice scopes everything: alpha's camera is now invisible.
    assert rotate(api, a.camera).status_code == 404


def test_cannot_select_an_org_you_do_not_belong_to(make_tree, make_member):
    a, b = make_tree("alpha"), make_tree("beta")
    make_member(a.org, Role.OWNER, OWNER)
    response = logged_in(OWNER).put("/auth/active-org", {"org_id": str(b.org.pk)})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "org_not_found"


def test_another_orgs_camera_is_not_found_rather_than_forbidden(make_tree, make_member):
    a, b = make_tree("alpha"), make_tree("beta")
    make_member(a.org, Role.OWNER, OWNER)
    response = rotate(logged_in(OWNER), b.camera)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"
    assert (Camera.unscoped.get(pk=b.camera.pk).device_token_hash
            == b.camera.device_token_hash)


def test_operator_cannot_perform_owner_actions(make_tree, make_member):
    a = make_tree("alpha")
    make_member(a.org, Role.OPERATOR, OPERATOR)
    response = rotate(logged_in(OPERATOR), a.camera)
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "insufficient_role"
    assert body["detail"] == {"required": ["OWNER"], "held": "OPERATOR"}


def test_demotion_takes_effect_on_the_next_request(make_tree, make_member):
    a = make_tree("alpha")
    user = make_member(a.org, Role.OWNER, OWNER)
    api = logged_in(OWNER)
    assert rotate(api, a.camera).status_code == 200

    Membership.objects.for_org(a.org).filter(user=user).update(role=Role.OPERATOR)
    assert rotate(api, a.camera).json()["error"]["code"] == "insufficient_role"


def test_removal_takes_effect_on_the_next_request(make_tree, make_member):
    a = make_tree("alpha")
    user = make_member(a.org, Role.OWNER, OWNER)
    api = logged_in(OWNER)
    assert rotate(api, a.camera).status_code == 200

    Membership.objects.for_org(a.org).filter(user=user).delete()
    response = rotate(api, a.camera)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "no_membership"


def test_role_sets_must_be_role_members_not_strings():
    """v1 compared 'parking_admin' against a stored 'parkingadmin'. A string
    cannot reach the comparison here."""
    with pytest.raises(TypeError):
        OrgMember({"OWNER"})
    with pytest.raises(TypeError):
        OrgMember(set())