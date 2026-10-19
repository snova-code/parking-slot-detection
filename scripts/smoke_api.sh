#!/usr/bin/env bash
# scripts/smoke_api.sh
# Drives the whole API against a running server: bootstrap an organization,
# log in, create a lot and camera, annotate slots, then act as the worker.
#
#   ./scripts/smoke_api.sh [base_url]
#
# Tests run through Django's test client; this runs through real HTTP, real
# middleware, real CSRF and a real device token. It is also what a reviewer
# can run to see the system work without reading any code.
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000/api/v1}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${APMS_SERVER_PYTHON:-$HOME/venvs/server/bin/python}"
JAR="$(mktemp)"
SLUG="smoke-$(date +%s)"
PHONE="+977981${RANDOM:0:2}${RANDOM:0:5}"
export APMS_SMOKE_PASSWORD="smoke-$(openssl rand -hex 12)"

trap 'rm -f "$JAR"' EXIT

json() { "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(eval("d"+sys.argv[1]))' "$1"; }
step() { printf '\n== %s\n' "$1"; }

CARD="$(ls "$REPO"/models/*.json 2>/dev/null | head -1 || true)"
if [ -z "$CARD" ]; then
  echo "No model card in $REPO/models/. Train and export a model first, or"
  echo "point APMS_MODEL_CARDS_DIR at a directory containing one."
  exit 1
fi
MODEL="$(basename "$CARD" .json)"
echo "Using model $MODEL"

step "bootstrap an organization"
cd "$REPO/server"
"$REPO/scripts/with_env.sh" "$PY" manage.py create_org \
  --slug "$SLUG" --name "Smoke Test Co" --owner-phone "$PHONE" \
  --password-env APMS_SMOKE_PASSWORD

step "log in"
curl -sf -c "$JAR" "$BASE/auth/csrf" > /dev/null
CSRF="$(awk '$6=="csrftoken"{print $7}' "$JAR")"
curl -sf -b "$JAR" -c "$JAR" -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' \
  -d "{\"phone_number\":\"$PHONE\",\"password\":\"$APMS_SMOKE_PASSWORD\"}" \
  "$BASE/auth/login" > /dev/null

post() {
  CSRF="$(awk '$6=="csrftoken"{print $7}' "$JAR")"
  curl -sf -b "$JAR" -c "$JAR" -X "${3:-POST}" \
    -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' \
    -d "$2" "$BASE$1"
}

step "create a lot"
LOT="$(post /lots '{"name":"Smoke Lot","city":"Lalitpur"}' | json '["id"]')"

step "create a camera"
CAMERA_JSON="$(post "/lots/$LOT/cameras" "$(cat <<EOF
{"name":"Smoke cam","source_uri":"file:///data/demo.mp4",
 "reference_frame":{"width":1280,"height":720},"model_version":"$MODEL"}
EOF
)")"
CAMERA="$(echo "$CAMERA_JSON" | json '["id"]')"
TOKEN="$(echo "$CAMERA_JSON" | json '["device_token"]')"
VERSION="$(echo "$CAMERA_JSON" | json '["slots_version"]')"

step "annotate two slots"
SLOTS_JSON="$(post "/cameras/$CAMERA/slots:bulk" "$(cat <<EOF
{"slots_version":"$VERSION","reference_frame":{"width":1280,"height":720},
 "slots":[{"label":"A-01","polygon":[[120,340],[186,338],[190,382],[124,384]]},
          {"label":"A-02","polygon":[[200,340],[266,338],[270,382],[204,384]]}]}
EOF
)")"
SLOT="$(echo "$SLOTS_JSON" | json '["slots"][0]["id"]')"
echo "$SLOTS_JSON" | json '["slots"][0]["polygon"]'

step "worker fetches its config"
curl -sf -H "Authorization: Bearer $TOKEN" "$BASE/worker/config" | json '["config_version"]'

step "worker heartbeats"
curl -sf -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"camera_id\":\"$CAMERA\",\"fps\":1.94,\"frames_processed\":100,
       \"queue_depth\":0,\"model_version\":\"$MODEL\",\"uptime_seconds\":50}" \
  "$BASE/worker/heartbeat" | json '["resync_slot_ids"]'

step "worker posts an arrival, then the same batch again"
EVENT_ID="$("$PY" -c 'import uuid;print(uuid.uuid4())')"
NOW="$("$PY" -c 'from datetime import datetime,timezone;print(datetime.now(timezone.utc).isoformat())')"
BATCH="{\"camera_id\":\"$CAMERA\",\"events\":[{\"id\":\"$EVENT_ID\",
  \"slot_id\":\"$SLOT\",\"previous_status\":\"VACANT\",\"status\":\"OCCUPIED\",
  \"confidence\":0.94,\"observed_at\":\"$NOW\",\"model_version\":\"$MODEL\"}]}"
for attempt in 1 2; do
  curl -sf -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "$BATCH" "$BASE/worker/events"
  echo " <- attempt $attempt"
done

step "dashboard reads the lot"
curl -sf -b "$JAR" "$BASE/lots" | json '[0]'

step "analytics over the last hour"
FROM="$("$PY" -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())')"
TO="$("$PY" -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())')"
curl -sf -b "$JAR" --get --data-urlencode "from=$FROM" --data-urlencode "to=$TO" \
  --data-urlencode "bucket=hour" "$BASE/lots/$LOT/analytics" | json '["dwell_minutes"]'

step "stream, for three seconds"
curl -sN -b "$JAR" --max-time 3 "$BASE/lots/$LOT/stream" || true

printf '\nSmoke test finished. Organization slug: %s\n' "$SLUG"