# worker/main.py
"""Worker entry point.

    --once      score one frame and print it (Task 3.1's diagnostic)
    default     run continuously: sample, classify, vote, emit, heartbeat

The loop does one thing per frame and keeps two clocks: frames are sampled by
the source at the configured rate, and the heartbeat and config refresh are
driven by elapsed time, not by frame counts, so a slow frame cannot stop the
worker reporting.

Config is refetched every 5 minutes, and immediately whenever a heartbeat
comes back with a config_version the worker does not hold — which saves a poll
(04_API_CONTRACT.md).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

from worker.api_client import ApiClient, ApiError, AuthRejected, build_http_client
from worker.config import ConfigurationError, Settings, load_settings
from worker.contract import WorkerConfig
from worker.crops import crop_batch, scale_to_capture
from worker.inference import Classifier, ModelMismatch
from worker.model_store import ModelIntegrityError, ModelStore
from worker.queue import EventQueue
from worker.sender import Sender
from worker.smoothing import DECISION_THRESHOLD, VoterSet, window_seconds
from worker.source import FrameSource, SourceUnavailable

CONFIG_REFRESH_SECONDS = 300
HEARTBEAT_SECONDS = 30
FPS_WINDOW = 60.0

logger = logging.getLogger("worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Worker:
    def __init__(self, settings: Settings, client: ApiClient,
                 store: ModelStore, queue: EventQueue) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.queue = queue
        self.running = True
        self.started = time.monotonic()
        self.frames_processed = 0
        self._recent_frames: list[float] = []

        self.config: WorkerConfig | None = None
        self.classifier: Classifier | None = None
        self.voters: VoterSet | None = None
        self.source: FrameSource | None = None
        self.sender: Sender | None = None
        self.slot_order: list = []

    # ------------------------------------------------------------ lifecycle
    def stop(self, signum=None, frame=None) -> None:
        logger.info("shutting down")
        self.running = False

    def close(self) -> None:
        if self.source is not None:
            self.source.close()
        self.queue.close()

    # --------------------------------------------------------------- config
    def apply_config(self, config: WorkerConfig) -> None:
        previous = self.config
        self.config = config

        if previous is None or previous.model.version != config.model.version:
            path = self.store.ensure(config.model)
            self.classifier = Classifier.load(path, config.model.input_size,
                                              config.model.version,
                                              self.settings.ort_threads)

        if previous is None or previous.source_uri != config.source_uri \
                or previous.sampling.target_fps != config.sampling.target_fps:
            if self.source is not None:
                self.source.close()
            self.source = FrameSource(config.source_uri, config.sampling.target_fps)

        older = self.voters
        self.voters = VoterSet(config.slots, config.smoothing.window,
                               config.smoothing.min_agreement)
        self.voters.carry_over(older)
        self.slot_order = [slot.id for slot in config.slots]
        self.geometries = scale_to_capture(config.slots, config.reference_frame,
                                           self.source.size)
        if self.sender is None:
            self.sender = Sender(self.client, self.queue, config.camera_id)

        seconds = window_seconds(config.smoothing.window, config.sampling.target_fps)
        logger.info(
            "config %s: %d slot(s), %d-of-%d vote over %.1fs at %.1f fps",
            config.config_version, len(config.slots),
            config.smoothing.min_agreement, config.smoothing.window,
            seconds, config.sampling.target_fps)
        if previous is not None and previous.config_version != config.config_version:
            logger.info("config changed from %s", previous.config_version)

    def refresh_config(self) -> None:
        fetched = self.client.fetch_config()
        if self.config is not None and fetched.config_version == self.config.config_version:
            return
        self.apply_config(fetched)

    # ---------------------------------------------------------------- frame
    def process_frame(self, frame) -> None:
        observed_at = _now()
        batch = crop_batch(frame, self.geometries, self.config.model.input_size)
        probabilities = self.classifier.probabilities(batch)

        for voter, previous_status, probability in \
                self.voters.observe(self.slot_order, probabilities):
            self.queue.append(
                slot_id=voter.slot_id,
                previous_status=previous_status,
                status=voter.committed,
                confidence=probability if voter.committed == "OCCUPIED" else 1 - probability,
                observed_at=observed_at,
                model_version=self.config.model.version,
            )
            logger.info("%s %s -> %s (p=%.3f)", voter.label, previous_status,
                        voter.committed, probability)

        self.frames_processed += 1
        self._recent_frames.append(time.monotonic())

    def measured_fps(self) -> float:
        cutoff = time.monotonic() - FPS_WINDOW
        self._recent_frames = [stamp for stamp in self._recent_frames if stamp >= cutoff]
        if len(self._recent_frames) < 2:
            return 0.0
        span = self._recent_frames[-1] - self._recent_frames[0]
        return (len(self._recent_frames) - 1) / span if span > 0 else 0.0

    # ------------------------------------------------------------ heartbeat
    def send_heartbeat(self) -> str | None:
        result = self.client.heartbeat(
            self.config.camera_id,
            fps=self.measured_fps(),
            frames_processed=self.frames_processed,
            queue_depth=self.queue.depth(),
            model_version=self.config.model.version,
            uptime_seconds=time.monotonic() - self.started,
        )
        if result.resync_slot_ids:
            touched = self.voters.forget_committed(result.resync_slot_ids)
            if touched:
                logger.info("server asked for a resync of %d slot(s); "
                            "re-announcing on the next decision", touched)
        return result.config_version

    # ----------------------------------------------------------------- loop
    def run(self) -> int:
        self.refresh_config()
        frames = self.source.frames()

        last_config = last_heartbeat = time.monotonic()
        while self.running:
            try:
                self.process_frame(next(frames))
            except StopIteration:
                logger.error("frame source ended")
                return 1

            now = time.monotonic()
            try:
                self.sender.flush(now)

                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    served = self.send_heartbeat()
                    if served and served != self.config.config_version:
                        logger.info("heartbeat reports config %s; refetching", served)
                        self.refresh_config()
                        last_config = now

                if now - last_config >= CONFIG_REFRESH_SECONDS:
                    last_config = now
                    self.refresh_config()
            except AuthRejected as exc:
                logger.error("%s", exc)
                return 1
            except ApiError as exc:
                # Observation continues while the server is away; the queue
                # holds what it cannot deliver.
                logger.warning("server unreachable: %s", exc)
        return 0


def run_once(settings: Settings, save_crops: Path | None) -> int:
    with build_http_client(settings) as http:
        client = ApiClient(http)
        config = client.fetch_config()
        logger.info("camera %s in %r, config %s, %d slot(s)",
                    config.camera_id, config.lot_name, config.config_version,
                    len(config.slots))
        if not config.slots:
            logger.error("this camera has no active slots; annotate it first")
            return 1
        model_path = ModelStore(settings.models_dir).ensure(config.model)   # in run_once

    classifier = Classifier.load(model_path, config.model.input_size,
                                 config.model.version, settings.ort_threads)

    with FrameSource(config.source_uri, config.sampling.target_fps) as source:
        frame = source.read()
        if frame is None:
            logger.error("could not read a frame from %s", config.source_uri)
            return 1
        capture_size = (frame.shape[1], frame.shape[0])
        geometries = scale_to_capture(config.slots, config.reference_frame, capture_size)
        batch = crop_batch(frame, geometries, config.model.input_size)
        scores = classifier.probabilities(batch)

    print(f"\ncapture        {capture_size[0]}x{capture_size[1]}")
    print(f"reference      {config.reference_frame.width}x{config.reference_frame.height}")
    print(f"model          {config.model.version} "
          f"({config.model.input_size[0]}x{config.model.input_size[1]})")
    print(f"smoothing      {config.smoothing.min_agreement}-of-{config.smoothing.window}"
          f" = {window_seconds(config.smoothing.window, config.sampling.target_fps):.1f}s"
          " at this sampling rate   (not applied by --once)")
    print(f"\n{'slot':<10} {'p(occupied)':>12}  status")
    for geometry, score in zip(geometries, scores, strict=True):
        status = "OCCUPIED" if score >= DECISION_THRESHOLD else "VACANT"
        print(f"{geometry.label:<10} {score:>12.4f}  {status}")

    occupied = int((scores >= DECISION_THRESHOLD).sum())
    print(f"\n{occupied}/{len(scores)} occupied")
    if scores.size and float(scores.max() - scores.min()) < 0.01:
        print("\nEvery slot scored the same. That usually means the crops are "
              "wrong, not that the lot is uniform — check --save-crops.")

    if save_crops:
        save_crops.mkdir(parents=True, exist_ok=True)
        for geometry, patch, score in zip(geometries, batch, scores, strict=True):
            image = patch.transpose(1, 2, 0).astype("uint8")
            cv2.imwrite(str(save_crops / f"{geometry.label}_{score:.3f}.png"), image)
        cv2.imwrite(str(save_crops / "_frame.png"), frame)
        print(f"\ncrops written to {save_crops}")
        print("Put one next to a PKLot training patch. If the framing differs "
              "the benchmark does not describe this camera.")
    return 0


def run_forever(settings: Settings) -> int:
    queue = EventQueue(settings.data_dir / "outbox.sqlite3")
    with build_http_client(settings) as http:
        worker = Worker(settings, ApiClient(http), ModelStore(settings.models_dir), queue)
        signal.signal(signal.SIGTERM, worker.stop)
        signal.signal(signal.SIGINT, worker.stop)
        try:
            return worker.run()
        finally:
            worker.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="score one frame and exit")
    parser.add_argument("--save-crops", type=Path, metavar="DIR",
                        help="write the warped patches for inspection (with --once)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                        stream=sys.stderr)
    try:
        return run_once(settings, args.save_crops) if args.once else run_forever(settings)
    except (ApiError, ModelIntegrityError, ModelMismatch, SourceUnavailable) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())