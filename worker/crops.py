# worker/crops.py
"""Polygon to model input, in one transform.

Slot polygons arrive in the camera's reference-frame pixels. The capture may
be at another resolution, so each point is scaled once, here, and never again.
v1 scaled by x2 in one code path and x1.8/x1.9 in another; there is one path
in this program and this is it.

The perspective warp goes straight to the model's input size — no intermediate
crop, no second resize. cv2.getPerspectiveTransform maps point i of the quad
to corner i of the destination, which is why the server canonicalises corner
order to clockwise-from-top-left before storing (docs/04_API_CONTRACT.md,
slots:bulk). Feeding a differently wound quad would silently rotate or mirror
every patch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import cv2
import numpy as np

from worker.contract import ReferenceFrame, SlotSpec

logger = logging.getLogger(__name__)
ASPECT_TOLERANCE = 0.01


@dataclass(frozen=True)
class SlotGeometry:
    slot_id: UUID
    label: str
    quad: np.ndarray        # (4, 2) float32, in capture pixels


def scale_to_capture(slots: list[SlotSpec], reference: ReferenceFrame,
                     capture_size: tuple[int, int]) -> list[SlotGeometry]:
    """capture_size is (width, height) as OpenCV reports it."""
    capture_width, capture_height = capture_size
    scale_x = capture_width / reference.width
    scale_y = capture_height / reference.height

    if abs(scale_x - scale_y) / max(scale_x, scale_y) > ASPECT_TOLERANCE:
        logger.warning(
            "capture %dx%d has a different aspect ratio from the reference "
            "frame %dx%d (x%.3f vs x%.3f): every crop will be stretched. "
            "Re-annotate at the capture resolution.",
            capture_width, capture_height, reference.width, reference.height,
            scale_x, scale_y)
    elif abs(scale_x - 1.0) > 1e-6:
        logger.info("scaling polygons by x%.3f for a %dx%d capture",
                    scale_x, capture_width, capture_height)

    return [
        SlotGeometry(
            slot_id=slot.id,
            label=slot.label,
            quad=np.array([[x * scale_x, y * scale_y] for x, y in slot.polygon],
                          dtype=np.float32),
        )
        for slot in slots
    ]


def destination_quad(size: tuple[int, int]) -> np.ndarray:
    """Corners of the output patch, in the same clockwise-from-top-left order
    the polygons are stored in."""
    height, width = size
    return np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
                    dtype=np.float32)


def warp(frame: np.ndarray, geometry: SlotGeometry, size: tuple[int, int]) -> np.ndarray:
    height, width = size
    transform = cv2.getPerspectiveTransform(geometry.quad, destination_quad(size))
    return cv2.warpPerspective(frame, transform, (width, height),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)


def crop_batch(frame: np.ndarray, geometries: list[SlotGeometry],
               size: tuple[int, int]) -> np.ndarray:
    """(N, 3, H, W) float32, BGR in [0, 255] — what the exported graph expects.
    Channel order, scaling and normalisation live inside the model, so there is
    nothing here that can disagree with training."""
    if not geometries:
        return np.empty((0, 3, size[0], size[1]), dtype=np.float32)
    patches = [warp(frame, geometry, size) for geometry in geometries]
    stacked = np.stack(patches).transpose(0, 3, 1, 2)
    return np.ascontiguousarray(stacked, dtype=np.float32)