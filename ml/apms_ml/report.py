# ml/apms_ml/report.py
"""Generate RESULTS.md from results.csv. Never hand-edit RESULTS.md.

Run:  python -m apms_ml.report

Every table in the output is derived from results/results.csv and
splits/day_assignments.csv. Interpretation lives in RESULTS_PREAMBLE.md, which
is hand-written and copied in verbatim at the top. The split is deliberate: a
script cannot judge whether a number means anything, and a human should not be
retyping numbers.

Refuses to emit if any row carries a dirty commit, so a reported figure always
traces to a commit that exists.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

from .paths import DAY_ASSIGNMENTS_CSV, RESULTS_CSV, SPLITS_DIR

HERE = Path(__file__).resolve().parent
RESULTS_DIR = RESULTS_CSV.parent
PREAMBLE_MD = RESULTS_DIR / "RESULTS_PREAMBLE.md"
OUT_MD = RESULTS_DIR / "RESULTS.md"

# Splits in reporting order. Anything in the CSV but not here is still reported,
# appended after these, so a new split cannot be silently dropped.
TEST_SPLITS = ["same_lot_test", "cross_lot_test", "cross_dataset_test"]
WEATHER_SPLITS = ["weather_sunny", "weather_cloudy", "weather_rainy"]

METHOD_LABELS = {"threshold_a": "A", "mobilenet_b": "B"}

REQUIRED_COLUMNS = [
    "run_at", "git_commit", "method", "method_version", "split",
    "n_samples", "tp", "fp", "tn", "fn", "occupied_share",
    "accuracy", "precision", "recall", "specificity", "f1",
    "false_vacancy_rate", "balanced_accuracy",
    "latency_ms_per_slot_p50", "latency_ms_per_slot_p90",
    "latency_batch_size", "model_size_bytes", "host_cpu",
]


def load_rows() -> list[dict]:
    if not RESULTS_CSV.exists():
        raise SystemExit(f"{RESULTS_CSV} missing — run `apms_ml.evaluate --all-test` first.")

    with open(RESULTS_CSV, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"{RESULTS_CSV} is missing columns: {missing}\n"
                "The column set changed. Regenerate results.csv rather than patching it."
            )
        rows = list(reader)

    if not rows:
        raise SystemExit(f"{RESULTS_CSV} has a header but no rows.")

    dirty = sorted({r["git_commit"] for r in rows if "dirty" in r["git_commit"]})
    if dirty:
        raise SystemExit(
            "Refusing to generate RESULTS.md: these commits are dirty and the "
            f"rows are not reproducible: {dirty}\n"
            "Commit, delete results.csv, and re-run the benchmark."
        )
    return rows


def latest_per_key(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """One row per (method, split). Later run_at wins, so a re-run supersedes
    an earlier one without needing the CSV to be edited."""
    best: dict[tuple[str, str], dict] = {}
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        key = (row["method"], row["split"])
        seen[key] += 1
        if key not in best or row["run_at"] > best[key]["run_at"]:
            best[key] = row
    for key, count in sorted(seen.items()):
        if count > 1:
            print(f"  note: {count} rows for {key[0]} on {key[1]}; using latest "
                  f"({best[key]['run_at']})", file=sys.stderr)
    return best


def f(row: dict, column: str, places: int = 4) -> str:
    raw = (row.get(column) or "").strip()
    if not raw:
        return "—"
    try:
        return f"{float(raw):.{places}f}"
    except ValueError:
        return raw


def i(row: dict, column: str) -> str:
    raw = (row.get(column) or "").strip()
    if not raw:
        return "—"
    try:
        return f"{int(float(raw)):,}"
    except ValueError:
        return raw


def day_counts() -> dict[str, int]:
    if not DAY_ASSIGNMENTS_CSV.exists():
        return {}
    counts: dict[str, int] = defaultdict(int)
    with open(DAY_ASSIGNMENTS_CSV, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            counts[row["split"]] += 1
    return dict(counts)


def split_sizes() -> dict[str, int]:
    """Patch counts straight off the split manifests, so the day table's
    patches-per-day column is measured rather than inferred."""
    sizes: dict[str, int] = {}
    for path in sorted(SPLITS_DIR.glob("*.csv")):
        name = path.stem
        if name in ("index", "day_assignments"):
            continue
        with open(path, newline="", encoding="utf-8") as handle:
            sizes[name] = max(sum(1 for _ in handle) - 1, 0)
    return sizes


def table(header: list[str], aligns: list[str], body: list[list[str]]) -> list[str]:
    sep = []
    for a in aligns:
        sep.append("---:" if a == "r" else ("---" if a == "l" else ":---:"))
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(sep) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return lines + [""]


def ordered_splits(best: dict, preferred: list[str]) -> list[str]:
    present = {split for _method, split in best}
    extra = sorted(s for s in present if s not in TEST_SPLITS + WEATHER_SPLITS)
    return [s for s in preferred if s in present] + (
        extra if preferred is TEST_SPLITS else [])


def main() -> int:
    rows = load_rows()
    best = latest_per_key(rows)
    days = day_counts()
    sizes = split_sizes()

    methods = [m for m in ("threshold_a", "mobilenet_b")
               if any(k[0] == m for k in best)]
    other = sorted({k[0] for k in best} - set(methods))
    methods += other

    out: list[str] = []

    if PREAMBLE_MD.exists():
        out.append(PREAMBLE_MD.read_text(encoding="utf-8").rstrip())
        out.append("")
    else:
        out += ["# APMS v2 — Phase 1 results", "",
                f"`{PREAMBLE_MD.name}` not found; tables only.", ""]

    out += ["<!-- Everything below this line is generated by apms_ml.report.",
            "     Edit RESULTS_PREAMBLE.md or the CSV, never this file. -->", ""]

    # ---- provenance -------------------------------------------------------
    commits = sorted({r["git_commit"] for r in best.values()})
    hosts = sorted({r["host_cpu"] for r in best.values()})
    batches = sorted({r["latency_batch_size"] for r in best.values()})
    runs = sorted(r["run_at"] for r in best.values())

    out += ["## Provenance", ""]
    out += table(
        ["Field", "Value"], ["l", "l"],
        [["rows reported", str(len(best))],
         ["commit(s)", ", ".join(f"`{c}`" for c in commits)],
         ["first run", runs[0]],
         ["last run", runs[-1]],
         ["host CPU", "; ".join(hosts)],
         ["latency batch size", ", ".join(batches)],
         ["source", f"`{RESULTS_CSV.name}`, generated by `apms_ml.report`"]])

    # ---- methods ----------------------------------------------------------
    out += ["## Methods", ""]
    body = []
    for method in methods:
        row = next(r for (m, _s), r in best.items() if m == method)
        body.append([METHOD_LABELS.get(method, "—"), f"`{method}`",
                     f"`{row['method_version']}`", i(row, "model_size_bytes") + " bytes"])
    out += table(["ID", "Method", "Version", "Model size"],
                 ["l", "l", "l", "r"], body)

    # ---- head to head -----------------------------------------------------
    test_present = [s for s in TEST_SPLITS if any(k[1] == s for k in best)]
    if len(methods) > 1 and test_present:
        out += ["## Head-to-head: balanced accuracy", ""]
        header = ["Split", "n"] + [f"{METHOD_LABELS.get(m, m)}" for m in methods]
        body = []
        for split in test_present:
            row0 = next((best[(m, split)] for m in methods if (m, split) in best), None)
            line = [f"`{split}`", i(row0, "n_samples") if row0 else "—"]
            for method in methods:
                r = best.get((method, split))
                line.append(f(r, "balanced_accuracy") if r else "—")
            body.append(line)
        out += table(header, ["l", "r"] + ["r"] * len(methods), body)

        out += ["## Head-to-head: false vacancy rate", "",
                "Lower is better. False vacancy is a driver sent to an occupied bay.", ""]
        body = []
        for split in test_present:
            line = [f"`{split}`"]
            for method in methods:
                r = best.get((method, split))
                line.append(f(r, "false_vacancy_rate") if r else "—")
            body.append(line)
        out += table(["Split"] + [METHOD_LABELS.get(m, m) for m in methods],
                     ["l"] + ["r"] * len(methods), body)

    # ---- per method full tables -------------------------------------------
    for method in methods:
        label = METHOD_LABELS.get(method, method)
        splits = [s for s in TEST_SPLITS + WEATHER_SPLITS if (method, s) in best]
        extra = sorted(s for (m, s) in best
                       if m == method and s not in TEST_SPLITS + WEATHER_SPLITS)
        if not splits and not extra:
            continue
        out += [f"## Method {label} (`{method}`) — all splits", ""]
        body = []
        for split in splits + extra:
            r = best[(method, split)]
            body.append([
                f"`{split}`", i(r, "n_samples"), f(r, "occupied_share"),
                i(r, "tp"), i(r, "fp"), i(r, "tn"), i(r, "fn"),
                f(r, "balanced_accuracy"), f(r, "accuracy"), f(r, "precision"),
                f(r, "recall"), f(r, "specificity"), f(r, "false_vacancy_rate"),
            ])
        out += table(
            ["Split", "n", "Occ share", "TP", "FP", "TN", "FN",
             "Bal acc", "Acc", "Prec", "Recall", "Spec", "False vac"],
            ["l"] + ["r"] * 12, body)

    # ---- latency ----------------------------------------------------------
    out += ["## Latency", ""]
    body = []
    for method in methods:
        for split in [s for s in TEST_SPLITS + WEATHER_SPLITS if (method, s) in best]:
            r = best[(method, split)]
            body.append([METHOD_LABELS.get(method, method), f"`{split}`",
                         f(r, "latency_ms_per_slot_p50"),
                         f(r, "latency_ms_per_slot_p90"),
                         r["latency_batch_size"]])
    out += table(["Method", "Split", "p50 ms/slot", "p90 ms/slot", "Batch"],
                 ["l", "l", "r", "r", "r"], body)

    # ---- day coverage -----------------------------------------------------
    if days:
        out += ["## Split day coverage", "",
                "Splits are day-atomic; the unit is `(dataset, lot, day)`.", ""]
        body = []
        for split in sorted(days, key=lambda s: (-days[s], s)):
            n = sizes.get(split)
            per = f"{n // days[split]:,}" if n and days[split] else "—"
            body.append([f"`{split}`", f"{days[split]:,}",
                         f"{n:,}" if n is not None else "—", per])
        out += table(["Split", "Days", "Patches", "Patches/day"],
                     ["l", "r", "r", "r"], body)

    # ---- smoothing --------------------------------------------------------
    smoothed = {k: r for k, r in best.items()
                if (r.get("smoothing_window_m") or "").strip()}
    out += ["## Method C — temporal smoothing", ""]
    if smoothed:
        body = []
        for (method, split), r in sorted(smoothed.items()):
            body.append([f"`{method}`", f"`{split}`",
                         r["smoothing_window_m"], r["smoothing_min_agreement_n"],
                         f(r, "balanced_accuracy"), f(r, "false_vacancy_rate"),
                         f(r, "transitions_per_slot_per_hour", 3),
                         f(r, "truth_transitions_per_slot_per_hour", 3),
                         f(r, "median_detection_lag_seconds", 0)])
        out += table(["Method", "Split", "M", "N", "Bal acc", "False vac",
                      "Flicker/h", "Truth/h", "Lag p50 s"],
                     ["l", "l", "r", "r", "r", "r", "r", "r", "r"], body)
    else:
        out += ["No smoothed rows in `results.csv`: smoothing was swept and not",
                "applied. The sweep is the evidence; see `smoothing_sweep.md` and",
                "the Method C section of the preamble.", ""]

    OUT_MD.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {OUT_MD}")
    print(f"  {len(best)} rows, {len(methods)} methods, commits: {', '.join(commits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())