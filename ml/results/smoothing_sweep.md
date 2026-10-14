# Method C — N-of-M sweep

Split: `same_lot_val` (validation; selection on a test split would leak)

Generated: 2026-10-14T15:42:26+0000 at commit `fbe8f76`

Causal window. Warm-up threshold scales as ceil((N/M)*w).
Max gap 20 min; 60 breaks; 200 sequences over 17,018 patches.

Ground-truth transitions/slot/hour: 0.198

```
  M   N      acc  bal_acc       F1  false_vac   flick/h   vs M=1  lag_p50  lag_p90  missed
------------------------------------------------------------------------------------------
  1   1   0.9999   0.9999   0.9999     0.0001      0.20        —        0        0       0
  5   3   0.9770   0.9754   0.9681     0.0171      0.11   +44.6%      600      990      40
  5   4   0.9758   0.9704   0.9659     0.0271      0.12   +38.9%      300      901      35
  9   6   0.9640   0.9586   0.9494     0.0335      0.09   +56.4%      900     1801      54
  9   7   0.9622   0.9535   0.9461     0.0422      0.10   +52.1%      600     1801      49
  9   8   0.9587   0.9457   0.9400     0.0537      0.11   +47.1%      300     2101      45
 15   9   0.9525   0.9478   0.9337     0.0385      0.06   +67.9%     1350     2701      72
 15  11   0.9488   0.9386   0.9268     0.0533      0.07   +65.0%     1200     3001      73
 15  12   0.9465   0.9332   0.9225     0.0611      0.08   +62.1%      900     3302      63
```

Regenerate with `python -m apms_ml.smoothing --sweep --split same_lot_val`.
