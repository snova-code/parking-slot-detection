"""Build the zip that goes to Google Drive.

Run:  python -m apms_ml.package_for_colab

Streams straight into the archive — no staging copy, so packing ~90k patches
costs one pass over the files instead of two. Stored uncompressed: JPEGs do not
deflate, and ZIP_STORED saves several minutes for roughly zero bytes.

Only same_lot_train and same_lot_val are packed. Test splits are evaluated
locally because latency must be measured on the target CPU.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import time
import zipfile
from collections import Counter
from pathlib import Path

from .paths import SPLITS_DIR, SUMMARY_MD, ML_DIR, data_root, pklot_segmented_root

PACKAGE_SPLITS = ["same_lot_train", "same_lot_val"]
TRAIN_SCRIPT = ML_DIR / "train" / "train_mobilenetv3.py"
MANIFEST_FIELDS = ["dataset", "lot", "camera", "weather", "day",
                   "capture_datetime", "slot_id", "label", "rel_path"]


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def read_split(split: str) -> list[dict]:
    path = SPLITS_DIR / f"{split}.csv"
    if not path.exists():
        raise SystemExit(f"{path} missing — run `python -m apms_ml.splits` first.")
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def cap_per_class(rows: list[dict], limit: int | None) -> list[dict]:
    if not limit:
        return rows
    kept: list[dict] = []
    seen: Counter = Counter()
    for row in rows:
        if seen[row["label"]] < limit:
            kept.append(row)
            seen[row["label"]] += 1
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=data_root() / "colab" / "apms_train_package.zip")
    parser.add_argument("--max-per-class", type=int, default=None,
                        help="smoke package: cap patches per class per split")
    args = parser.parse_args()

    if not TRAIN_SCRIPT.exists():
        raise SystemExit(f"{TRAIN_SCRIPT} missing — write it before packaging.")

    segmented_root = pklot_segmented_root()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    stats: dict[str, Counter] = {}
    total_files = 0
    started = time.perf_counter()

    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_STORED) as archive:
        for split in PACKAGE_SPLITS:
            rows = cap_per_class(read_split(split), args.max_per_class)
            counts: Counter = Counter()
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()

            for row in rows:
                source = Path(row["path"])
                try:
                    rel = source.relative_to(segmented_root)
                except ValueError:
                    raise SystemExit(
                        f"{source} is outside {segmented_root}. Only PKLot patches "
                        "can be packaged; regenerate splits."
                    )
                archive.write(source, f"patches/{rel.as_posix()}")
                writer.writerow({**{k: row[k] for k in MANIFEST_FIELDS[:-1]},
                                 "rel_path": rel.as_posix()})
                counts[row["label"]] += 1
                total_files += 1
                if total_files % 5000 == 0:
                    print(f"\r  {total_files:,} files packed", end="", flush=True)

            archive.writestr(f"manifests/{split}.csv", buffer.getvalue())
            stats[split] = counts

        archive.write(TRAIN_SCRIPT, "train_mobilenetv3.py")
        if SUMMARY_MD.exists():
            archive.write(SUMMARY_MD, "split_summary.md")

        package_info = {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "git_commit": git_commit(),
            "segmented_root": str(segmented_root),
            "max_per_class": args.max_per_class,
            "counts": {s: {"vacant": c["0"], "occupied": c["1"]} for s, c in stats.items()},
        }
        archive.writestr("PACKAGE.json", json.dumps(package_info, indent=2))

    print(f"\r  {total_files:,} files packed")
    size_mb = args.out.stat().st_size / 1e6
    print(f"\n{args.out}")
    print(f"  {size_mb:,.0f} MB, {time.perf_counter() - started:.0f}s")
    for split, counts in stats.items():
        print(f"  {split:<16} vacant={counts['0']:,} occupied={counts['1']:,}")
    if package_info["git_commit"].endswith("-dirty"):
        print("\nWARNING: dirty tree. The model card will record an unreproducible commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())