# server/tests/test_auth.py
"""Human session authentication and the uniform error shape."""
from __future__ import annotations

import pytest
from django.test import Client

from accounts.models import User
from tests.factories import PASSWORD, ApiClient, give_token

PHONE = "+9779812345678"


@pytest.fixture
def user(db):
    return User.objects.create_user(PHONE, PASSWORD, full_name="Test Owner")


def test_openapi_schema_renders_anonymously(db):
    response = Client().get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert any(p.endswith("/auth/login") for p in paths)
    assert any(p.endswith("/token:rotate") for p in paths)


def test_login_requires_a_csrf_token(user):
    response = Client(enforce_csrf_checks=True).post(
        "/api/v1/auth/login", {"phone_number": PHONE, "password": PASSWORD},
        content_type="application/json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_login_establishes_a_session(user):
    api = ApiClient()
    assert api.login(PHONE).json()["user_id"] == str(user.pk)
    me = api.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["phone_number"] == PHONE


@pytest.mark.parametrize("typed", [
    "9812345678", "981-234-5678", "+977 981 234 5678", "009779812345678",
])
def test_login_accepts_the_formats_people_type(user, typed):
    ApiClient().login(typed)


def test_wrong_password_and_unknown_phone_are_indistinguishable(user):
    api = ApiClient()
    wrong = api.post("/auth/login", {"phone_number": PHONE, "password": "wrong-password-x"})
    unknown = api.post("/auth/login", {"phone_number": "+9779800000099",
                                       "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"


def test_invalid_phone_is_a_clean_422_not_a_500(user):
    """Regression guard: the validator's exception object in pydantic's ctx
    would crash JSON rendering if errors were passed through unfiltered."""
    response = ApiClient().post("/auth/login", {"phone_number": "12345",
                                                "password": PASSWORD})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_failed"
    assert any("phone_number" in item["loc"] for item in body["detail"]["errors"])


def test_inactive_user_cannot_log_in(user):
    User.objects.filter(pk=user.pk).update(is_active=False)
    response = ApiClient().post("/auth/login", {"phone_number": PHONE,
                                                "password": PASSWORD})
    assert response.status_code == 401


def test_unauthenticated_request_gets_the_uniform_shape(db):
    response = ApiClient().get("/auth/me")
    assert response.status_code == 401
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "detail"}
    assert body["error"]["code"] == "unauthenticated"


def test_session_requests_still_need_csrf_after_login(user):
    api = ApiClient()
    api.login(PHONE)
    response = api.http.post("/api/v1/auth/logout", content_type="application/json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_logout_ends_the_session(user):
    api = ApiClient()
    api.login(PHONE)
    assert api.post("/auth/logout").status_code == 204
    assert api.get("/auth/me").status_code == 401


def test_device_token_does_not_authenticate_a_human_endpoint(make_tree):
    token = give_token(make_tree("alpha").camera)
    response = ApiClient().get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401