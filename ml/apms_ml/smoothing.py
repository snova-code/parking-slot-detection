# ml/apms_ml/smoothing.py
"""Method C — N-of-M temporal vote over Method B's scores.

Sweep on validation:
    python -m apms_ml.smoothing --sweep --split same_lot_val

Apply the chosen parameters to the test splits:
    python -m apms_ml.smoothing --apply --m 9 --n 6 --all-test

The window is CAUSAL: the vote at frame i covers frames [i-M+1, i]. A centered
window would score better and could not be implemented in the worker, which has
no access to future frames.

During warm-up (fewer than M observations available) the requirement scales to
ceil((N/M) * w). At w=1 this is the raw prediction, so M=1,N=1 reproduces
Method B exactly and serves as the unsmoothed flicker baseline.

Sequences are (dataset, lot, camera, slot_id, day) ordered by capture time, and
are broken wherever the gap between consecutive frames exceeds --max-gap-minutes.
Voting across a three-hour hole would be meaningless.
"""
from __future__ import annotations

import argparse
import csv
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .evaluate import TEST_SPLITS, append_result
from .provenance import git_commit, host_cpu
from .metrics import Confusion
from .paths import SCORES_DIR, SMOOTHING_SWEEP_MD, SPLITS_DIR

DECISION_THRESHOLD = 0.5
SWEEP_GRID_M = [5, 9, 15]
SWEEP_FRACTIONS = [0.6, 0.7, 0.8]
DEFAULT_MAX_GAP_MINUTES = 20.0
SCORE_METHOD = "mobilenet_b"


@dataclass
class Sequence:
    key: tuple
    times: np.ndarray    # seconds since epoch, ascending
    truth: np.ndarray    # int8
    raw: np.ndarray      # int8, Method B hard prediction


# ------------------------------------------------------------- loading ----
def load_scores(method: str, split: str) -> dict[str, float]:
    path = SCORES_DIR / f"{method}__{split}.csv"
    if not path.exists():
        raise SystemExit(
            f"{path} missing. Generate it first:\n"
            f"  python -m apms_ml.evaluate --method {method} "
            f"--split {split} --dump-scores"
            + ("  --no-write" if split.endswith("_val") else "")
        )
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return {row[0]: float(row[1]) for row in reader if len(row) == 2}


def build_sequences(split: str, scores: dict[str, float],
                    max_gap_minutes: float) -> tuple[list[Sequence], dict]:
    split_path = SPLITS_DIR / f"{split}.csv"
    if not split_path.exists():
        raise SystemExit(f"{split_path} missing — run `python -m apms_ml.splits`.")

    grouped: dict[tuple, list[tuple]] = defaultdict(list)
    report = {"rows": 0, "no_score": 0, "no_timestamp": 0, "no_slot_id": 0}

    with open(split_path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            report["rows"] += 1
            score = scores.get(row["path"])
            if score is None:
                report["no_score"] += 1
                continue
            if not row["capture_datetime"]:
                report["no_timestamp"] += 1
                continue
            if not row["slot_id"]:
                report["no_slot_id"] += 1
                continue
            stamp = datetime.fromisoformat(row["capture_datetime"]).timestamp()
            key = (row["dataset"], row["lot"], row["camera"], row["slot_id"], row["day"])
            grouped[key].append((stamp, int(row["label"]),
                                 1 if score >= DECISION_THRESHOLD else 0))

    max_gap_seconds = max_gap_minutes * 60.0
    sequences: list[Sequence] = []
    breaks = 0
    intervals: list[float] = []

    for key, entries in grouped.items():
        entries.sort(key=lambda item: item[0])
        run_start = 0
        for position in range(1, len(entries) + 1):
            at_end = position == len(entries)
            if not at_end:
                gap = entries[position][0] - entries[position - 1][0]
                intervals.append(gap)
                if gap <= max_gap_seconds:
                    continue
                breaks += 1
            block = entries[run_start:position]
            sequences.append(Sequence(
                key=key,
                times=np.array([e[0] for e in block], dtype=np.float64),
                truth=np.array([e[1] for e in block], dtype=np.int8),
                raw=np.array([e[2] for e in block], dtype=np.int8),
            ))
            run_start = position

    report["sequences"] = len(sequences)
    report["gap_breaks"] = breaks
    report["used_rows"] = int(sum(len(s.truth) for s in sequences))
    report["median_interval_seconds"] = (
        float(np.median(intervals)) if intervals else 0.0
    )
    return sequences, report


# -------------------------------------------------------------- voting ----
def smooth(raw: np.ndarray, window: int, min_agreement: int) -> np.ndarray:
    """Causal N-of-M vote with a scaled warm-up threshold."""
    n = raw.size
    if window <= 1:
        return raw.copy()
    cumulative = np.concatenate(([0], np.cumsum(raw, dtype=np.int64)))
    positions = np.arange(n)
    lengths = np.minimum(positions + 1, window)
    starts = positions - lengths + 1
    votes = cumulative[positions + 1] - cumulative[starts]
    required = np.ceil((min_agreement / window) * lengths).astype(np.int64)
    return (votes >= required).astype(np.int8)


def transitions(series: np.ndarray) -> int:
    return int(np.count_nonzero(series[1:] != series[:-1])) if series.size > 1 else 0


def detection_lags(truth: np.ndarray, predicted: np.ndarray,
                   times: np.ndarray) -> tuple[list[float], int]:
    """Seconds between each true transition and the first frame at which the
    prediction reflects the new state. Transitions never reflected are counted
    as missed rather than folded into the median."""
    lags: list[float] = []
    missed = 0
    change_points = np.flatnonzero(truth[1:] != truth[:-1]) + 1
    for index in change_points:
        target = truth[index]
        matches = np.flatnonzero(predicted[index:] == target)
        if matches.size == 0:
            missed += 1
        else:
            lags.append(float(times[index + matches[0]] - times[index]))
    return lags, missed


# ----------------------------------------------------------- evaluation ----
def evaluate_combination(sequences: list[Sequence], window: int,
                         min_agreement: int) -> dict:
    confusion = Confusion()
    predicted_transitions = 0
    truth_transitions = 0
    total_seconds = 0.0
    lags: list[float] = []
    missed = 0

    started = time.perf_counter()
    for sequence in sequences:
        predicted = smooth(sequence.raw, window, min_agreement)
        confusion.update_many(sequence.truth, predicted)
        predicted_transitions += transitions(predicted)
        truth_transitions += transitions(sequence.truth)
        if sequence.times.size > 1:
            total_seconds += float(sequence.times[-1] - sequence.times[0])
        sequence_lags, sequence_missed = detection_lags(
            sequence.truth, predicted, sequence.times)
        lags.extend(sequence_lags)
        missed += sequence_missed
    elapsed = time.perf_counter() - started

    hours = total_seconds / 3600.0
    rows = confusion.n
    return {
        "window": window,
        "min_agreement": min_agreement,
        "confusion": confusion,
        "transitions_per_hour": predicted_transitions / hours if hours else None,
        "truth_transitions_per_hour": truth_transitions / hours if hours else None,
        "median_lag_seconds": float(np.median(lags)) if lags else None,
        "p90_lag_seconds": float(np.percentile(lags, 90)) if lags else None,
        "missed_transitions": missed,
        "true_transitions": truth_transitions,
        "overhead_ms_per_slot": (elapsed * 1000.0 / rows) if rows else None,
    }


def grid() -> list[tuple[int, int]]:
    combinations = {(1, 1)}
    for window in SWEEP_GRID_M:
        for fraction in SWEEP_FRACTIONS:
            combinations.add((window, math.ceil(window * fraction)))
    return sorted(combinations)


def fmt(value, places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


# ---------------------------------------------------------------- modes ----
def run_sweep(split: str, max_gap_minutes: float) -> int:
    scores = load_scores(SCORE_METHOD, split)
    sequences, report = build_sequences(split, scores, max_gap_minutes)

    print(f"split                    {split}")
    print(f"rows in split            {report['rows']:,}")
    print(f"rows used                {report['used_rows']:,}")
    print(f"dropped: no score        {report['no_score']:,}")
    print(f"dropped: no timestamp    {report['no_timestamp']:,}")
    print(f"dropped: no slot_id      {report['no_slot_id']:,}")
    print(f"sequences                {report['sequences']:,}")
    print(f"gap breaks               {report['gap_breaks']:,} "
          f"(threshold {max_gap_minutes:.0f} min)")
    print(f"median frame interval    {report['median_interval_seconds'] / 60:.2f} min")
    if report["no_timestamp"] or report["no_slot_id"]:
        print("\nWARNING: rows without timestamp or slot_id are excluded, so the\n"
              "M=1 row below covers fewer patches than the Method B row in\n"
              "results.csv. Compare the counts before trusting the comparison.")
    print()

    results = [evaluate_combination(sequences, m, n) for m, n in grid()]
    baseline = results[0]
    base_flicker = baseline["transitions_per_hour"]

    header = (f"{'M':>3} {'N':>3} {'acc':>8} {'bal_acc':>8} {'F1':>8} "
              f"{'false_vac':>10} {'flick/h':>9} {'vs M=1':>8} "
              f"{'lag_p50':>8} {'lag_p90':>8} {'missed':>7}")
    lines = [header, "-" * len(header)]
    for result in results:
        confusion = result["confusion"]
        reduction = (
            "—" if result is baseline or not base_flicker
            else f"{1 - result['transitions_per_hour'] / base_flicker:+.1%}"
        )
        lines.append(
            f"{result['window']:>3} {result['min_agreement']:>3} "
            f"{fmt(confusion.accuracy):>8} {fmt(confusion.balanced_accuracy):>8} "
            f"{fmt(confusion.f1):>8} {fmt(confusion.false_vacancy_rate):>10} "
            f"{fmt(result['transitions_per_hour'], 2):>9} {reduction:>8} "
            f"{fmt(result['median_lag_seconds'], 0):>8} "
            f"{fmt(result['p90_lag_seconds'], 0):>8} "
            f"{result['missed_transitions']:>7}"
        )

    table = "\n".join(lines)
    print(table)
    print(f"\nground-truth transitions/slot/hour: "
          f"{fmt(baseline['truth_transitions_per_hour'], 3)}")
    print(f"true transitions in split:          {baseline['true_transitions']:,}")
    print("\nlag is in seconds. Pick by balanced accuracy, then flicker, with lag")
    print("as the constraint — a driver is not helped by a stable wrong answer.")
    print("Nothing is auto-selected. Re-run with --apply --m M --n N.")

    SMOOTHING_SWEEP_MD.parent.mkdir(parents=True, exist_ok=True)
    SMOOTHING_SWEEP_MD.write_text(
        "# Method C — N-of-M sweep\n\n"
        f"Split: `{split}` (validation; selection on a test split would leak)\n\n"
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
        f"at commit `{git_commit()}`\n\n"
        f"Causal window. Warm-up threshold scales as ceil((N/M)*w).\n"
        f"Max gap {max_gap_minutes:.0f} min; {report['gap_breaks']:,} breaks; "
        f"{report['sequences']:,} sequences over {report['used_rows']:,} patches.\n\n"
        f"Ground-truth transitions/slot/hour: "
        f"{fmt(baseline['truth_transitions_per_hour'], 3)}\n\n"
        f"```\n{table}\n```\n\n"
        "Regenerate with `python -m apms_ml.smoothing --sweep "
        f"--split {split}`.\n",
        encoding="utf-8",
    )
    print(f"\nwrote {SMOOTHING_SWEEP_MD}")
    return 0


def run_apply(splits: list[str], window: int, min_agreement: int,
              max_gap_minutes: float, write: bool, notes: str) -> int:
    if not 1 <= min_agreement <= window:
        raise SystemExit(f"need 1 <= N <= M, got N={min_agreement} M={window}")

    commit = git_commit()
    if write and commit.endswith("-dirty"):
        print(f"WARNING: dirty tree, recording commit as {commit}. "
              "This row is not reproducible until you commit.")

    for split in splits:
        scores = load_scores(SCORE_METHOD, split)
        sequences, report = build_sequences(split, scores, max_gap_minutes)
        result = evaluate_combination(sequences, window, min_agreement)
        confusion = result["confusion"]

        print(f"\n=== smoothed_c M={window} N={min_agreement} on {split} ===")
        print(f"  sequences {report['sequences']:,}  rows used "
              f"{report['used_rows']:,}/{report['rows']:,}")

        row = {
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "git_commit": commit,
            "method": "smoothed_c",
            "method_version": f"{SCORE_METHOD}+vote{min_agreement}of{window}",
            "split": split,
            "n_unreadable": report["rows"] - report["used_rows"],
            "latency_ms_per_slot_p50": None,
            "latency_ms_per_slot_p90": None,
            "latency_batch_size": None,
            "model_size_bytes": None,
            "host_cpu": host_cpu(),
            "smoothing_window_m": window,
            "smoothing_min_agreement_n": min_agreement,
            "n_sequences": report["sequences"],
            "transitions_per_slot_per_hour":
                None if result["transitions_per_hour"] is None
                else round(result["transitions_per_hour"], 4),
            "truth_transitions_per_slot_per_hour":
                None if result["truth_transitions_per_hour"] is None
                else round(result["truth_transitions_per_hour"], 4),
            "median_detection_lag_seconds":
                None if result["median_lag_seconds"] is None
                else round(result["median_lag_seconds"], 1),
            "smoothing_overhead_ms_per_slot":
                None if result["overhead_ms_per_slot"] is None
                else round(result["overhead_ms_per_slot"], 5),
            "notes": (notes or
                      f"inference latency is the {SCORE_METHOD} row for this split; "
                      f"this column covers the vote only"),
            **confusion.as_dict(),
        }
        for key in ("accuracy", "precision", "recall", "specificity", "f1",
                    "false_vacancy_rate", "miss_rate", "balanced_accuracy",
                    "occupied_share"):
            if row[key] is not None:
                row[key] = round(row[key], 6)

        width = max(len(k) for k in row)
        for key, value in row.items():
            print(f"  {key:<{width}}  {'' if value is None else value}")

        if write:
            append_result(row)
            print("  appended to results.csv")
        else:
            print("  NOT written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sweep", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--split", default="same_lot_val")
    parser.add_argument("--all-test", action="store_true")
    parser.add_argument("--m", type=int, help="window size M (required with --apply)")
    parser.add_argument("--n", type=int, help="min agreement N (required with --apply)")
    parser.add_argument("--max-gap-minutes", type=float,
                        default=DEFAULT_MAX_GAP_MINUTES)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    if args.sweep:
        if args.split.endswith("_test"):
            raise SystemExit(
                f"Refusing to sweep on {args.split}. Choosing N and M on a test "
                "split is selection leakage. Sweep on same_lot_val."
            )
        return run_sweep(args.split, args.max_gap_minutes)

    if args.m is None or args.n is None:
        raise SystemExit("--apply requires --m and --n from the sweep table.")
    splits = TEST_SPLITS if args.all_test else [args.split]
    return run_apply(splits, args.m, args.n, args.max_gap_minutes,
                     not args.no_write, args.notes)


if __name__ == "__main__":
    raise SystemExit(main())