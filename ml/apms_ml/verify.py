# ml/apms_ml/verify.py
"""Leak checks on the generated splits.

Run:  python -m apms_ml.verify

Exit code 0 means no day and no file appears on both sides of any boundary that
matters. A suspiciously high cross-lot accuracy later should send you here first.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .paths import DAY_ASSIGNMENTS_CSV, SPLITS_DIR
from .splits import CROSS_LOT, HELD_OUT_OF_VIEWS, SAME_LOT, WEATHER_VIEWS

DISJOINT = ["same_lot_train", "same_lot_val", "same_lot_test"]
failures: list[str] = []


def check(condition: bool, description: str) -> None:
    if condition:
        print(f"  PASS  {description}")
    else:
        print(f"  FAIL  {description}")
        failures.append(description)


def read_split(name: str) -> list[dict]:
    path = SPLITS_DIR / f"{name}.csv"
    if not path.exists():
        failures.append(f"{name}.csv missing")
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    if not DAY_ASSIGNMENTS_CSV.exists():
        raise SystemExit("day_assignments.csv missing — run `python -m apms_ml.splits`.")

    with open(DAY_ASSIGNMENTS_CSV, newline="", encoding="utf-8") as handle:
        assignments = list(csv.DictReader(handle))

    print("== Day assignment integrity ==")
    day_to_splits: dict[tuple, set] = defaultdict(set)
    for row in assignments:
        day_to_splits[(row["dataset"], row["lot"], row["day"])].add(row["split"])
    check(all(len(v) == 1 for v in day_to_splits.values()),
          "each (dataset, lot, day) has exactly one split")

    splits = {name: read_split(name) for name in DISJOINT}
    splits.update({name: read_split(name)
                   for name in ["cross_lot_test", "cross_dataset_test", *WEATHER_VIEWS]})

    print("\n== Same-lot disjointness ==")
    day_sets = {name: {(r["lot"], r["day"]) for r in splits[name]} for name in DISJOINT}
    path_sets = {name: {r["path"] for r in splits[name]} for name in DISJOINT}
    for i, left in enumerate(DISJOINT):
        for right in DISJOINT[i + 1:]:
            check(not day_sets[left] & day_sets[right],
                  f"no shared day between {left} and {right}")
            check(not path_sets[left] & path_sets[right],
                  f"no shared file between {left} and {right}")

    print("\n== Lot isolation ==")
    check(all(r["lot"] == SAME_LOT for name in DISJOINT for r in splits[name]),
          f"same_lot splits contain only {SAME_LOT}")
    check(all(r["lot"] == CROSS_LOT for r in splits["cross_lot_test"]),
          f"cross_lot_test contains only {CROSS_LOT}")
    check(all(r["dataset"] == "cnr_ext" for r in splits["cross_dataset_test"]),
          "cross_dataset_test contains only CNR-EXT")

    print("\n== Weather views ==")
    train_val_days = day_sets["same_lot_train"] | day_sets["same_lot_val"]
    for view, weather in WEATHER_VIEWS.items():
        rows = splits[view]
        check(all(r["weather"] == weather for r in rows),
              f"{view} contains only {weather}")
        check(not {(r["lot"], r["day"]) for r in rows} & train_val_days,
              f"{view} shares no day with training or validation")
        check(bool(rows), f"{view} is non-empty")

    print("\n== Labels and balance ==")
    for name, rows in splits.items():
        labels = {r["label"] for r in rows}
        if not labels <= {"0", "1"}:
            check(False, f"{name} has labels outside {{0,1}}: {sorted(labels)}")
            continue
        occupied = sum(1 for r in rows if r["label"] == "1")
        total = len(rows)
        share = occupied / total if total else 0.0
        print(f"  INFO  {name:<20} {total:>9,} rows, occupied share {share:.3f}")
        if total and not 0.05 < share < 0.95:
            print(f"        ^ heavily imbalanced — note it in RESULTS.md")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("All split checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())