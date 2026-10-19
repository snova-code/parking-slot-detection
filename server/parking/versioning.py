# server/parking/versioning.py
"""Content hashes used as optimistic-concurrency tokens.

Two different things are versioned, deliberately:

  slots_version   the camera's reference frame and its active slots — exactly
                  what a bulk slot write replaces. The annotation tool sends
                  back the value it drew against, so one person's save cannot
                  silently overwrite another's.
  config_version  everything the worker is told, including settings-derived
                  fields. Using it as the annotation token would reject a save
                  because an unrelated environment variable changed.
"""
from __future__ import annotations

import hashlib
import json

from parking.models import ParkingSlot


def content_hash(body) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def slots_version(camera) -> str:
    slots = (ParkingSlot.objects.for_org(camera.org_id)
             .filter(camera=camera, is_active=True).order_by("label")
             .values_list("label", "slot_type", "polygon"))
    return content_hash({
        "frame": [camera.frame_width, camera.frame_height],
        "slots": [[label, slot_type, polygon] for label, slot_type, polygon in slots],
    })