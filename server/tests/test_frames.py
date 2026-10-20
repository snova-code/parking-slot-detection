# server/tests/test_frames.py
"""POST /worker/frame and GET /cameras/{id}/frame."""
from __future__ import annotations

import struct

import pytest

from accounts.models import Role
from parking.frames import frame_path
from tests.factories import worker_for


def jpeg(width: int, height: int, padding: int = 0) -> bytes:
    """A minimal JPEG: SOI, a SOF0 frame header, EOI. Enough for a dimension
    reader, and it keeps an image library out of the test suite."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + sof + b"\x00" * padding + b"\xff\xd9"


def upload(camera, body: bytes):
    return worker_for(camera).http.post(
        "/api/v1/worker/frame", data=body, content_type="image/jpeg",
        headers={"Authorization": worker_for(camera).headers["Authorization"]})


def upload_with(worker, body: bytes):
    return worker.http.post("/api/v1/worker/frame", data=body,
                            content_type="image/jpeg", headers=worker.headers)


def test_a_frame_is_stored_and_served_back(make_tree, as_owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    a = make_tree("alpha")
    body = jpeg(1280, 720, padding=64)

    stored = upload_with(worker_for(a.camera), body).json()
    assert (stored["width"], stored["height"]) == (1280, 720)
    assert stored["matches_reference_frame"] is True
    assert frame_path(a.camera.pk).read_bytes() == body

    served = as_owner(a.org).get(f"/cameras/{a.camera.pk}/frame")
    assert served.status_code == 200
    assert served["Content-Type"] == "image/jpeg"
    assert b"".join(served.streaming_content) == body


def test_a_capture_resolution_mismatch_is_reported(make_tree, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    a = make_tree("alpha")          # reference frame is 1280x720
    stored = upload_with(worker_for(a.camera), jpeg(1920, 1080)).json()
    assert (stored["width"], stored["height"]) == (1920, 1080)
    assert stored["matches_reference_frame"] is False


def test_a_second_upload_replaces_the_first(make_tree, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    a = make_tree("alpha")
    worker = worker_for(a.camera)
    upload_with(worker, jpeg(1280, 720, padding=8))
    upload_with(worker, jpeg(1280, 720, padding=999))
    assert len(list((tmp_path / "frames").iterdir())) == 1
    assert len(frame_path(a.camera.pk).read_bytes()) > 900


@pytest.mark.parametrize("body", [b"", b"not a jpeg at all", b"\xff\xd8\xff" + b"\x00" * 40],
                         ids=["empty", "not-jpeg", "jpeg-magic-no-header"])
def test_unusable_uploads_are_refused(make_tree, settings, tmp_path, body):
    settings.MEDIA_ROOT = tmp_path
    a = make_tree("alpha")
    response = upload_with(worker_for(a.camera), body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_frame"
    assert not frame_path(a.camera.pk).exists()


def test_an_oversized_frame_is_refused(make_tree, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.FRAME_MAX_BYTES = 512
    a = make_tree("alpha")
    response = upload_with(worker_for(a.camera), jpeg(1280, 720, padding=4096))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "frame_too_large"


def test_a_camera_without_a_frame_says_so(make_tree, as_owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    a = make_tree("alpha")
    response = as_owner(a.org).get(f"/cameras/{a.camera.pk}/frame")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "frame_not_available"


def test_another_orgs_frame_is_not_served(make_tree, as_owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    a, b = make_tree("alpha"), make_tree("beta")
    upload_with(worker_for(b.camera), jpeg(1280, 720))
    response = as_owner(a.org).get(f"/cameras/{b.camera.pk}/frame")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "camera_not_found"


def test_operators_may_view_a_frame(make_tree, as_owner, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    a = make_tree("alpha")
    upload_with(worker_for(a.camera), jpeg(1280, 720))
    api = as_owner(a.org, phone="+9779822222222", role=Role.OPERATOR)
    assert api.get(f"/cameras/{a.camera.pk}/frame").status_code == 200