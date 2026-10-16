# ml/apms_ml/report.py
"""Generate RESULTS.md from results.csv. RESULTS.md is never edited by hand.

Run:  python -m apms_ml.report           write ml/results/RESULTS.md
      python -m apms_ml.report --check   exit 1 if the committed file is stale

Enforced here rather than trusted:
  - every number comes from a results.csv row; a missing cell renders as TBD
    and the command that produces it is listed
  - rows stamped with a dirty or unknown commit are excluded
  - one method_version per method across all splits
  - one (M, N) for Method C across all splits
  - the M=1 smoothing row must reproduce Method B's confusion matrix whenever
    both cover the same patches
  - the split leak checks are re-run at generation time
  - Method A rows from an unverified port are marked and never presented as v1

Any failure above is fatal: RESULTS.md is not written.

Output is deterministic for a given results.csv and split set. No wall-clock
time is written, so --check compares byte for byte.

This file reports measurements. Interpretation belongs in the writeup.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
from collections import Counter
from datetime import datetime

from .evaluate import TEST_SPLITS
from .paths import REPO_ROOT, RESULTS_CSV, RESULTS_DIR, SPLITS_DIR
from .splits import SPLIT_SEED

RESULTS_MD = RESULTS_DIR / "RESULTS.md"
MODELS_DIR = REPO_ROOT / "models"

CORE_SPLITS = ["same_lot_test", "cross_lot_test", "cross_dataset_test"]
WEATHER_ROWS = [("Sunny", "weather_sunny"),
                ("Overcast", "weather_cloudy"),
                ("Rainy", "weather_rainy")]

TBD = "TBD"
DAGGER = "†"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

CMD_A = "python -m apms_ml.evaluate --method threshold_a --all-test"
CMD_B = "python -m apms_ml.evaluate --method mobilenet_b --all-test --dump-scores"
CMD_C1 = "python -m apms_ml.smoothing --apply --m 1 --n 1 --all-test"


# ------------------------------------------------------------ formatting ----
def num(row: dict | None, key: str) -> float | None:
    if row is None:
        return None
    value = row.get(key, "")
    return None if value in ("", None) else float(value)


def pct(value: float | None) -> str:
    return TBD if value is None else f"{value * 100:.2f}%"


def pp(value: float | None) -> str:
    return TBD if value is None else f"{value * 100:+.2f} pp"


def fixed(value: float | None, places: int) -> str:
    return TBD if value is None else f"{value:.{places}f}"


def megabytes(value: float | None) -> str:
    if value is None:
        return TBD
    return "0" if value == 0 else f"{value / 1e6:.2f} MB"


def md_table(headers: list[str], body: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join([":---"] + ["---:"] * (len(headers) - 1)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return lines


# ------------------------------------------------------------- selection ----
def stamp(row: dict) -> datetime:
    return datetime.strptime(row["run_at"], STAMP_FORMAT)


def load_rows() -> list[dict]:
    if not RESULTS_CSV.exists():
        raise SystemExit(f"{RESULTS_CSV} missing — run the evaluations first.")
    with open(RESULTS_CSV, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def select(rows: list[dict]) -> tuple[dict[tuple, dict], int]:
    """Latest clean row per key. Method C is keyed on (M, N) as well, so the
    M=1 baseline and the chosen pair coexist."""
    latest: dict[tuple, dict] = {}
    excluded = 0
    for row in rows:
        commit = row.get("git_commit", "")
        if commit.endswith("-dirty") or commit in ("", "unknown"):
            excluded += 1
            continue
        if row["method"] == "smoothed_c":
            key = ("smoothed_c", row["split"],
                   int(row["smoothing_window_m"]),
                   int(row["smoothing_min_agreement_n"]))
        else:
            key = (row["method"], row["split"])
        if key not in latest or stamp(row) > stamp(latest[key]):
            latest[key] = row
    return latest, excluded


def organise(latest: dict[tuple, dict]):
    a = {k[1]: r for k, r in latest.items() if k[0] == "threshold_a"}
    b = {k[1]: r for k, r in latest.items() if k[0] == "mobilenet_b"}
    c1 = {k[1]: r for k, r in latest.items() if k[0] == "smoothed_c" and k[2] == 1}
    cx: dict[str, dict] = {}
    for key, row in latest.items():
        if key[0] == "smoothed_c" and key[2] > 1:
            if key[1] not in cx or stamp(row) > stamp(cx[key[1]]):
                cx[key[1]] = row
    return a, b, c1, cx


# ---------------------------------------------------------------- checks ----
def run_leak_check() -> tuple[bool | None, str]:
    from . import verify

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = verify.main()
    except SystemExit as exc:
        return None, str(exc)
    return code == 0, buffer.getvalue()


def split_composition(split: str) -> str:
    path = SPLITS_DIR / f"{split}.csv"
    if not path.exists():
        return "split file absent"
    counts: Counter = Counter()
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            counts[row["lot"]] += 1
    return " · ".join(f"{lot} {n:,}" for lot, n in sorted(counts.items()))


def model_card(version: str | None) -> dict | None:
    if not version:
        return None
    path = MODELS_DIR / f"{version}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ----------------------------------------------------------------- build ----
def build(rows: list[dict]) -> tuple[str, list[str], list[str], int]:
    errors: list[str] = []
    notes: list[str] = []
    latest, excluded = select(rows)
    a, b, c1, cx = organise(latest)

    def single_version(label: str, group: dict) -> str | None:
        versions = sorted({r["method_version"] for r in group.values()})
        if len(versions) > 1:
            errors.append(f"{label}: rows from several versions {versions}. "
                          "Re-run that method on every split.")
        return versions[0] if len(versions) == 1 else None

    a_version = single_version("Method A", a)
    b_version = single_version("Method B", b)

    pairs = sorted({(int(r["smoothing_window_m"]), int(r["smoothing_min_agreement_n"]))
                    for r in cx.values()})
    if len(pairs) > 1:
        errors.append(f"Method C: splits disagree on (M, N) {pairs}. "
                      "Re-apply one pair to every split.")
    chosen = pairs[0] if len(pairs) == 1 else None
    a_verified = bool(a_version and a_version.endswith("-verified"))

    identical: list[str] = []
    for split in TEST_SPLITS:
        brow, crow = b.get(split), c1.get(split)
        if brow is None or crow is None:
            continue
        cells = ("tp", "fp", "tn", "fn")
        b_cm = tuple(int(brow[k]) for k in cells)
        c_cm = tuple(int(crow[k]) for k in cells)
        b_n, c_n = int(brow["n_samples"]), int(crow["n_samples"])
        if b_n == c_n:
            if b_cm != c_cm:
                errors.append(
                    f"{split}: M=1 confusion {c_cm} differs from Method B {b_cm} "
                    f"over the same {b_n:,} patches. Sequence reconstruction is "
                    "wrong; no Method C number can be trusted."
                )
            else:
                identical.append(split)
        else:
            notes.append(f"`{split}`: sequence reconstruction used {c_n:,} of "
                         f"{b_n:,} patches; the other {b_n - c_n:,} lack a "
                         "timestamp or slot id, so M=1 and B are not compared.")

    leak_ok, leak_log = run_leak_check()
    if leak_ok is None:
        errors.append(f"split leak checks could not run: {leak_log}")
    elif not leak_ok:
        failing = [line.strip() for line in leak_log.splitlines() if "FAIL" in line]
        errors.append("split leak checks failed: " + "; ".join(failing or ["see verify"]))

    m_text = str(chosen[0]) if chosen else "<M>"
    n_text = str(chosen[1]) if chosen else "<N>"
    cmd_cx = f"python -m apms_ml.smoothing --apply --m {m_text} --n {n_text} --all-test"
    missing: dict[str, list[str]] = {}
    for command, group, splits in ((CMD_A, a, CORE_SPLITS), (CMD_B, b, TEST_SPLITS),
                                   (CMD_C1, c1, TEST_SPLITS), (cmd_cx, cx, TEST_SPLITS)):
        absent = [s for s in splits if s not in group]
        if absent:
            missing[command] = absent

    label_a = "A: Threshold (v1)" + (f" {DAGGER}" if a and not a_verified else "")
    label_b = "B: MobileNetV3-S"
    label_c = f"C: B + {n_text}-of-{m_text} vote"

    def core(row: dict | None) -> list[str]:
        return [pct(num(row, k)) for k in ("accuracy", "balanced_accuracy",
                                           "precision", "recall", "f1",
                                           "false_vacancy_rate", "miss_rate")]

    def delta(group: dict, split: str) -> str:
        here = num(group.get(split), "balanced_accuracy")
        base = num(group.get("same_lot_test"), "balanced_accuracy")
        return pp(None if here is None or base is None else here - base)

    def c_latency(split: str) -> str:
        base = num(b.get(split), "latency_ms_per_slot_p50")
        extra = num(cx.get(split), "smoothing_overhead_ms_per_slot")
        return TBD if base is None or extra is None else f"{base:.3f} + {extra:.4f}"

    rendered = [*a.values(), *b.values(), *c1.values(), *cx.values()]
    as_of = max((r["run_at"] for r in rendered), key=lambda s: datetime.strptime(
        s, STAMP_FORMAT)) if rendered else "no rows"

    out: list[str] = [
        "# APMS v2 — Benchmark results",
        "",
        "> Generated by `python -m apms_ml.report` from `ml/results/results.csv`.",
        "> Do not edit — `python -m apms_ml.report --check` fails on any hand change.",
        f"> Rebuild every row: `./scripts/regenerate_results.sh {m_text} {n_text}`",
        "",
        f"Latest run included: `{as_of}`. Split seed: `{SPLIT_SEED}`.",
        "",
    ]

    if a and not a_verified:
        out += [
            f"> **{DAGGER} Method A is an unverified port.** Three constants in",
            "> `ml/apms_ml/methods/threshold_a.py` are marked VERIFY and have not",
            f"> been diffed against the v1 source. Rows marked {DAGGER} describe this",
            "> port, not necessarily what v1 achieved. Do not quote them as the v1",
            "> baseline.",
            "",
        ]

    out += ["## Test sets", ""]
    out += md_table(["Split", "Patches", "Occupied share"], [
        [f"`{s}`",
         TBD if (b.get(s) or a.get(s)) is None
         else f"{int((b.get(s) or a.get(s))['n_samples']):,}",
         pct(num(b.get(s) or a.get(s), "occupied_share"))]
        for s in TEST_SPLITS
    ])

    out += ["", "## Same-lot — train UFPR04, test UFPR04 held-out days", ""]
    out += md_table(
        ["Method", "Acc", "Bal. acc", "P", "R", "F1", "False vacancy",
         "Miss rate", "ms/slot (p50)", "Size"],
        [
            [label_a, *core(a.get("same_lot_test")),
             fixed(num(a.get("same_lot_test"), "latency_ms_per_slot_p50"), 3),
             megabytes(num(a.get("same_lot_test"), "model_size_bytes"))],
            [label_b, *core(b.get("same_lot_test")),
             fixed(num(b.get("same_lot_test"), "latency_ms_per_slot_p50"), 3),
             megabytes(num(b.get("same_lot_test"), "model_size_bytes"))],
            [label_c, *core(cx.get("same_lot_test")),
             c_latency("same_lot_test"),
             megabytes(num(b.get("same_lot_test"), "model_size_bytes"))],
        ],
    )

    out += ["", "## Cross-lot — train UFPR04, test PUCPR", ""]
    out += md_table(
        ["Method", "Acc", "Bal. acc", "P", "R", "F1", "False vacancy",
         "Miss rate", "Δ bal. acc vs same-lot"],
        [[label, *core(group.get("cross_lot_test")), delta(group, "cross_lot_test")]
         for label, group in ((label_a, a), (label_b, b), (label_c, cx))],
    )

    out += ["", "## Cross-dataset — train PKLot UFPR04, test CNR-EXT", ""]
    out += md_table(
        ["Method", "Acc", "Bal. acc", "F1", "False vacancy", "Δ bal. acc vs same-lot"],
        [[label,
          pct(num(group.get("cross_dataset_test"), "accuracy")),
          pct(num(group.get("cross_dataset_test"), "balanced_accuracy")),
          pct(num(group.get("cross_dataset_test"), "f1")),
          pct(num(group.get("cross_dataset_test"), "false_vacancy_rate")),
          delta(group, "cross_dataset_test")]
         for label, group in ((label_a, a), (label_b, b), (label_c, cx))],
    )

    out += [
        "", "## Weather — trained on UFPR04 only", "",
        "Each condition pools every PKLot day not used for training or validation,",
        "so a row mixes held-out UFPR04 days with PUCPR. Composition is shown; a",
        "difference between rows can reflect lot mix as well as weather.", "",
    ]
    out += md_table(
        ["Condition", "Composition", "B acc", "B bal. acc", "C acc", "C bal. acc"],
        [[name, split_composition(split),
          pct(num(b.get(split), "accuracy")), pct(num(b.get(split), "balanced_accuracy")),
          pct(num(cx.get(split), "accuracy")), pct(num(cx.get(split), "balanced_accuracy"))]
         for name, split in WEATHER_ROWS],
    )

    out += [
        "", "## Flicker and detection lag", "",
        "B is the M=1 row: the same code path as C with the vote disabled.", "",
    ]
    flicker_rows = []
    for split in TEST_SPLITS:
        truth = num(cx.get(split) or c1.get(split), "truth_transitions_per_slot_per_hour")
        base = num(c1.get(split), "transitions_per_slot_per_hour")
        smoothed = num(cx.get(split), "transitions_per_slot_per_hour")
        reduction = None if base in (None, 0) or smoothed is None else 1 - smoothed / base
        flicker_rows.append([
            f"`{split}`", fixed(truth, 3), fixed(base, 3), fixed(smoothed, 3),
            pct(reduction),
            fixed(num(c1.get(split), "median_detection_lag_seconds"), 0),
            fixed(num(cx.get(split), "median_detection_lag_seconds"), 0),
        ])
    out += md_table(["Split", "Ground truth /slot/h", "B /slot/h", "C /slot/h",
                     "Reduction", "B lag p50 (s)", "C lag p50 (s)"], flicker_rows)

    out += [
        "", "## Definitions", "",
        "- Positive class is OCCUPIED.",
        "- **False vacancy** = FN / (FN + TN): of slots reported vacant, the fraction",
        "  actually occupied. The driver-facing error.",
        "- **Miss rate** = FN / (FN + TP) = 1 − recall.",
        "- **Δ** is balanced accuracy in percentage points against the same method's",
        "  same-lot result. Balanced, because occupied share differs between lots and",
        "  plain accuracy moves with class balance alone.",
        "- **Latency** is single-threaded, 50-patch batches, median of 30 timed runs",
        "  after 5 warm-up batches, image decoding excluded. Method C's figure is",
        "  B's inference plus the measured cost of the vote.",
        "- **Flicker** is predicted state changes summed over per-slot sequences,",
        "  divided by summed sequence duration. Sequences break at gaps over 20 min.",
        "- **Lag** is seconds from a true transition to the first frame reflecting it.",
        "  The smoothing window is causal; no future frames are used.",
    ]

    out += ["", "## Consistency checks, run at generation time", ""]
    out.append("- Split leak checks (`python -m apms_ml.verify`): PASS")
    out.append(f"- One model version per method across splits: PASS")
    if identical:
        out.append(f"- M=1 smoothing reproduces Method B's confusion matrix exactly on "
                   f"{len(identical)} of {len(TEST_SPLITS)} test splits: "
                   + ", ".join(f"`{s}`" for s in identical))
    out += [f"- {note}" for note in notes]
    out.append(f"- Rows excluded for a dirty or unknown commit: {excluded}")

    out += ["", "## Provenance", ""]
    prov = []
    for label, group in ((label_a, a), (label_b, b), ("C: M=1 baseline", c1),
                         (label_c, cx)):
        versions = ", ".join(sorted({r["method_version"] for r in group.values()})) or TBD
        commits = ", ".join(f"`{c}`" for c in sorted({r["git_commit"]
                                                       for r in group.values()})) or TBD
        prov.append([label, f"`{versions}`" if group else TBD, commits])
    out += md_table(["Method", "Version", "Commits"], prov)

    card = model_card(b_version)
    if card:
        out += [
            "",
            f"Method B model `{card['version']}`: sha256 `{card['onnx_sha256']}`, "
            f"{card['onnx_bytes']:,} bytes, ONNX opset {card['onnx_opset']}, "
            f"exporter `{card['onnx_exporter']}`. Model card: "
            f"`models/{card['version']}.json`.",
        ]
    elif b_version:
        notes.append(f"no model card at models/{b_version}.json")
        out += ["", f"Method B model card `models/{b_version}.json` not found."]

    if missing:
        out += ["", "## Missing cells", "",
                "Every TBD above is produced by one of these commands:", ""]
        for command, splits in missing.items():
            out.append(f"- `{command}` — missing for "
                       + ", ".join(f"`{s}`" for s in splits))

    out.append("")
    tbd_count = sum(line.count(TBD) for line in out
                    if line.startswith("|") and not line.startswith("| Method |"))
    return "\n".join(out), errors, notes, tbd_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    content, errors, notes, tbd_count = build(load_rows())

    for note in notes:
        print(f"NOTE: {note}")
    if errors:
        print("\nRefusing to write RESULTS.md:")
        for error in errors:
            print(f"  - {error}")
        return 1

    if args.check:
        current = RESULTS_MD.read_text(encoding="utf-8") if RESULTS_MD.exists() else ""
        if current != content:
            print("RESULTS.md is stale or hand-edited. Run `python -m apms_ml.report`.")
            return 1
        print("RESULTS.md matches results.csv.")
        return 0

    RESULTS_MD.write_text(content, encoding="utf-8")
    print(f"wrote {RESULTS_MD}")
    print(f"TBD cells remaining: {tbd_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())