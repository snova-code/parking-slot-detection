# server/tests/factories.py
"""Row builders, HTTP helpers and assertions shared by the tests."""
from __future__ import annotations

import secrets
import uuid
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory
from django.utils import timezone

SQUARE = [[10, 10], [50, 10], [50, 40], [10, 40]]
PASSWORD = "not-a-real-password-1"


def camera_fields(lot, **overrides) -> dict:
    return {
        "lot": lot,
        "name": "cam",
        "source_uri": "file:///data/demo.mp4",
        "device_token_hash": secrets.token_hex(32),
        "frame_width": 1280,
        "frame_height": 720,
        "model_version": "test-model",
        **overrides,
    }


def event_fields(slot, camera, **overrides) -> dict:
    return {
        "id": uuid.uuid4(),
        "slot": slot,
        "camera": camera,
        "previous_status": "VACANT",
        "status": "OCCUPIED",
        "confidence": Decimal("0.940"),
        "observed_at": timezone.now(),
        "model_version": "test-model",
        **overrides,
    }


def expect_integrity_error(action) -> None:
    """The statement must violate a database constraint. The savepoint keeps
    the test transaction usable afterwards."""
    with pytest.raises(IntegrityError), transaction.atomic():
        action()


def give_token(camera) -> str:
    from parking.models import Camera
    from parking.tokens import generate_device_token

    token, digest = generate_device_token()
    Camera.unscoped.filter(pk=camera.pk).update(device_token_hash=digest)
    return token


def bearer(token: str):
    return RequestFactory().get("/", headers={"Authorization": f"Bearer {token}"})


class ApiClient:
    """Drives the API the way the dashboard will: CSRF enforced, the token
    fetched from /auth/csrf, and the cookie's current value sent as
    X-CSRFToken on every unsafe request."""

    BASE = "/api/v1"

    def __init__(self) -> None:
        self.http = Client(enforce_csrf_checks=True)

    def _csrf(self) -> str:
        cookie = self.http.cookies.get(settings.CSRF_COOKIE_NAME)
        if cookie is None or not cookie.value:
            self.http.get(f"{self.BASE}/auth/csrf")
            cookie = self.http.cookies[settings.CSRF_COOKIE_NAME]
        return cookie.value

    def get(self, path: str, headers: dict | None = None):
        return self.http.get(self.BASE + path, headers=headers or {})

    def _unsafe(self, method: str, path: str, body, headers):
        send = getattr(self.http, method)
        return send(self.BASE + path, data=body or {}, content_type="application/json",
                    headers={"X-CSRFToken": self._csrf(), **(headers or {})})

    def post(self, path: str, body: dict | None = None, headers: dict | None = None):
        return self._unsafe("post", path, body, headers)

    def put(self, path: str, body: dict | None = None, headers: dict | None = None):
        return self._unsafe("put", path, body, headers)

    def login(self, phone: str, password: str = PASSWORD):
        response = self.post("/auth/login", {"phone_number": phone, "password": password})
        assert response.status_code == 200, response.content
        return response

class WorkerClient:
    """A worker: bearer token on every request, and Django's CSRF checks left
    ON — every POST through this client proves bearer-authenticated requests
    are not blocked by CsrfViewMiddleware."""

    BASE = "/api/v1"

    def __init__(self, token: str) -> None:
        self.http = Client(enforce_csrf_checks=True)
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str):
        return self.http.get(self.BASE + path, headers=self.headers)

    def post(self, path: str, body: dict):
        return self.http.post(self.BASE + path, data=body,
                              content_type="application/json", headers=self.headers)


def worker_for(camera) -> WorkerClient:
    return WorkerClient(give_token(camera))


def event_payload(slot, **overrides) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "slot_id": str(slot.pk),
        "previous_status": "VACANT",
        "status": "OCCUPIED",
        "confidence": 0.94,
        "observed_at": timezone.now().isoformat(),
        "model_version": "test-model",
        **overrides,
    }