# worker/main.py
"""Worker entry point.

Today: `--once` fetches config, makes sure the model is cached, grabs one
frame, and prints a probability per slot. That is the whole perception path
end to end, and it is what tells you whether the polygons, the scaling and the
model actually agree before any events are emitted.

    APMS_ENV_FILE=worker/.env ./scripts/with_env.sh \\
        ~/venvs/worker/bin/python -m worker.main --once --save-crops /tmp/crops
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2

from worker.api_client import ApiClient, ApiError, build_http_client
from worker.config import ConfigurationError, load_settings
from worker.crops import crop_batch, scale_to_capture
from worker.inference import Classifier, ModelMismatch
from worker.model_store import ModelIntegrityError, ModelStore
from worker.source import FrameSource, SourceUnavailable

DECISION_THRESHOLD = 0.5
logger = logging.getLogger("worker")


def run_once(settings, save_crops: Path | None) -> int:
    with build_http_client(settings) as http:
        client = ApiClient(http)
        config = client.fetch_config()
        logger.info("camera %s in %r, config %s, %d slot(s)",
                    config.camera_id, config.lot_name, config.config_version,
                    len(config.slots))
        if not config.slots:
            logger.error("this camera has no active slots; annotate it first")
            return 1

        model_path = ModelStore(settings.models_dir, http).ensure(config.model)

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
          "   (not applied by --once)")
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="score one frame and exit")
    parser.add_argument("--save-crops", type=Path, metavar="DIR",
                        help="write the warped patches for inspection")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not args.once:
        print("Only --once is implemented so far. The continuous loop, "
              "smoothing and event emission arrive with Task 3.2/3.3.",
              file=sys.stderr)
        return 2

    try:
        return run_once(settings, args.save_crops)
    except (ApiError, ModelIntegrityError, ModelMismatch, SourceUnavailable) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())