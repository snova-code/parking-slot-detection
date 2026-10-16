#!/usr/bin/env bash
# scripts/regenerate_results.sh
# Rebuild results.csv and RESULTS.md from scratch.
#   ./scripts/regenerate_results.sh <M> <N>
# M and N are the smoothing pair selected in ml/results/smoothing_sweep.md.
# RESULTS.md prints the exact invocation that produced it.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <M> <N>"
  echo "  M, N: the pair chosen from ml/results/smoothing_sweep.md"
  exit 2
fi
M="$1"
N="$2"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${APMS_ML_PYTHON:-$HOME/venvs/ml/bin/python}"

cd "$REPO"
dirty="$(git status --porcelain -- . ':(exclude)ml/results')"
if [ -n "$dirty" ]; then
  echo "ABORT: uncommitted changes outside ml/results/."
  echo "       Every row would be stamped -dirty and excluded from RESULTS.md."
  echo "$dirty"
  exit 1
fi

cd "$REPO/ml"
"$PY" -m apms_ml.splits
rm -f results/results.csv
"$PY" -m apms_ml.evaluate --method threshold_a --all-test
"$PY" -m apms_ml.evaluate --method mobilenet_b --all-test --dump-scores
"$PY" -m apms_ml.smoothing --apply --m 1 --n 1 --all-test \
  --notes "unsmoothed baseline: flicker reference and M=1 identity check"
"$PY" -m apms_ml.smoothing --apply --m "$M" --n "$N" --all-test
"$PY" -m apms_ml.report

echo
echo "Review ml/results/RESULTS.md, then:"
echo "  git add ml/results/ && git commit -m 'Regenerate benchmark results'"
echo "If a step failed midway: git checkout ml/results/results.csv"