# APMS v2 — split summary

Seed: `20260101`. Split unit: `(lot, date)`, weather ignored when
forming the unit, used only to stratify assignment.

Regenerate with `python -m apms_ml.splits`; verify with
`python -m apms_ml.verify`. Only `day_assignments.csv` and this file are
committed — the per-split CSVs are derived and gitignored.

| Split | Days | Vacant | Occupied | Total | Occupied share |
|---|---:|---:|---:|---:|---:|
| `same_lot_train` | 22 | 44,096 | 33,288 | 77,384 | 0.430 |
| `same_lot_val` | 5 | 10,901 | 6,117 | 17,018 | 0.359 |
| `same_lot_test` | 3 | 4,721 | 6,720 | 11,441 | 0.587 |
| `cross_lot_test` | 0 | 0 | 0 | 0 | n/a |
| `cross_dataset_test` | 23 | 65,658 | 79,307 | 144,965 | 0.547 |
| `weather_sunny` | 0 | 157,381 | 157,140 | 314,521 | 0.500 |
| `weather_cloudy` | 0 | 114,057 | 79,657 | 193,714 | 0.411 |
| `weather_rainy` | 0 | 31,636 | 61,578 | 93,214 | 0.661 |

Days held out of every split (`unassigned`): 71

Weather views overlap the test splits by design and exclude every
training and validation day. They carry a `lot` column, so a mixed
same-lot / cross-lot population stays visible in the evaluation.

## Stratification notes

- Cloudy: 12 days, 41,823 patches; train share 0.732 (target 0.70)
- Sunny: 18 days, 64,020 patches; train share 0.731 (target 0.70)
