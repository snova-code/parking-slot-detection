#!/usr/bin/env bash
# scripts/inventory_datasets.sh
# Counts what is actually on disk and writes $APMS_DATA_ROOT/COUNTS.md.
#
# Publisher figures are quoted for comparison only; they are claims, not
# measurements. If your counts differ, record the difference — do not adjust.
#
# Note: deliberately NOT `set -e`. An inventory tool should report every
# section it can reach and then exit non-zero if any section failed, rather
# than dying halfway and leaving a truncated COUNTS.md.
set -uo pipefail

DATA_ROOT="${APMS_DATA_ROOT:-$HOME/data}"
PKLOT_DIR="$DATA_ROOT/pklot"
CNR_DIR="$DATA_ROOT/cnrpark"
OUT="$DATA_ROOT/COUNTS.md"

PROBLEMS=0
problem () { PROBLEMS=$((PROBLEMS + 1)); echo "**MISSING:** $*"; }

mkdir -p "$DATA_ROOT"

# Shared awk helper: tally a path field and print each group sorted by key,
# with the group header attached to its own group. (The previous version
# piped the whole awk block through `sort`, which hoisted every indented
# count line above the "by ...:" headers.)
read -r -d '' AWK_DUMP <<'AWK' || true
function dump(label, arr,   keys, n, k, i, j, t) {
  print label
  n = 0
  for (k in arr) keys[++n] = k
  for (i = 2; i <= n; i++) {
    t = keys[i]; j = i - 1
    while (j > 0 && keys[j] > t) { keys[j+1] = keys[j]; j-- }
    keys[j+1] = t
  }
  for (i = 1; i <= n; i++) printf "  %-12s %8d\n", keys[i], arr[keys[i]]
}
AWK

main () {

echo "# APMS v2 — dataset inventory"
echo
echo "Measured: $(date -Is)"
echo "Host: $(uname -srm)"
echo "Data root: \`${DATA_ROOT/#$HOME/\~}\`"
echo

# ======================================================================
echo "## PKLot"
echo
seg=""
if [ -d "$PKLOT_DIR" ]; then
  seg=$(find "$PKLOT_DIR" -maxdepth 3 -type d -name 'PKLotSegmented' | head -1)
else
  problem "\`$PKLOT_DIR\` does not exist — run fetch_datasets.sh / extract_datasets.sh."
fi

if [ -z "$seg" ]; then
  if [ -d "$PKLOT_DIR" ]; then
    problem "No \`PKLotSegmented\` directory under \`$PKLOT_DIR\`."
    echo
    echo "Top-level contents:"
    echo '```'
    find "$PKLOT_DIR" -maxdepth 2 -mindepth 1 -type d | sed "s|$PKLOT_DIR/||"
    echo '```'
    echo
    echo "Patches must be cut from the full frames + XML in Task 1.1."
  fi
else
  echo "Segmented root: \`${seg/#$HOME/\~}\`"
  echo
  echo '```'
  find "$seg" -type f -name '*.jpg' -printf '%P\n' | awk -F/ "$AWK_DUMP"'
    NF < 3 { shallow++; total++; next }
    { total++; lot[$1]++; weather[$2]++; cls[$(NF-1)]++ }
    END {
      printf "total patches   %8d\n", total
      if (shallow) printf "unparsable path %8d\n", shallow
      print ""
      dump("by lot:", lot);         print ""
      dump("by weather:", weather); print ""
      dump("by class:", cls)
    }'
  echo '```'
fi
echo

# Full frames live beside their XML, outside the segmented tree.
if [ -n "$seg" ]; then
  frames=$(find "$PKLOT_DIR" -type f -name '*.jpg' -not -path "$seg/*" | wc -l)
else
  frames=$(find "$PKLOT_DIR" -type f -name '*.jpg' 2>/dev/null | wc -l)
fi
xmls=$(find "$PKLOT_DIR" -type f -name '*.xml' 2>/dev/null | wc -l)

echo "Full frames (\`.jpg\` outside segmented): $frames"
echo "Annotation files (\`.xml\`): $xmls"
echo

# An off-by-N here is a real data fact, not rounding. Name the frames.
if [ "$frames" != "$xmls" ] && [ -d "$PKLOT_DIR" ]; then
  echo "Frame/annotation mismatch: $((frames - xmls)). Frames with no \`.xml\`:"
  echo '```'
  if [ -n "$seg" ]; then
    comm -23 \
      <(find "$PKLOT_DIR" -type f -name '*.jpg' -not -path "$seg/*" -printf '%P\n' \
          | sed 's/\.jpg$//' | sort) \
      <(find "$PKLOT_DIR" -type f -name '*.xml' -printf '%P\n' \
          | sed 's/\.xml$//' | sort) | head -20
  else
    comm -23 \
      <(find "$PKLOT_DIR" -type f -name '*.jpg' -printf '%P\n' | sed 's/\.jpg$//' | sort) \
      <(find "$PKLOT_DIR" -type f -name '*.xml' -printf '%P\n' | sed 's/\.xml$//' | sort) | head -20
  fi
  echo '```'
  echo "(\`.xml\` files with no frame, if any:)"
  echo '```'
  comm -13 \
    <(find "$PKLOT_DIR" -type f -name '*.jpg' ${seg:+-not -path "$seg/*"} -printf '%P\n' \
        | sed 's/\.jpg$//' | sort) \
    <(find "$PKLOT_DIR" -type f -name '*.xml' -printf '%P\n' | sed 's/\.xml$//' | sort) | head -20
  echo '```'
  echo
fi

echo "UFPR publishes: *around 695,900* segmented parking spaces, 12,417 full frames."
echo

# ======================================================================
echo "## CNR-EXT"
echo
cnrext=""
if [ -d "$CNR_DIR" ]; then
  cnrext=$(find "$CNR_DIR" -maxdepth 3 -type d -name 'PATCHES' | head -1)
else
  problem "\`$CNR_DIR\` does not exist — run fetch_datasets.sh / extract_datasets.sh."
fi

if [ -z "$cnrext" ]; then
  [ -d "$CNR_DIR" ] && problem "No \`PATCHES\` directory under \`$CNR_DIR\`."
else
  echo '```'
  find "$cnrext" -type f -name '*.jpg' -printf '%P\n' | awk -F/ "$AWK_DUMP"'
    NF < 3 { shallow++; total++; next }
    { total++; weather[$1]++; cam[$3]++ }
    END {
      printf "total patches   %8d\n", total
      if (shallow) printf "unparsable path %8d\n", shallow
      print ""
      dump("by weather:", weather); print ""
      dump("by camera:", cam)
    }'
  echo '```'
fi
echo

labels=""
[ -d "$CNR_DIR" ] && labels=$(find "$CNR_DIR" -maxdepth 3 -type d -name 'LABELS' | head -1)
if [ -n "$labels" ]; then
  echo "Label list files (\`<path> <0|1>\`, busy=1 / free=0):"
  echo '```'
  for f in "$labels"/*; do
    [ -f "$f" ] || continue
    lines=$(wc -l < "$f")
    busy=$(awk '$NF==1' "$f" | wc -l)
    free=$(awk '$NF==0' "$f" | wc -l)
    other=$((lines - busy - free))
    pct=0
    [ "$lines" -gt 0 ] && pct=$(( busy * 100 / lines ))
    printf '  %-24s %6d lines  busy=%-6d free=%-6d  %d%% busy%s\n' \
      "$(basename "$f")" "$lines" "$busy" "$free" "$pct" \
      "$( [ "$other" -ne 0 ] && printf '  [%d unparsed]' "$other" )"
  done
  echo '```'
  echo
  echo "Class balance differs across the official splits — check this before"
  echo "reporting plain accuracy on val or test."
else
  [ -d "$CNR_DIR" ] && problem "No \`LABELS\` directory under \`$CNR_DIR\`."
fi
echo
echo "CNR publishes: 144,965 CNR-EXT patches from 4,081 frames, 9 cameras."
echo

# ======================================================================
echo "## CNRPark (preliminary subset)"
echo
# xargs appends its arguments AFTER the command, so the old
#   find ... -print0 | xargs -0 find -type f -name '*.jpg' -printf ...
# built `find -type f ... /path/to/A`, i.e. a path after the expression.
# Collect the roots first and pass them in the right position instead.
roots=()
if [ -d "$CNR_DIR" ]; then
  while IFS= read -r -d '' d; do roots+=("$d"); done < <(
    find "$CNR_DIR" -maxdepth 1 -mindepth 1 \( -name 'A' -o -name 'B' \) -type d -print0
  )
fi

if [ "${#roots[@]}" -eq 0 ]; then
  problem "No \`A\`/\`B\` camera directories under \`$CNR_DIR\` — CNRPark patches not extracted."
else
  echo "Camera roots found: ${#roots[@]}"
  echo
  echo '```'
  for root in "${roots[@]}"; do
    find "$root" -type f -name '*.jpg' -printf "$(basename "$root")/%P\n"
  done | awk -F/ "$AWK_DUMP"'
    NF < 2 { shallow++; total++; next }
    { total++; cam[$1]++; cls[$(NF-1)]++ }
    END {
      printf "total patches   %8d\n", total
      if (shallow) printf "unparsable path %8d\n", shallow
      print ""
      dump("by camera:", cam); print ""
      dump("by class:", cls)
    }'
  echo '```'
fi
echo
echo "CNR publishes: 12,584 CNRPark patches from 2 cameras."
echo

# ======================================================================
echo "## Metadata CSV"
echo
csv="$CNR_DIR/CNRPark+EXT.csv"
if [ -f "$csv" ]; then
  echo '```'
  echo "rows (incl. header): $(wc -l < "$csv")"
  echo "header: $(head -1 "$csv")"
  echo '```'
else
  problem "\`CNRPark+EXT.csv\` not found at \`$csv\`."
fi
echo

# ======================================================================
echo "## Status"
echo
if [ "$PROBLEMS" -eq 0 ]; then
  echo "All sections measured."
else
  echo "$PROBLEMS section(s) could not be measured — see **MISSING** above."
fi

}

main 2>&1 | tee "$OUT"
status=${PIPESTATUS[0]}
exit "$status"