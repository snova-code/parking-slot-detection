#!/usr/bin/env bash
# scripts/with_env.sh
# Run one command with infra/.env loaded, without exporting secrets into your
# interactive shell.
#   ./scripts/with_env.sh <command> [args...]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/infra/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "missing $ENV_FILE — copy infra/.env.example and fill it"
  exit 1
fi
set -a
. "$ENV_FILE"
set +a
exec "$@"