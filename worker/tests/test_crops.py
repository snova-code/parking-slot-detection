# worker/tests/test_crops.py
"""Scaling and the perspective warp — the code path v1 got wrong twice."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from worker.contract import ReferenceFrame, SlotSpec
from worker.crops import crop_batch, destination_quad, scale_to_capture, warp

SQUARE = [[100, 100], [200, 100], [200, 180], [100, 180]]


def slot(polygon=None, label="A-01"):
    return SlotSpec(id="3f2a0000-0000-4000-8000-000000000001",
                    label=label, polygon=polygon or SQUARE)


def test_polygons_are_scaled_once_for_a_larger_capture():
    [geometry] = scale_to_capture([slot()], ReferenceFrame(width=1280, height=720),
                                  (2560, 1440))
    assert geometry.quad.tolist() == [[200, 200], [400, 200], [400, 360], [200, 360]]


def test_identical_resolutions_do_not_move_anything():
    [geometry] = scale_to_capture([slot()], ReferenceFrame(width=1280, height=720),
                                  (1280, 720))
    assert geometry.quad.tolist() == [[float(x), float(y)] for x, y in SQUARE]


def test_a_different_aspect_ratio_is_reported(caplog):
    with caplog.at_level("WARNING"):
        scale_to_capture([slot()], ReferenceFrame(width=1280, height=720), (1280, 960))
    assert "aspect ratio" in caplog.text


def test_destination_corners_match_the_stored_winding():
    assert destination_quad((96, 96)).tolist() == [[0, 0], [95, 0], [95, 95], [0, 95]]


def test_warp_puts_the_quads_first_corner_top_left():
    """A marked corner must land where its index says it does; if it does not,
    every patch reaching the model is rotated."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(frame, (100, 100), (200, 180), (255, 255, 255), -1)
    cv2.rectangle(frame, (100, 100), (120, 116), (0, 0, 255), -1)   # red corner

    [geometry] = scale_to_capture([slot()], ReferenceFrame(width=1280, height=720),
                                  (1280, 720))
    patch = warp(frame, geometry, (96, 96))

    top_left = patch[4, 4]
    bottom_right = patch[91, 91]
    assert top_left[2] > 200 and top_left[0] < 60       # red
    assert bottom_right.min() > 200                      # white


def test_a_rotated_quad_produces_a_rotated_patch():
    """Documents why the server canonicalises corner order rather than leaving
    it to clients."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(frame, (100, 100), (200, 180), (255, 255, 255), -1)
    cv2.rectangle(frame, (100, 100), (120, 116), (0, 0, 255), -1)

    reference = ReferenceFrame(width=1280, height=720)
    upright = warp(scale_to_capture([slot()], reference, (1280, 720))[0], (96, 96)) \
        if False else warp(scale_to_capture([slot()], reference, (1280, 720))[0],
                           (96, 96))
    rotated_quad = SQUARE[1:] + SQUARE[:1]
    rotated = warp(scale_to_capture([slot(rotated_quad)], reference, (1280, 720))[0],
                   (96, 96))
    assert not np.array_equal(upright, rotated)


def test_batch_shape_and_dtype():
    frame = np.full((720, 1280, 3), 128, dtype=np.uint8)
    geometries = scale_to_capture([slot(), slot(label="A-02")],
                                  ReferenceFrame(width=1280, height=720), (1280, 720))
    batch = crop_batch(frame, geometries, (96, 96))
    assert batch.shape == (2, 3, 96, 96)
    assert batch.dtype == np.float32
    assert batch.max() <= 255 and batch.min() >= 0


def test_no_slots_gives_an_empty_batch():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert crop_batch(frame, [], (96, 96)).shape == (0, 3, 96, 96)