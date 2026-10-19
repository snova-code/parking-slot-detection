# worker/source.py
"""Frames from a video file, an RTSP stream or an HTTP stream.

Sampling is done by skipping with grab() rather than by sleeping between
reads. Two reasons: a demo video then plays at its natural speed instead of
15x slow, and a live stream stays current instead of falling further behind
its buffer with every cycle.

Nothing here opens a window. No imshow, no waitKey — the worker must run
headless in a container (02_ARCHITECTURE.md).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import cv2
import numpy as np

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)


class SourceUnavailable(RuntimeError):
    pass


def resolve_uri(uri: str) -> tuple[str, bool]:
    """Returns (what to hand OpenCV, is_file)."""
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        path = Path(unquote(parsed.path if parsed.scheme else uri)).expanduser()
        if not path.exists():
            raise SourceUnavailable(f"no such file: {path}")
        return str(path), True
    return uri, False


class FrameSource:
    def __init__(self, uri: str, target_fps: float) -> None:
        self.uri, self.is_file = resolve_uri(uri)
        self.target_fps = target_fps
        self.capture: cv2.VideoCapture | None = None
        self.loops = 0
        self._open()

    # ---------------------------------------------------------------- setup
    def _open(self) -> None:
        capture = cv2.VideoCapture(self.uri)
        if not capture.isOpened():
            raise SourceUnavailable(f"OpenCV could not open {self.uri}")
        self.capture = capture
        self.source_fps = self._source_fps(capture)
        self.skip = max(int(round(self.source_fps / self.target_fps)) - 1, 0) \
            if self.source_fps else 0
        logger.info("source %s open at %dx%d, %.1f fps; sampling %.1f fps "
                    "(skipping %d frames per sample)",
                    self.uri, *self.size, self.source_fps, self.target_fps, self.skip)

    @staticmethod
    def _source_fps(capture) -> float:
        reported = capture.get(cv2.CAP_PROP_FPS)
        if reported is None or reported != reported or reported <= 0:  # NaN or 0
            return 0.0
        return float(reported)

    @property
    def size(self) -> tuple[int, int]:
        return (int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def __enter__(self) -> "FrameSource":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- reads
    def read(self) -> np.ndarray | None:
        for _ in range(self.skip):
            self.capture.grab()
        ok, frame = self.capture.read()
        if ok:
            return frame
        if self.is_file:
            return self._rewind()
        return None

    def _rewind(self) -> np.ndarray | None:
        self.loops += 1
        self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = self.capture.read()
        if ok:
            return frame
        # Some containers will not seek; reopening always works.
        self.close()
        self._open()
        ok, frame = self.capture.read()
        return frame if ok else None

    def frames(self):
        """Yields frames at roughly target_fps, forever. Files loop; streams
        reconnect with backoff."""
        interval = 1.0 / self.target_fps
        failures = 0
        next_tick = time.monotonic()
        while True:
            frame = self.read()
            if frame is None:
                delay = RECONNECT_BACKOFF[min(failures, len(RECONNECT_BACKOFF) - 1)]
                failures += 1
                logger.warning("source read failed, reconnecting in %.0fs", delay)
                time.sleep(delay)
                self.close()
                try:
                    self._open()
                except SourceUnavailable as exc:
                    logger.warning("reopen failed: %s", exc)
                continue
            failures = 0
            yield frame
            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # Falling behind: drop the debt rather than accumulating it.
                next_tick = time.monotonic()