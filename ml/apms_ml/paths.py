"""Filesystem locations.

No absolute path is hardcoded. Everything derives from APMS_DATA_ROOT
(default ~/data) and the repository root, so the same code runs on the laptop,
in Colab, and on the VPS.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ML_DIR = REPO_ROOT / "ml"
SPLITS_DIR = ML_DIR / "splits"
INDEX_CSV = SPLITS_DIR / "index.csv"
DAY_ASSIGNMENTS_CSV = SPLITS_DIR / "day_assignments.csv"
SUMMARY_MD = SPLITS_DIR / "split_summary.md"


def data_root() -> Path:
    root = Path(os.environ.get("APMS_DATA_ROOT", Path.home() / "data")).expanduser()
    resolved = root.resolve()
    if str(resolved).startswith("/mnt/"):
        raise RuntimeError(
            f"APMS_DATA_ROOT resolves to {resolved}, on a Windows drive. "
            "Reading 840k small files across the 9P layer takes hours. "
            "Move the data into the WSL2 ext4 filesystem."
        )
    return resolved


def _find_dir(base: Path, name: str, max_depth: int = 4) -> Path | None:
    """Bounded breadth-limited search. Depth cap prevents descending into the
    700k-file tree when the target is missing."""
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == name:
                return Path(entry.path)
            stack.append((Path(entry.path), depth + 1))
    return None


def _require(found: Path | None, what: str, hint: str) -> Path:
    if found is None:
        raise SystemExit(f"Could not locate {what}.\n{hint}")
    return found


def pklot_segmented_root() -> Path:
    return _require(
        _find_dir(data_root() / "pklot", "PKLotSegmented"),
        "PKLotSegmented/ under ~/data/pklot",
        "The archive did not contain a pre-segmented tree. Patches must be cut\n"
        "from the full frames plus their XML rotated rectangles. That is a\n"
        "separate task, not part of Task 1.1 — stop and scope it before\n"
        "continuing, and record the decision in the session log.",
    )


def cnr_ext_patches_root() -> Path:
    return _require(
        _find_dir(data_root() / "cnrpark", "PATCHES"),
        "PATCHES/ under ~/data/cnrpark",
        "Run scripts/extract_datasets.sh first.",
    )


def cnr_ext_labels_root() -> Path:
    return _require(
        _find_dir(data_root() / "cnrpark", "LABELS"),
        "LABELS/ under ~/data/cnrpark",
        "CNR-EXT patch labels live in list files, not in the directory path.\n"
        "Without them the cross-dataset test set cannot be labelled.",
    )