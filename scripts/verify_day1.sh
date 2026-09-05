#!/usr/bin/env bash
# scripts/verify_day1.sh
# Runs the Phase 0 Day 1 checklist. Exit 0 means the session is committable.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0

ok ()   { printf '  PASS  %s\n' "$1"; }
bad ()  { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

echo "== Filesystem =="
case "$REPO" in
  /mnt/*) bad "repo is on a Windows drive ($REPO)" ;;
  *)      ok  "repo is on ext4 ($REPO)" ;;
esac

echo "== WSL resources =="
mem_gb=$(( $(grep MemTotal /proc/meminfo | tr -dc '0-9') / 1024 / 1024 ))
[ "$mem_gb" -ge 14 ] && ok "memory ${mem_gb}GB" || bad "memory ${mem_gb}GB (expected ~16)"
cpus=$(nproc)
[ "$cpus" -ge 8 ] && ok "nproc $cpus" || bad "nproc $cpus (expected 10)"
watches=$(cat /proc/sys/fs/inotify/max_user_watches)
[ "$watches" -ge 524288 ] && ok "inotify $watches" || bad "inotify $watches (expected 524288)"

echo "== Git =="
[ "$(git -C "$REPO" config core.autocrlf)" = "input" ] \
  && ok "core.autocrlf=input" || bad "core.autocrlf not 'input'"
[ "$(git -C "$REPO" config core.hooksPath)" = ".githooks" ] \
  && ok "core.hooksPath=.githooks" || bad "core.hooksPath not set"
check ".gitattributes exists"        "[ -f '$REPO/.gitattributes' ]"
check ".gitignore exists"            "[ -f '$REPO/.gitignore' ]"
check "pre-commit hook executable"   "[ -x '$REPO/.githooks/pre-commit' ]"
check "infra/.env is ignored"        "git -C '$REPO' check-ignore -q infra/.env"
check "github ssh auth"              "ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -q 'successfully authenticated'"

echo "== CRLF trap =="
tmp="$(mktemp -d)"
printf '#!/usr/bin/env bash\necho ok\n' > "$tmp/t.sh"
chmod +x "$tmp/t.sh"
[ "$("$tmp/t.sh" 2>/dev/null)" = "ok" ] && ok "LF script executes" || bad "LF script failed"
rm -rf "$tmp"

echo "== Docker =="
check "docker daemon"     "docker run --rm hello-world"
check "compose v2"        "docker compose version"

echo "== Services =="
if [ -f "$REPO/infra/.env" ]; then
  set -a; . "$REPO/infra/.env"; set +a
  PGPASSWORD="$POSTGRES_PASSWORD" \
    psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT version();' >/dev/null 2>&1 \
    && ok "psql connects" || bad "psql cannot connect"
  unset PGPASSWORD
else
  bad "infra/.env missing"
fi
[ "$(redis-cli -h 127.0.0.1 ping 2>/dev/null)" = "PONG" ] && ok "redis PONG" || bad "redis unreachable"

echo
if [ "$fails" -eq 0 ]; then
  echo "Day 1 checklist: all passed."
else
  echo "Day 1 checklist: $fails failed."
fi
exit "$fails"