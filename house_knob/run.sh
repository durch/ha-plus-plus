#!/usr/bin/with-contenv sh
# House Knob bridge add-on entrypoint.
# Config lives in the add-on config dir (/config inside the container,
# addon_configs/<slug>/ on the HA host) so it survives updates and is editable.
set -e
[ -f /config/knob.yaml ] || cp /app/knob.default.yaml /config/knob.yaml
mkdir -p /config/firmware

export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

ARGS="--config /config/knob.yaml --firmware-dir /config/firmware --state /data/state.json"
if [ "$(jq -r '.dry_run' /data/options.json 2>/dev/null)" = "true" ]; then
  ARGS="$ARGS --dry-run"
fi
cd /app
exec python3 -m ha_knob.server $ARGS
