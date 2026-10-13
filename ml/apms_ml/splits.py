# ml/apms_ml/splits.py
"""Seeded, day-atomic split construction.

Run:  python -m apms_ml.splits

Two passes over index.csv. Pass one aggregates to per-day counts; pass two
routes rows into the output files. The index is never held in memory.

Days are shuffled from a *sorted* list so filesystem ordering cannot affect the
result. Assignment is stratified by each day's dominant weather, so rainy days
cannot all land on one side of the split.
"""
from __future__ import annotations

import csv
import random
from collections import defaultdict
from pathlib import Path

from .paths import DAY_ASSIGNMENTS_CSV, INDEX_CSV, SPLITS_DIR, SUMMARY_MD
# ml/apms_ml/splits.py  — replace the constants block

SPLIT_SEED = 20260101
SAME_LOT = "UFPR04"
CROSS_LOT = "PUC"          # PKLotSegmented names it PUC, not PUCPR
HELD_OUT_LOTS = {"UFPR05"} # present in PKLot, deliberately unused in Phase 1
TRAIN_FRAC = 0.70
VAL_FRAC = 0.15

# PKLot names the condition "Cloudy"; the protocol calls it overcast. The file
# name follows the protocol, the data keeps its own label.
WEATHER_VIEWS = {
    "weather_sunny": "Sunny",
    "weather_cloudy": "Cloudy",
    "weather_rainy": "Rainy",
}
HELD_OUT_OF_VIEWS = {"same_lot_train", "same_lot_val"}


def aggregate_days(index_csv: Path) -> dict[tuple[str, str, str], dict]:
    days: dict[tuple[str, str, str], dict] = {}
    with open(index_csv, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (row["dataset"], row["lot"], row["day"])
            entry = days.get(key)
            if entry is None:
                entry = days[key] = {"n": 0, "occupied": 0, "weather": defaultdict(int)}
            entry["n"] += 1
            entry["occupied"] += int(row["label"])
            entry["weather"][row["weather"]] += 1
    return days


def _dominant_weather(entry: dict) -> str:
    return max(sorted(entry["weather"]), key=lambda w: entry["weather"][w])


def _cut_at(days: list[tuple], total: int, frac: float) -> int:
    accumulated = 0
    for position, (_key, count) in enumerate(days):
        accumulated += count
        if accumulated >= frac * total:
            return position + 1
    return len(days)


def assign_same_lot(day_items: list[tuple[tuple, int, str]], rng: random.Random):
    """Stratify by weather, then cut each stratum by cumulative patch count."""
    strata: dict[str, list[tuple]] = defaultdict(list)
    for key, count, weather in day_items:
        strata[weather].append((key, count))

    assignment: dict[tuple, str] = {}
    notes: list[str] = []

    for weather in sorted(strata):
        days = sorted(strata[weather])
        rng.shuffle(days)
        total = sum(count for _key, count in days)

        if len(days) == 1:
            assignment[days[0][0]] = "same_lot_train"
            notes.append(f"{weather}: 1 day only — all to train; no {weather} in val or test")
            continue
        if len(days) == 2:
            assignment[days[0][0]] = "same_lot_train"
            assignment[days[1][0]] = "same_lot_test"
            notes.append(f"{weather}: 2 days only — train and test; no {weather} in val")
            continue

        first = min(max(_cut_at(days, total, TRAIN_FRAC), 1), len(days) - 2)
        second = min(max(_cut_at(days, total, TRAIN_FRAC + VAL_FRAC), first + 1), len(days) - 1)

        for key, _count in days[:first]:
            assignment[key] = "same_lot_train"
        for key, _count in days[first:second]:
            assignment[key] = "same_lot_val"
        for key, _count in days[second:]:
            assignment[key] = "same_lot_test"

        achieved = sum(c for _k, c in days[:first]) / total
        notes.append(
            f"{weather}: {len(days)} days, {total:,} patches; "
            f"train share {achieved:.3f} (target {TRAIN_FRAC:.2f})"
        )

    return assignment, notes

def build_assignments(days: dict) -> tuple[dict, list[str]]:
    rng = random.Random(SPLIT_SEED)
    assignment: dict[tuple, str] = {}

    lots_in_index = {key[1] for key in days}
    for required in (SAME_LOT, CROSS_LOT):
        if required not in lots_in_index:
            raise SystemExit(
                f"Lot {required!r} is not in the index. Lots present: "
                f"{sorted(lots_in_index)}. Fix the constant, do not rename the data."
            )

    same_lot_items = [
        (key, entry["n"], _dominant_weather(entry))
        for key, entry in days.items()
        if key[0] == "pklot" and key[1] == SAME_LOT
    ]
    same_lot_assignment, notes = assign_same_lot(same_lot_items, rng)
    assignment.update(same_lot_assignment)

    for key in days:
        if key in assignment:
            continue
        if key[0] == "cnr_ext":
            assignment[key] = "cross_dataset_test"
        elif key[1] == CROSS_LOT:
            assignment[key] = "cross_lot_test"
        elif key[1] in HELD_OUT_LOTS:
            assignment[key] = "held_out"
        else:
            raise SystemExit(
                f"Day {key} has lot {key[1]!r}, which matches no split rule. "
                "Add it to HELD_OUT_LOTS deliberately or give it a split."
            )

    return assignment, notes

def write_assignments(days: dict, assignment: dict) -> None:
    with open(DAY_ASSIGNMENTS_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["dataset", "lot", "day", "dominant_weather",
                         "split", "n_patches", "n_occupied"])
        for key in sorted(days):
            entry = days[key]
            writer.writerow([*key, _dominant_weather(entry), assignment[key],
                             entry["n"], entry["occupied"]])


def materialise(index_csv: Path, assignment: dict) -> dict[str, dict[int, int]]:
    """Pass two. Weather views are written alongside the disjoint splits and
    deliberately overlap the test splits; they exclude any training or
    validation day so they cannot leak."""
    names = ["same_lot_train", "same_lot_val", "same_lot_test",
             "cross_lot_test", "cross_dataset_test", *WEATHER_VIEWS]
    counts: dict[str, dict[int, int]] = {name: {0: 0, 1: 0} for name in names}
    handles, writers = {}, {}

    with open(index_csv, newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames or []
        try:
            for name in names:
                handles[name] = open(SPLITS_DIR / f"{name}.csv", "w",
                                     newline="", encoding="utf-8")
                writers[name] = csv.DictWriter(handles[name], fieldnames=fieldnames)
                writers[name].writeheader()

            for row in reader:
                split = assignment[(row["dataset"], row["lot"], row["day"])]
                label = int(row["label"])
                if split in writers:
                    writers[split].writerow(row)
                    counts[split][label] += 1
                if row["dataset"] == "pklot" and split not in HELD_OUT_OF_VIEWS:
                    for view, weather in WEATHER_VIEWS.items():
                        if row["weather"] == weather:
                            writers[view].writerow(row)
                            counts[view][label] += 1
        finally:
            for handle in handles.values():
                handle.close()
    return counts


def write_summary(counts: dict, days: dict, assignment: dict, notes: list[str]) -> None:
    per_split_days: dict[str, int] = defaultdict(int)
    for key in days:
        per_split_days[assignment[key]] += 1

    lines = [
        "# APMS v2 — split summary",
        "",
        f"Seed: `{SPLIT_SEED}`. Split unit: `(lot, date)`, weather ignored when",
        "forming the unit, used only to stratify assignment.",
        "",
        "Regenerate with `python -m apms_ml.splits`; verify with",
        "`python -m apms_ml.verify`. Only `day_assignments.csv` and this file are",
        "committed — the per-split CSVs are derived and gitignored.",
        "",
        "| Split | Days | Vacant | Occupied | Total | Occupied share |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ["same_lot_train", "same_lot_val", "same_lot_test",
                 "cross_lot_test", "cross_dataset_test", *WEATHER_VIEWS]:
        vacant, occupied = counts[name][0], counts[name][1]
        total = vacant + occupied
        share = f"{occupied / total:.3f}" if total else "n/a"
        lines.append(f"| `{name}` | {per_split_days.get(name, 0)} | "
                     f"{vacant:,} | {occupied:,} | {total:,} | {share} |")

    lines += [
        "",
        f"Days held out of every split (`unassigned`): {per_split_days.get('unassigned', 0)}",
        "",
        "Weather views overlap the test splits by design and exclude every",
        "training and validation day. They carry a `lot` column, so a mixed",
        "same-lot / cross-lot population stays visible in the evaluation.",
        "",
        "## Stratification notes",
        "",
    ]
    lines += [f"- {note}" for note in notes]
    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if not INDEX_CSV.exists():
        raise SystemExit(f"{INDEX_CSV} missing — run `python -m apms_ml.index` first.")

    print("Pass 1: aggregating days...")
    days = aggregate_days(INDEX_CSV)
    print(f"  {len(days):,} distinct (dataset, lot, day) groups")

    assignment, notes = build_assignments(days)
    write_assignments(days, assignment)

    print("Pass 2: writing split files...")
    counts = materialise(INDEX_CSV, assignment)
    write_summary(counts, days, assignment, notes)

    print(f"\n{SUMMARY_MD.read_text(encoding='utf-8')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())