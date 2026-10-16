# server/tests/factories.py
"""Row builders and the constraint-violation assertion shared by the tests."""
from __future__ import annotations

import secrets
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

SQUARE = [[10, 10], [50, 10], [50, 40], [10, 40]]


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