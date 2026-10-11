#!/usr/bin/env bash
# scripts/verify_env.sh
# Environment checklist. Extended each phase; never forked per day.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="${APMS_DATA_ROOT:-$HOME/data}"
fails=0

ok ()   { printf '  PASS  %s\n' "$1"; }
bad ()  { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

echo "== Filesystem =="
case "$REPO" in
  /mnt/*) bad "repo is on a Windows drive ($REPO)" ;;
  *)      ok  "repo is on ext4 ($REPO)" ;;
esac
case "$(readlink -f "$DATA_ROOT")" in
  /mnt/*) bad "data root is on a Windows drive" ;;
  *)      ok  "data root is on ext4" ;;
esac

echo "== WSL resources =="
mem_gb=$(( $(grep MemTotal /proc/meminfo | tr -dc '0-9') / 1024 / 1024 ))
[ "$mem_gb" -ge 14 ] && ok "memory ${mem_gb}GB" || bad "memory ${mem_gb}GB (expected ~16)"
cpus=$(nproc)
[ "$cpus" -ge 8 ] && ok "nproc $cpus" || bad "nproc $cpus (expected 10)"
watches=$(cat /proc/sys/fs/inotify/max_user_watches)
[ "$watches" -ge 524288 ] && ok "inotify $watches" || bad "inotify $watches"

echo "== Git =="
[ "$(git -C "$REPO" config core.autocrlf)" = "input" ] \
  && ok "core.autocrlf=input" || bad "core.autocrlf not 'input'"
[ "$(git -C "$REPO" config core.hooksPath)" = ".githooks" ] \
  && ok "core.hooksPath=.githooks" || bad "core.hooksPath not set"
check ".gitattributes exists"      "[ -f '$REPO/.gitattributes' ]"
check "pre-commit hook executable" "[ -x '$REPO/.githooks/pre-commit' ]"
check "infra/.env is ignored"      "git -C '$REPO' check-ignore -q infra/.env"
check "nbstripout filter set"      "git -C '$REPO' config filter.nbstripout.clean"
check "github ssh auth" \
  "ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -q 'successfully authenticated'"

echo "== CRLF trap =="
tmp="$(mktemp -d)"
printf '#!/usr/bin/env bash\necho ok\n' > "$tmp/t.sh"; chmod +x "$tmp/t.sh"
[ "$("$tmp/t.sh" 2>/dev/null)" = "ok" ] && ok "LF script executes" || bad "LF script failed"
rm -rf "$tmp"

echo "== Docker =="
check "docker daemon" "docker run --rm hello-world"
check "compose v2"    "docker compose version"

echo "== Services =="
if [ -f "$REPO/infra/.env" ]; then
  set -a; . "$REPO/infra/.env"; set +a
  PGPASSWORD="$POSTGRES_PASSWORD" \
    psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT 1;' >/dev/null 2>&1 \
    && ok "psql connects" || bad "psql cannot connect"
  unset PGPASSWORD
else
  bad "infra/.env missing"
fi
[ "$(redis-cli -h 127.0.0.1 ping 2>/dev/null)" = "PONG" ] && ok "redis PONG" || bad "redis unreachable"

echo "== Virtualenvs =="
for v in server worker ml; do
  check "venv $v exists" "[ -x \"$HOME/venvs/$v/bin/python\" ]"
done
check "server: django importable" "$HOME/venvs/server/bin/python -c 'import django'"
check "server: ninja importable"  "$HOME/venvs/server/bin/python -c 'import ninja'"
check "worker: onnxruntime"       "$HOME/venvs/worker/bin/python -c 'import onnxruntime'"

# Architectural constraints, enforced rather than trusted:
if "$HOME/venvs/worker/bin/python" -c 'import torch' >/dev/null 2>&1; then
  bad "worker venv has torch (02_ARCHITECTURE.md: onnxruntime only)"
else
  ok "worker venv has no torch"
fi
if "$HOME/venvs/worker/bin/pip" list 2>/dev/null | grep -qE '^opencv-python[[:space:]]'; then
  bad "worker venv has GUI opencv (must be opencv-python-headless)"
else
  ok "worker venv has no GUI opencv"
fi
wo=$("$HOME/venvs/worker/bin/python" -c 'import onnxruntime;print(onnxruntime.__version__)' 2>/dev/null)
mo=$("$HOME/venvs/ml/bin/python"     -c 'import onnxruntime;print(onnxruntime.__version__)' 2>/dev/null)
[ -n "$wo" ] && [ "$wo" = "$mo" ] \
  && ok "onnxruntime matches across ml/worker ($wo)" \
  || bad "onnxruntime mismatch: worker=$wo ml=$mo"
if "$HOME/venvs/ml/bin/pip" list 2>/dev/null | grep -qi nvidia; then
  bad "ml venv pulled CUDA wheels — reinstall torch from the CPU index"
else
  ok "ml venv is CPU-only"
fi

echo "== Datasets =="
check "PKLot extracted"     "[ -f '$PKLOT_MARKER' ]" 2>/dev/null || true
check "PKLot extracted"     "[ -f '$DATA_ROOT/pklot/.extracted' ]"
check "CNRPark+EXT extracted" "[ -f '$DATA_ROOT/cnrpark/.extracted' ]"
check "MANIFEST.txt exists" "[ -s '$DATA_ROOT/MANIFEST.txt' ]"
check "COUNTS.md exists"    "[ -s '$DATA_ROOT/COUNTS.md' ]"

echo
if [ "$fails" -eq 0 ]; then echo "Environment checklist: all passed."
else echo "Environment checklist: $fails failed."; fi
exit "$fails"