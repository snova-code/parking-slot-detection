#!/usr/bin/env bash
# scripts/extract_datasets.sh
# Extracts the archives fetched by fetch_datasets.sh. Idempotent: re-running
# skips anything already extracted. Never overwrites.
set -euo pipefail

DATA_ROOT="${APMS_DATA_ROOT:-$HOME/data}"
ARCHIVES="$DATA_ROOT/archives"
PKLOT_DIR="$DATA_ROOT/pklot"
CNR_DIR="$DATA_ROOT/cnrpark"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$(readlink -f "$DATA_ROOT")" in
  /mnt/c/*|/mnt/d/*|/mnt/e/*)
    echo "ABORT: \$DATA_ROOT is on a Windows drive. Extracting 700k small files"
    echo "       across the 9P layer will take hours instead of minutes."
    exit 1 ;;
esac

need_archive () {
  [ -f "$ARCHIVES/$1" ] || { echo "ABORT: missing $ARCHIVES/$1 — run fetch_datasets.sh"; exit 1; }
}
need_archive "PKLot.tar.gz"
need_archive "CNR-EXT-Patches-150x150.zip"
need_archive "CNRPark-Patches-150x150.zip"
need_archive "CNRPark+EXT.csv"

avail_gb=$(df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9')
if [ "$avail_gb" -lt 15 ]; then
  echo "ABORT: ${avail_gb}GB free, need ~15GB to extract."
  exit 1
fi

mkdir -p "$PKLOT_DIR" "$CNR_DIR"

# --- PKLot --------------------------------------------------------------
if [ -f "$PKLOT_DIR/.extracted" ]; then
  echo "PKLot already extracted (remove $PKLOT_DIR/.extracted to force)."
else
  echo "=== PKLot archive layout (first two path levels) ==="
  { tar -tzf "$ARCHIVES/PKLot.tar.gz" || true; } \
    | head -5000 \
    | awk -F/ 'NF>1 && $2 != "" {print $1"/"$2}' \
    | sort -u | head -20
  echo
  echo "=== Extracting PKLot (4.6GB, ~700k files — one dot per 20k) ==="
  tar -xzf "$ARCHIVES/PKLot.tar.gz" -C "$PKLOT_DIR" \
      --checkpoint=20000 --checkpoint-action=dot
  echo
  touch "$PKLOT_DIR/.extracted"
fi

# --- CNRPark+EXT --------------------------------------------------------
if [ -f "$CNR_DIR/.extracted" ]; then
  echo "CNRPark+EXT already extracted (remove $CNR_DIR/.extracted to force)."
else
  echo "=== Extracting CNR-EXT patches (449MB) ==="
  unzip -q -n "$ARCHIVES/CNR-EXT-Patches-150x150.zip" -d "$CNR_DIR"
  echo "=== Extracting CNRPark patches (36MB) ==="
  unzip -q -n "$ARCHIVES/CNRPark-Patches-150x150.zip" -d "$CNR_DIR"
  cp -n "$ARCHIVES/CNRPark+EXT.csv" "$CNR_DIR/CNRPark+EXT.csv"
  touch "$CNR_DIR/.extracted"
fi

echo
du -sh "$PKLOT_DIR" "$CNR_DIR"
echo
exec "$REPO/scripts/inventory_datasets.sh"