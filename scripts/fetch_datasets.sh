#!/usr/bin/env bash
# scripts/fetch_datasets.sh
# Downloads PKLot and CNRPark+EXT. Resumable — safe to re-run after a dropped
# connection. Records self-computed SHA-256 (neither publisher publishes hashes).
set -euo pipefail

DATA_ROOT="${APMS_DATA_ROOT:-$HOME/data}"
ARCHIVES="$DATA_ROOT/archives"
MANIFEST="$DATA_ROOT/MANIFEST.txt"
WITH_FULL_IMAGES="${WITH_FULL_IMAGES:-0}"

CNR_BASE="https://github.com/fabiocarrara/deep-parking/releases/download/archive"

# Refuse to write into the Windows filesystem. 9P makes this 10x slower for the
# hundreds of thousands of small JPEGs these archives contain.
case "$(readlink -f "$DATA_ROOT")" in
  /mnt/c/*|/mnt/d/*|/mnt/e/*)
    echo "ABORT: \$DATA_ROOT resolves under /mnt/ (Windows drive)."
    echo "Use a path inside the WSL2 ext4 filesystem, e.g. \$HOME/data"
    exit 1
    ;;
esac

mkdir -p "$ARCHIVES"

avail_gb=$(df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9')
if [ "$avail_gb" -lt 20 ]; then
  echo "ABORT: only ${avail_gb}GB free on $DATA_ROOT. Need ~20GB for archives."
  exit 1
fi

fetch () {
  local url="$1" name="$2" published_size="$3"
  echo "=== $name  (published size: $published_size) ==="
  wget --continue \
       --tries=20 \
       --timeout=30 \
       --waitretry=10 \
       --progress=dot:giga \
       -O "$ARCHIVES/$name" \
       "$url"
}

fetch "http://www.inf.ufpr.br/vri/databases/PKLot.tar.gz" \
      "PKLot.tar.gz" "4.6 GB"

fetch "$CNR_BASE/CNRPark%2BEXT.csv" \
      "CNRPark+EXT.csv" "18.1 MB"

fetch "$CNR_BASE/CNR-EXT-Patches-150x150.zip" \
      "CNR-EXT-Patches-150x150.zip" "449.5 MB"

fetch "$CNR_BASE/CNRPark-Patches-150x150.zip" \
      "CNRPark-Patches-150x150.zip" "36.6 MB"

if [ "$WITH_FULL_IMAGES" = "1" ]; then
  fetch "$CNR_BASE/CNR-EXT_FULL_IMAGE_1000x750.tar" \
        "CNR-EXT_FULL_IMAGE_1000x750.tar" "1.1 GB"
fi

echo
echo "=== Computing SHA-256 (a few minutes for the 4.6GB archive) ==="
{
  echo "# APMS v2 dataset archive manifest"
  echo "# Generated: $(date -Is)"
  echo "# Hashes are SELF-COMPUTED. Neither UFPR nor CNR publishes checksums."
  echo "# Their purpose is detecting a corrupt or partial re-download, not"
  echo "# verifying authenticity against the publisher."
  echo
  cd "$ARCHIVES"
  for f in *; do
    printf '%s  %s bytes  %s\n' "$(sha256sum "$f" | cut -d' ' -f1)" \
                                "$(stat -c%s "$f")" "$f"
  done
} > "$MANIFEST"

cat "$MANIFEST"
echo
echo "Archives in $ARCHIVES. Next: scripts/extract_datasets.sh"