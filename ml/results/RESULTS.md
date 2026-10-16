# ml/results/RESULTS.md
# APMS v2 — Phase 1 results

All numbers produced by `python -m apms_ml.evaluate`; raw rows in
`results/results.csv`. Host CPU: AMD Ryzen 5 5500U (6c/12t), ONNX Runtime,
CPU only. Latency measured at batch size 50.

**Reproducibility warning.** Every row currently in `results.csv` was recorded
with a dirty working tree and carries a `-dirty` commit suffix. They are not
reproducible as recorded. They are reported here because the measurements are
real, but the benchmark must be re-run from a clean tree before any of these
figures is cited externally. TBD: re-run and replace this paragraph.

## Methods

| ID | Method | Version | Model size |
|---|---|---|---|
| A | Fixed pixel-count threshold, port of the v1 heuristic | `v1-port-1` | 0 bytes |
| B | MobileNetV3-Small, ONNX, preprocessing in-graph | `mnv3s-pklot-20261011` | 6,094,260 bytes |
| C | Causal N-of-M vote over Method B scores | — | rejected, see below |

## Method B — accuracy by split

| Split | n | Balanced acc | Accuracy | Precision | Recall | Specificity | False vacancy |
|---|---:|---:|---:|---:|---:|---:|---:|
| `same_lot_test` | 11,441 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `cross_lot_test` | 424,223 | 0.9973 | 0.9973 | 0.9970 | 0.9970 | 0.9975 | 0.0025 |
| `cross_dataset_test` | 144,965 | 0.9315 | 0.9268 | 0.9829 | 0.8816 | 0.9814 | 0.1272 |

`same_lot_val` (model selection split, not a test result): n=17,018,
accuracy 0.999941, one false negative, false vacancy 9.2e-05.

### Reading these

`same_lot_test` returned zero errors across 11,441 patches. This is consistent
with the validation rate rather than surprising: at the val error rate of
5.9e-05 the expected error count in 11,441 patches is 0.67, so zero is the
most likely single outcome. It is not evidence of a leak — the split is
day-atomic, keyed on `(dataset, lot, day)`, assigned before any patch is
written (`apms_ml.splits`).

It is also not evidence of generalisation. Same-lot PKLot is close to
saturated: the Method A pixel threshold reaches 0.9449 balanced accuracy on
the same split. A near-perfect score here says the task is easy, not that the
model is strong.

`cross_lot_test` dropping only to 0.9973 is a weaker result than the name
suggests. PUC and UFPR04 are different lots within the same dataset — same
camera hardware, same city, same capture protocol. This measures a change of
viewpoint, not a change of domain.

`cross_dataset_test` (CNR-EXT) is the real generalisation number, and it is
where the model degrades: 9,391 false negatives, a 12.7% false vacancy rate.
Under `05_BENCHMARK_PROTOCOL.md` false vacancy is the costly error — a driver
directed to an occupied bay — so a 50-fold rise from cross-lot to
cross-dataset is the headline limitation of Method B as trained.

### Test-split coverage

`same_lot_test` is drawn from a small number of UFPR04 days. A near-perfect
score over few days of one fixed camera is a narrow claim.

TBD: day counts per split.
`awk -F, 'NR>1 {print $5}' splits/day_assignments.csv | sort | uniq -c`

## Method B — accuracy by weather

Weather views overlap the test splits by design and exclude every training and
validation day. They mix lots, so they are not comparable to the splits above.

| View | n | Balanced acc | Accuracy | False vacancy |
|---|---:|---:|---:|---:|
| `weather_sunny` | 314,521 | 0.9932 | 0.9932 | 0.0051 |
| `weather_cloudy` | 193,714 | 0.9981 | 0.9980 | 0.0007 |
| `weather_rainy` | 93,214 | 0.9970 | 0.9973 | 0.0042 |

Rain does not degrade Method B measurably on PKLot. Sunny is marginally the
worst of the three, plausibly shadow-related, but the spread is small enough
that no claim about weather robustness is warranted from this alone.

## Method A — baseline

| Split | n | Balanced acc | Accuracy | Precision | Recall | Specificity | False vacancy |
|---|---:|---:|---:|---:|---:|---:|---:|
| `same_lot_test` | 11,441 | 0.9494 | 0.9449 | 0.9813 | 0.9238 | 0.9750 | 0.1001 |

TBD: Method A on `cross_lot_test` and `cross_dataset_test`. The original run
predated the cross-lot split fix and stopped at an empty manifest.
`python -m apms_ml.evaluate --method threshold_a --all-test`

Method A exists to establish the floor. Its same-lot false vacancy rate of
10.0% is 40x Method B's cross-lot rate, which is the comparison that justifies
Method B's 6MB and 1.1ms.

## Latency

| Method | Split | p50 ms/slot | p90 ms/slot |
|---|---|---:|---:|
| A | `same_lot_test` | 0.1134 | 0.1174 |
| B | `same_lot_test` | 1.1044 | 1.1337 |
| B | `cross_lot_test` | 1.0921 | 1.1179 |
| B | `cross_dataset_test` | 1.0841 | 1.1014 |

Method B is stable at roughly 1.09ms per slot across 580,000 patches. At the
2 fps sampling target a 50-slot lot consumes about 55ms of each 500ms budget.
Method A is 10x faster and is not the bottleneck either; neither method
constrains the deployment target.

## Export parity

Verified with `python -m apms_ml.parity` against the downloaded ONNX file, on
a different runtime and CPU than the Colab export: