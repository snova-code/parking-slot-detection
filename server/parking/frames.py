# server/parking/frames.py
"""Reference frames: one JPEG per camera, uploaded by its worker.

Phase 4's annotation tool has to draw polygons on the image the camera
actually produces. Anything else — a screenshot someone took, a still from a
different crop of the stream — reintroduces the coordinate mismatch this
rebuild exists to remove.

The file is validated as a decodable JPEG before it is stored, and its
dimensions are recorded. A worker capturing at a different resolution from the
one the polygons were drawn against is reported rather than tolerated.
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

from config.errors import ApiError

logger = logging.getLogger(__name__)

JPEG_MAGIC = b"\xff\xd8\xff"


def frame_path(camera_id) -> Path:
    return Path(settings.MEDIA_ROOT) / "frames" / f"{camera_id}.jpg"


def read_dimensions(data: bytes) -> tuple[int, int]:
    """(width, height) from the JPEG itself, decoded rather than believed.

    cv2 is already a server dependency through nothing at all — so this parses
    the SOF marker directly and keeps opencv out of the web process.
    """
    if not data.startswith(JPEG_MAGIC):
        raise ApiError(422, "invalid_frame", "The upload is not a JPEG.")
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = int.from_bytes(data[index + 2:index + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            if width < 1 or height < 1:
                raise ApiError(422, "invalid_frame", "The JPEG reports no size.")
            return width, height
        index += 2 + segment
    raise ApiError(422, "invalid_frame", "No JPEG frame header found.")


def store(camera, data: bytes) -> tuple[int, int]:
    if len(data) > settings.FRAME_MAX_BYTES:
        raise ApiError(413, "frame_too_large", "Reference frame is too large.",
                       {"limit_bytes": settings.FRAME_MAX_BYTES,
                        "received_bytes": len(data)})
    width, height = read_dimensions(data)
    target = frame_path(camera.pk)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(target)          # atomic: the tool never reads half a frame
    logger.info("reference frame stored camera=%s %dx%d %d bytes",
                camera.pk, width, height, len(data))
    return width, height