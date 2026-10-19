# worker/tests/test_source.py
"""Frame reading, sampling and looping, against a real encoded file."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from worker.source import FrameSource, SourceUnavailable, resolve_uri

WIDTH, HEIGHT, FPS, FRAMES = 160, 120, 10, 20


@pytest.fixture
def video(tmp_path):
    """Frame n is filled with intensity n*10, so a frame identifies itself."""
    path = tmp_path / "clip.avi"
    for fourcc in ("MJPG", "mp4v"):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc),
                                 FPS, (WIDTH, HEIGHT))
        if writer.isOpened():
            break
        writer.release()
    else:
        pytest.skip("this OpenCV build cannot encode MJPG or mp4v")
    for index in range(FRAMES):
        writer.write(np.full((HEIGHT, WIDTH, 3), index * 10, dtype=np.uint8))
    writer.release()
    return path


def test_file_uris_are_resolved_both_ways(video):
    assert resolve_uri(f"file://{video}") == (str(video), True)
    assert resolve_uri(str(video)) == (str(video), True)
    assert resolve_uri("rtsp://camera/stream") == ("rtsp://camera/stream", False)


def test_a_missing_file_fails_at_open(tmp_path):
    with pytest.raises(SourceUnavailable):
        FrameSource(str(tmp_path / "absent.avi"), 2.0)


def test_capture_size_is_reported(video):
    with FrameSource(str(video), 2.0) as source:
        assert source.size == (WIDTH, HEIGHT)


def test_sampling_skips_frames_rather_than_sleeping(video):
    """At 2 fps from a 10 fps file, consecutive samples are five frames apart —
    a demo video plays at natural speed instead of 5x slow."""
    with FrameSource(str(video), 2.0) as source:
        assert source.skip == 4
        first = int(source.read()[0, 0, 0])
        second = int(source.read()[0, 0, 0])
    assert second - first == pytest.approx(50, abs=15)


def test_a_file_loops_instead_of_ending(video):
    with FrameSource(str(video), float(FPS)) as source:
        for _ in range(FRAMES + 3):
            assert source.read() is not None
        assert source.loops >= 1