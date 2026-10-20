# worker/tests/test_frame_upload.py
"""The worker's side of the reference-frame upload."""
from __future__ import annotations

import httpx
import numpy as np
import pytest

from worker.api_client import ApiClient

CAMERA = "3f2a0000-0000-4000-8000-000000000001"


def client_with(handler) -> ApiClient:
    return ApiClient(httpx.Client(transport=httpx.MockTransport(handler),
                                  base_url="http://server/api/v1"))


def test_a_jpeg_is_posted_as_raw_bytes():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        seen["type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"camera_id": CAMERA, "width": 1280,
                                         "height": 720, "bytes": len(request.content),
                                         "matches_reference_frame": True})

    result = client_with(handler).upload_frame(b"\xff\xd8\xff-pretend-jpeg")
    assert seen["type"] == "image/jpeg"
    assert seen["body"].startswith(b"\xff\xd8\xff")
    assert result.matches_reference_frame is True


def test_a_mismatch_is_surfaced_not_swallowed():
    handler = lambda request: httpx.Response(200, json={
        "camera_id": CAMERA, "width": 1920, "height": 1080,
        "bytes": 10, "matches_reference_frame": False})
    assert client_with(handler).upload_frame(b"x").matches_reference_frame is False


def test_encoding_a_real_frame_produces_a_jpeg():
    import cv2

    frame = np.random.default_rng(0).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert ok
    assert encoded.tobytes().startswith(b"\xff\xd8\xff")