"""Walk both datasets and emit one row per labelled patch.

Run:  python -m apms_ml.index

Columns beyond the six named in 05_BENCHMARK_PROTOCOL.md:
  dataset   distinguishes pklot from cnr_ext; the splits need it
  day       the split unit; derived once here so it is derived identically everywhere
  slot_id   needed to reconstruct per-slot temporal sequences for Method C
"""
from __future__ import annotations

import csv
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .paths import (
    INDEX_CSV,
    SPLITS_DIR,
    cnr_ext_labels_root,
    cnr_ext_patches_root,
    pklot_segmented_root,
)

FIELDNAMES = [
    "dataset", "lot", "camera", "weather", "day",
    "capture_datetime", "slot_id", "label", "path",
]

PKLOT_CLASS_TO_LABEL = {"empty": 0, "occupied": 1}
PKLOT_STAMP = re.compile(
    r"(?P<d>\d{4}-\d{2}-\d{2})_(?P<h>\d{2})_(?P<mi>\d{2})_(?P<s>\d{2})#(?P<slot>\d+)"
)
CNR_STAMP = re.compile(
    r"^[SOR]_(?P<d>\d{4}-\d{2}-\d{2})_(?P<h>\d{2})\.(?P<mi>\d{2})"
    r"_C0(?P<cam>\d)_(?P<slot>\d+)\.jpg$"
)


def _walk_jpgs(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".jpg"):
                yield Path(dirpath) / name


def iter_pklot(root: Path, report: Counter):
    """PKLotSegmented/<LOT>/<WEATHER>/<DATE>/<Empty|Occupied>/<file>.jpg"""
    for path in _walk_jpgs(root):
        parts = path.relative_to(root).parts
        if len(parts) != 5:
            report["pklot_unexpected_layout"] += 1
            continue
        lot, weather, date_dir, class_dir, filename = parts
        label = PKLOT_CLASS_TO_LABEL.get(class_dir.lower())
        if label is None:
            report["pklot_unknown_class"] += 1
            continue
        match = PKLOT_STAMP.search(filename)
        if match:
            day = match["d"]
            captured = f"{match['d']}T{match['h']}:{match['mi']}:{match['s']}"
            slot_id = match["slot"].lstrip("0") or "0"
        else:
            report["pklot_unparsed_filename"] += 1
            day = date_dir
            captured = ""
            slot_id = ""
        yield {
            "dataset": "pklot",
            "lot": lot,
            "camera": lot,          # one fixed camera per PKLot subset
            "weather": weather,
            "day": day,
            "capture_datetime": captured,
            "slot_id": slot_id,
            "label": label,
            "path": str(path),
        }


def load_cnr_labels(labels_root: Path, report: Counter) -> dict[str, int]:
    """Union of the LABELS list files, keyed on basename.

    Format per cnrpark.it: '<IMAGE_PATH> <LABEL>', 0 = free, 1 = busy. The
    prefix of IMAGE_PATH is not documented, so basenames are used as keys and
    their uniqueness is verified below rather than assumed.
    """
    labels: dict[str, int] = {}
    conflicts = 0
    for list_file in sorted(labels_root.iterdir()):
        if not list_file.is_file():
            continue
        with open(list_file, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 2 or fields[-1] not in ("0", "1"):
                    report["cnr_label_line_malformed"] += 1
                    continue
                key = os.path.basename(fields[0])
                value = int(fields[-1])
                if key in labels and labels[key] != value:
                    conflicts += 1
                labels[key] = value
    report["cnr_label_conflicts"] = conflicts
    report["cnr_labels_loaded"] = len(labels)
    return labels


def iter_cnr_ext(root: Path, labels: dict[str, int], report: Counter):
    """PATCHES/<WEATHER>/<DATE>/camera<N>/<file>.jpg"""
    seen_basenames: set[str] = set()
    for path in _walk_jpgs(root):
        parts = path.relative_to(root).parts
        if len(parts) != 4:
            report["cnr_unexpected_layout"] += 1
            continue
        weather, date_dir, camera_dir, filename = parts
        if filename in seen_basenames:
            report["cnr_duplicate_basename"] += 1
        seen_basenames.add(filename)

        label = labels.get(filename)
        if label is None:
            report["cnr_unlabelled_dropped"] += 1
            continue

        match = CNR_STAMP.match(filename)
        if match:
            day = match["d"]
            captured = f"{match['d']}T{match['h']}:{match['mi']}:00"
            camera = f"camera{match['cam']}"
            slot_id = match["slot"]
        else:
            report["cnr_unparsed_filename"] += 1
            day = date_dir
            captured = ""
            camera = camera_dir
            slot_id = ""
        yield {
            "dataset": "cnr_ext",
            "lot": "CNR",
            "camera": camera,
            "weather": weather,
            "day": day,
            "capture_datetime": captured,
            "slot_id": slot_id,
            "label": label,
            "path": str(path),
        }


def main() -> int:
    SPLITS_DIR.mkdir(parents=True, exist_ok=True)
    report: Counter = Counter()
    per_group: dict[tuple, Counter] = defaultdict(Counter)
    written = 0

    pklot_root = pklot_segmented_root()
    cnr_root = cnr_ext_patches_root()
    cnr_labels = load_cnr_labels(cnr_ext_labels_root(), report)

    print(f"PKLot segmented : {pklot_root}")
    print(f"CNR-EXT patches : {cnr_root}")
    print(f"CNR-EXT labels  : {report['cnr_labels_loaded']} entries")
    print()

    with open(INDEX_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for source in (
            iter_pklot(pklot_root, report),
            iter_cnr_ext(cnr_root, cnr_labels, report),
        ):
            for row in source:
                writer.writerow(row)
                written += 1
                per_group[(row["dataset"], row["lot"], row["weather"])][row["label"]] += 1
                if written % 100_000 == 0:
                    print(f"  {written:,} rows")

    print(f"\nWrote {written:,} rows to {INDEX_CSV}")
    print(f"Size: {INDEX_CSV.stat().st_size / 1e6:.0f} MB (gitignored, regenerable)\n")

    print(f"{'dataset':<9} {'lot':<8} {'weather':<10} {'vacant':>9} {'occupied':>9} {'total':>9}")
    for key in sorted(per_group):
        counts = per_group[key]
        vacant, occupied = counts[0], counts[1]
        print(f"{key[0]:<9} {key[1]:<8} {key[2]:<10} "
              f"{vacant:>9,} {occupied:>9,} {vacant + occupied:>9,}")

    if report:
        print("\nAnomalies (report these, do not silence them):")
        for key in sorted(report):
            print(f"  {key:<28} {report[key]:,}")

    if written == 0:
        print("\nNo rows written.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())