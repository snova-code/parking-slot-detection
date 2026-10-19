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

@pytest.fixture
def worker_settings(settings, tmp_path):
    """A model card for make_tree's 'test-model' plus deterministic worker
    settings. The smoothing numbers are arbitrary test values, not results."""
    import json

    card = {"version": "test-model", "onnx_sha256": "ab" * 32,
            "input": {"shape": ["batch", 3, 96, 96]}}
    (tmp_path / "test-model.json").write_text(json.dumps(card))
    settings.MODEL_CARDS_DIR = tmp_path
    settings.MODEL_URL_TEMPLATE = "https://models.example/{version}.onnx"
    settings.SMOOTHING_WINDOW = 9
    settings.SMOOTHING_MIN_AGREEMENT = 6
    settings.TARGET_FPS = 2.0
    return settings

@pytest.fixture
def as_owner(make_member):
    """A logged-in client for a new member of the given organization."""
    from accounts.models import Role
    from tests.factories import ApiClient

    def build(org, phone: str = "+9779811111111", role=Role.OWNER) -> ApiClient:
        make_member(org, role, phone)
        api = ApiClient()
        api.login(phone)
        return api

    return build

@pytest.fixture
def redis_bus(settings):
    """A reachable Redis bus for the SSE tests.

    The pubsub client is lru_cached per process, so the cache must be cleared
    around any test that changes REDIS_URL — otherwise this test inherits the
    connection dead_bus left behind, or leaves its own for the next one.

    Fails rather than skips when Redis is down: a silently skipped SSE suite
    looks like a passing one. Set APMS_SKIP_REDIS_TESTS=1 to opt out
    deliberately.
    """
    import os

    import redis as redis_lib

    from parking import pubsub

    pubsub.client.cache_clear()
    try:
        pubsub.client().ping()
    except redis_lib.RedisError as exc:
        pubsub.client.cache_clear()
        if os.environ.get("APMS_SKIP_REDIS_TESTS") == "1":
            pytest.skip(f"Redis unreachable at {settings.REDIS_URL}: {exc}")
        pytest.fail(
            f"Redis unreachable at {settings.REDIS_URL}: {exc}\n"
            "Start it with: docker compose -f infra/docker-compose.yml up -d redis"
        )
    yield pubsub.client()
    pubsub.client.cache_clear()


@pytest.fixture
def sse_settings(settings):
    """Short SSE timings so the stream tests finish in seconds.

    Production heartbeat is 20s; test_heartbeats_keep_the_stream_open reads the
    frame straight after the snapshot, and next_event() gives up at 10s. The
    connection cap is shortened too, so a stream that never closes ends the
    test instead of holding the suite open.
    """
    settings.SSE_HEARTBEAT_SECONDS = 0.2
    settings.SSE_MAX_CONNECTION_SECONDS = 5.0
    return settings