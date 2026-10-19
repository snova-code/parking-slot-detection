# server/parking/worker_config.py
"""What GET /worker/config serves, its version, and which slots need a resync.

config_version is a hash of the served content, not a counter. The smoothing
pair, sampling rate and model URL come from settings, and a counter in the
database cannot see an environment variable change on redeploy. A hash changes
exactly when the served config does — and never otherwise, so a server restart
does not make every worker rebuild its slot map.

Model facts (sha256, input size) are read from the committed model card the
training script wrote, so the server never holds a second copy that could
drift from the file the worker actually downloads.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db.models import Q

from config.errors import ApiError
from parking.models import Camera, ParkingSlot, SlotStatus
from parking.versioning import content_hash

MODEL_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _unavailable(version: str, reason: str) -> ApiError:
    return ApiError(503, "model_unavailable",
                    "The camera's model is not available from this server.",
                    {"model_version": version, "reason": reason})


@lru_cache(maxsize=32)
def _read_card(path: str, mtime_ns: int) -> dict:
    # mtime_ns is in the key so an edited card is re-read without a restart.
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_model_card(version: str) -> dict:
    if not MODEL_VERSION_PATTERN.fullmatch(version or ""):
        # The version becomes part of a filesystem path. Refuse anything that
        # could name a file outside MODEL_CARDS_DIR.
        raise _unavailable(version, "invalid version string")
    path = Path(settings.MODEL_CARDS_DIR) / f"{version}.json"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        raise _unavailable(version, "no model card") from None
    try:
        card = _read_card(str(path), mtime_ns)
        shape = card["input"]["shape"]
        valid = bool(
            card["version"] == version
            and SHA256_PATTERN.fullmatch(card["onnx_sha256"])
            and len(shape) == 4
            and all(isinstance(d, int) and d > 0 for d in shape[1:])
        )
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise _unavailable(version, "malformed model card")
    return card


def build_config(camera: Camera) -> dict:
    card = load_model_card(camera.model_version)
    _, _, height, width = card["input"]["shape"]
    slots = (ParkingSlot.objects.for_org(camera.org_id)
             .filter(camera=camera, is_active=True)
             .order_by("label")
             .values("id", "label", "polygon"))
    body = {
        "camera_id": str(camera.pk),
        "lot_name": camera.lot.name,
        "source_uri": camera.source_uri,
        "reference_frame": {"width": camera.frame_width, "height": camera.frame_height},
        "model": {
            "version": camera.model_version,
            "url": settings.MODEL_URL_TEMPLATE.format(version=camera.model_version),
            "sha256": card["onnx_sha256"],
            "input_size": [height, width],
        },
        "smoothing": {
            "window": settings.SMOOTHING_WINDOW,
            "min_agreement": settings.SMOOTHING_MIN_AGREEMENT,
        },
        "sampling": {"target_fps": settings.TARGET_FPS},
        "slots": [{"id": str(s["id"]), "label": s["label"], "polygon": s["polygon"]}
                  for s in slots],
    }
    body["config_version"] = content_hash(body)
    return body


def resync_slot_ids(camera: Camera) -> list[UUID]:
    """Active slots with no current observation on the server: never observed,
    or marked UNKNOWN. The worker resets exactly these and re-announces them."""
    return list(
        ParkingSlot.objects.for_org(camera.org_id)
        .filter(camera=camera, is_active=True)
        .filter(Q(state__isnull=True) | Q(state__status=SlotStatus.UNKNOWN))
        .order_by("label")
        .values_list("pk", flat=True)
    )