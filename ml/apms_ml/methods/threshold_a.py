# ml/apms_ml/methods/threshold_a.py
"""Method A — the v1 adaptive-threshold rule, ported unchanged.

This is a baseline, not a candidate. It is deliberately not tuned: the point is
to measure what the original project actually achieved, including on data it was
never calibrated for.

Every constant below comes from the pipeline recorded in
05_BENCHMARK_PROTOCOL.md. Three of them are underdetermined by that description
and must be diffed against the v1 source before any row produced by this file
enters RESULTS.md. They are marked VERIFY.
"""
from __future__ import annotations

import cv2
import numpy as np

from . import register

# v1 calibrated its constant against axis-aligned boxes of this size at 640x480.
# 660 is meaningless at any other scale, so patches are resized to it. Doing so
# is the only way the comparison is fair to the baseline.
# VERIFY (1): orientation. Read as width=40, height=25.
REFERENCE_SIZE = (40, 25)          # (width, height) in pixels

GAUSSIAN_KSIZE = (3, 3)
# VERIFY (2): sigmaX. The recorded pipeline gives only the kernel size.
# 0 lets OpenCV derive sigma from the kernel; many v1-era snippets pass 1.
GAUSSIAN_SIGMA_X = 0

ADAPTIVE_MAX_VALUE = 255
ADAPTIVE_BLOCK_SIZE = 25
ADAPTIVE_C = 16
MEDIAN_KSIZE = 3
DILATE_KERNEL = np.ones((3, 3), np.uint8)

# VERIFY (3): polarity. Implemented literally as recorded — count the ZERO
# pixels, and fewer than 660 means occupied. If v1 used cv2.countNonZero and
# compared that, the comparison inverts and every Method A number is wrong.
ZERO_PIXEL_THRESHOLD = 660

_TOTAL_PIXELS = REFERENCE_SIZE[0] * REFERENCE_SIZE[1]


# Set True only after the three VERIFY items above have been diffed against the
# v1 source and any corrections committed. The flag is baked into `version`,
# which is recorded in every results.csv row, so rows produced before
# verification stay marked unverified permanently. Bump PORT_REVISION if the
# diff changes any constant.
V1_SOURCE_VERIFIED = False
PORT_REVISION = 1


class ThresholdBaseline:
    name = "threshold_a"
    version = (f"v1-port-{PORT_REVISION}-"
               f"{'verified' if V1_SOURCE_VERIFIED else 'unverified'}")
    model_size_bytes = 0

    def predict(self, patches: list[np.ndarray]) -> np.ndarray:
        scores = np.empty(len(patches), dtype=np.float32)
        for position, patch in enumerate(patches):
            scores[position] = self._classify(patch)
        return scores

    @staticmethod
    def _classify(patch: np.ndarray) -> float:
        if patch.ndim == 3:
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        else:
            gray = patch
        gray = cv2.resize(gray, REFERENCE_SIZE, interpolation=cv2.INTER_AREA)
        blurred = cv2.GaussianBlur(gray, GAUSSIAN_KSIZE, GAUSSIAN_SIGMA_X)
        binary = cv2.adaptiveThreshold(
            blurred,
            ADAPTIVE_MAX_VALUE,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            ADAPTIVE_BLOCK_SIZE,
            ADAPTIVE_C,
        )
        binary = cv2.medianBlur(binary, MEDIAN_KSIZE)
        binary = cv2.dilate(binary, DILATE_KERNEL)
        zero_pixels = _TOTAL_PIXELS - cv2.countNonZero(binary)
        return 1.0 if zero_pixels < ZERO_PIXEL_THRESHOLD else 0.0


register("threshold_a", ThresholdBaseline)