# APMS v2 — Benchmark Protocol

This phase exists to replace an unsupported claim ("CNN and SVM") with a measured
result. It is the single most valuable deliverable in the project.

**Rule:** no number enters a document unless a script in `ml/` produced it and
you can re-run that script.

---

## Datasets

### PKLot
Parking lots in Brazil (UFPR04, UFPR05, PUCPR), roughly 700k labelled slot
patches across sunny, overcast and rainy conditions. Use the **segmented**
version — pre-cropped patches, not full frames.

### CNRPark-EXT
Nine cameras in Italy, deliberately including partial occlusion, shadow and
varying viewpoints. Use the **patches** distribution.

Verify archive sizes and checksums on the download pages before extracting.
Record the file counts you actually get — you will cite them in the writeup, and
they should match the published figures. If they do not, say so.

Store under `~/data/` **inside the WSL2 filesystem**, never under `/mnt/c/`.
See `06_ENV_SETUP.md` for why this matters enormously.

---

## Splits

Write manifests to CSV once, seeded, and never re-shuffle. Every method reads the
same manifest. This is what makes the comparison valid.

```
ml/splits/
  same_lot_train.csv     PKLot UFPR04, 70%
  same_lot_val.csv       PKLot UFPR04, 15%
  same_lot_test.csv      PKLot UFPR04, 15%
  cross_lot_test.csv     PKLot PUCPR, 100%       (never seen in training)
  cross_dataset_test.csv CNRPark-EXT, 100%       (different country, cameras, climate)
  weather_rainy.csv      PKLot rainy subset
  weather_cloudy.csv     PKLot overcast subset
```

Columns: `path,label,lot,camera,weather,capture_datetime`.

**Split by day, not by image.** Images from the same day and camera are highly
correlated; a random image split leaks and inflates accuracy by several points.
Getting this right is itself a point worth making in the writeup.

---

## Methods to compare

### A — Threshold baseline (v1, unchanged)
Port the original rule exactly: grayscale → `GaussianBlur(3,3)` →
`adaptiveThreshold(255, GAUSSIAN_C, BINARY_INV, 25, 16)` → `medianBlur(3)` →
`dilate(3×3)` → count zero pixels → `count < 660` means occupied.

Do not tune it. The point is to show what the original actually achieved. If it
performs poorly on cross-lot data, that is the result.

Note in the writeup that 660 is scale-dependent — it was calibrated for 40×25
boxes at 640×480 and does not transfer. Resize patches to that reference scale so
the comparison is at least fair to the baseline.

### B — MobileNetV3-Small classifier
Binary classification per slot patch. Input 96×96. Trained on Colab.

- ImageNet-pretrained backbone, replaced head
- Augmentation: random brightness/contrast, small rotation (±10°), horizontal flip
- Class-balanced sampling
- Early stopping on `same_lot_val`
- Export to ONNX opset 17, verify parity between PyTorch and ONNX outputs on 100
  samples before trusting it

### C — B + temporal smoothing

Causal N-of-M majority vote: the decision at frame i covers frames [i-M+1, i].
A centered window scores better and cannot run in the worker, which has no
future frames. Only the causal form is reported.

Sweep M ∈ {5, 9, 15}, N = ceil(M × {0.6, 0.7, 0.8}) on **same_lot_val**, not on
a test split. Choosing N and M on test and then reporting Method C on that same
test set is selection leakage. Once chosen, one (N, M) pair is applied unchanged
to every test split.

During warm-up the requirement scales to ceil((N/M) × w) for window length w, so
M=1, N=1 is exactly Method B. That row is generated through the same code path
and must reproduce Method B's confusion matrix; a mismatch means sequence
reconstruction is dropping rows.

Sequences are (dataset, lot, camera, slot_id, day) ordered by capture time,
broken wherever consecutive frames are more than 20 minutes apart. Break counts
and the median frame interval are reported with the sweep.

Report three numbers, not two:

- **Flicker**: predicted state changes summed over sequences, divided by summed
  sequence duration in hours. Ground-truth transitions per slot per hour is
  reported alongside as the floor — flicker below it means the smoother is
  suppressing real arrivals.
- **Accuracy change** against the M=1 row.
- **Detection lag**: median and p90 seconds from a true transition to the first
  frame reflecting it, plus transitions never reflected at all. Smoothing buys
  stability with responsiveness and the flicker number alone hides the cost.

### D — YOLOv8n + polygon IoU (stretch, cut first)
Detect vehicles, assign to slots by IoU against polygons. Only attempt if Phase 1
finishes early. Its absence is legitimate future work.

---

## Metrics

Per method × per test set:

| Metric | Why |
|---|---|
| Accuracy | headline, but insufficient alone |
| Precision / Recall / F1 (occupied = positive) | class balance varies by lot |
| **False vacancy rate** | a driver sent to an occupied bay — the costly error |
| Latency ms/slot on Ryzen 5 5500U, single thread | deployability |
| Model size (MB) | deployability |

Report false vacancy separately from overall error. Directing a driver to an
occupied slot is worse than showing a vacant slot as occupied, and no aggregate
metric captures that asymmetry.

---

## Results table template

Copy into `RESULTS.md`. Fill from `results.csv` only. `TBD` until measured.

### Same-lot (train UFPR04 → test UFPR04)

| Method | Acc | P | R | F1 | False vacancy | ms/slot | Size |
|---|---|---|---|---|---|---|---|
| A: Threshold (v1) | TBD | TBD | TBD | TBD | TBD | TBD | 0 |
| B: MobileNetV3-S | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| C: B + smoothing | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

### Cross-lot (train UFPR04 → test PUCPR)

| Method | Acc | P | R | F1 | False vacancy | Δ vs same-lot |
|---|---|---|---|---|---|---|
| A | TBD | TBD | TBD | TBD | TBD | TBD |
| B | TBD | TBD | TBD | TBD | TBD | TBD |
| C | TBD | TBD | TBD | TBD | TBD | TBD |

### Cross-dataset (train PKLot → test CNRPark-EXT)

| Method | Acc | F1 | False vacancy | Δ vs same-lot |
|---|---|---|---|---|
| A | TBD | TBD | TBD | TBD |
| B | TBD | TBD | TBD | TBD |
| C | TBD | TBD | TBD | TBD |

### Weather (train on all, test by condition)

| Condition | Method B Acc | Method C Acc |
|---|---|---|
| Sunny | TBD | TBD |
| Overcast | TBD | TBD |
| Rainy | TBD | TBD |

### Flicker

| Method | Transitions/slot/hour | Reduction |
|---|---|---|
| B (no smoothing) | TBD | — |
| C (N-of-M) | TBD | TBD |

---

## Expect the cross-lot number to drop

Same-lot accuracy on PKLot is saturated near 99% and proves almost nothing.
Cross-lot and cross-dataset are where models fall apart, and published work
consistently shows a substantial drop.

**If your cross-lot accuracy is much lower than same-lot, that is the finding,
not a bug.** Report it plainly, analyse why (viewpoint, camera height, bay
geometry, climate), and let it motivate the detector-based approach as future
work. A reported honest gap reads as research maturity. A suspiciously perfect
number reads as a leak in your splits — and an interviewer will ask.

If cross-lot accuracy comes out *very* high, suspect your split before
celebrating.

---

## Reproducibility checklist

- [ ] Seeds fixed and logged (`numpy`, `torch`, `random`)
- [ ] Split manifests committed to git; image data is not
- [ ] `results.csv` written by the eval script, never hand-edited
- [ ] Model checkpoint sha256 recorded next to each result row
- [ ] Colab notebook committed with outputs cleared
- [ ] `RESULTS.md` regenerable by re-running one command
- [ ] Library versions pinned in `ml/requirements.txt`
