# ml/apms_ml/evaluate.py
"""Run a method over a split and append one provenance-stamped row to results.csv.

Run:  python -m apms_ml.evaluate --method threshold_a --split same_lot_test
      python -m apms_ml.evaluate --method threshold_a --all-test
      python -m apms_ml.evaluate --method threshold_a --split same_lot_test --limit 2000

Accuracy pass: threaded image decode, streaming confusion counts, no prediction
array retained.
Latency pass: separate, single-threaded, fixed 50-patch batches, median of
repeated timings. 50 is the realistic per-frame slot count for one camera.

--limit forces --no-write. A partial run must never land in results.csv.
"""
from __future__ import annotations

import argparse
import csv
import platform
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from .metrics import Confusion
from .methods import available, build
from .paths import RESULTS_CSV, RESULTS_DIR, SPLITS_DIR

TEST_SPLITS = ["same_lot_test", "cross_lot_test", "cross_dataset_test",
               "weather_sunny", "weather_cloudy", "weather_rainy"]

DECISION_THRESHOLD = 0.5
LATENCY_BATCH_SIZE = 50
LATENCY_WARMUP_BATCHES = 5
LATENCY_TIMED_BATCHES = 30

RESULT_FIELDS = [
    "run_at", "git_commit", "method", "method_version", "split",
    "n_samples", "n_unreadable", "tp", "fp", "tn", "fn", "occupied_share",
    "accuracy", "precision", "recall", "specificity", "f1",
    "false_vacancy_rate", "miss_rate", "balanced_accuracy",
    "latency_ms_per_slot_p50", "latency_ms_per_slot_p90",
    "latency_batch_size", "model_size_bytes", "host_cpu", "notes",
]


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def host_cpu() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def read_split_rows(split: str, limit: int | None) -> list[dict]:
    path = SPLITS_DIR / f"{split}.csv"
    if not path.exists():
        raise SystemExit(f"{path} missing — run `python -m apms_ml.splits` first.")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{path} is empty.")
    return rows[:limit] if limit else rows


def load_patch(path: str) -> np.ndarray | None:
    return cv2.imread(path, cv2.IMREAD_COLOR)


def chunk(sequence: list, size: int):
    for start in range(0, len(sequence), size):
        yield sequence[start:start + size]


def run_accuracy(method, rows: list[dict], workers: int) -> tuple[Confusion, int]:
    confusion = Confusion()
    unreadable = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for block in chunk(rows, 2048):
            images = list(pool.map(load_patch, (r["path"] for r in block)))
            patches, truths = [], []
            for row, image in zip(block, images, strict=True):
                if image is None:
                    unreadable += 1
                    continue
                patches.append(image)
                truths.append(int(row["label"]))
            if not patches:
                continue
            scores = method.predict(patches)
            predictions = (np.asarray(scores) >= DECISION_THRESHOLD).astype(int)
            confusion.update_many(truths, predictions)
            done += len(block)
            print(f"\r  {done:,}/{len(rows):,}", end="", flush=True)
    print()
    return confusion, unreadable


def run_latency(method, rows: list[dict]) -> tuple[float | None, float | None]:
    """Single-threaded, decoding excluded. Measures the method, not the disk."""
    cv2.setNumThreads(1)
    sample_paths = [r["path"] for r in rows[:LATENCY_BATCH_SIZE * 4]]
    batch = [img for img in (load_patch(p) for p in sample_paths) if img is not None]
    batch = batch[:LATENCY_BATCH_SIZE]
    if len(batch) < LATENCY_BATCH_SIZE:
        print(f"  latency skipped: only {len(batch)} readable patches")
        return None, None

    # Methods that manage their own thread pool expose a pinned variant.
    predict = getattr(method, "predict_single_thread", method.predict)
    for _ in range(LATENCY_WARMUP_BATCHES):
        predict(batch)

    per_slot_ms: list[float] = []
    for _ in range(LATENCY_TIMED_BATCHES):
        start = time.perf_counter()
        predict(batch)
        elapsed = time.perf_counter() - start
        per_slot_ms.append(elapsed * 1000.0 / LATENCY_BATCH_SIZE)

    per_slot_ms.sort()
    p90_index = min(int(0.9 * len(per_slot_ms)), len(per_slot_ms) - 1)
    return statistics.median(per_slot_ms), per_slot_ms[p90_index]


def append_result(row: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not RESULTS_CSV.exists()
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS,
                                extrasaction="raise", restval="")
        if is_new:
            writer.writeheader()
        writer.writerow({k: ("" if row.get(k) is None else row.get(k, ""))
                         for k in RESULT_FIELDS})


def evaluate_one(method, split: str, limit: int | None,
                 workers: int, write: bool, notes: str) -> None:
    rows = read_split_rows(split, limit)
    print(f"\n=== {method.name} on {split} ({len(rows):,} patches) ===")

    started = time.perf_counter()
    confusion, unreadable = run_accuracy(method, rows, workers)
    print(f"  accuracy pass: {time.perf_counter() - started:.1f}s, "
          f"{unreadable} unreadable")

    p50, p90 = run_latency(method, rows)

    result = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_commit": git_commit(),
        "method": method.name,
        "method_version": method.version,
        "split": split,
        "n_unreadable": unreadable,
        "latency_ms_per_slot_p50": None if p50 is None else round(p50, 4),
        "latency_ms_per_slot_p90": None if p90 is None else round(p90, 4),
        "latency_batch_size": LATENCY_BATCH_SIZE,
        "model_size_bytes": method.model_size_bytes,
        "host_cpu": host_cpu(),
        "notes": notes,
        **confusion.as_dict(),
    }

    for key in ("accuracy", "precision", "recall", "specificity", "f1",
                "false_vacancy_rate", "miss_rate", "balanced_accuracy",
                "occupied_share"):
        if result[key] is not None:
            result[key] = round(result[key], 6)

    width = max(len(k) for k in result)
    for key in RESULT_FIELDS:
        value = result.get(key)
        print(f"  {key:<{width}}  {'' if value is None else value}")

    if write:
        append_result(result)
        print(f"\n  appended to {RESULTS_CSV}")
    else:
        print("\n  NOT written to results.csv")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", required=True, choices=available())
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--split")
    group.add_argument("--all-test", action="store_true",
                       help=f"run every test split: {', '.join(TEST_SPLITS)}")
    parser.add_argument("--limit", type=int, default=None,
                        help="smoke-test on the first N rows; forces --no-write")
    parser.add_argument("--workers", type=int, default=6,
                        help="image-decode threads for the accuracy pass")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    write = not args.no_write
    if args.limit:
        write = False
        print(f"--limit {args.limit} set: results.csv will not be written.")

    commit = git_commit()
    if write and commit.endswith("-dirty"):
        print(f"WARNING: working tree is dirty. Recording commit as {commit}.")
        print("         This row is not reproducible until you commit.")

    method = build(args.method)
    splits = TEST_SPLITS if args.all_test else [args.split]
    for split in splits:
        evaluate_one(method, split, args.limit, args.workers, write, args.notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())