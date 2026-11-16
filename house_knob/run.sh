#!/usr/bin/with-contenv sh
# House Knob bridge add-on entrypoint.
# /config inside the container is addon_configs/<slug>/ on the HA host: an optional
# knob.yaml override and the firmware/ folder served to the knob for updates.
set -e
# The bundled knob.yaml is used unless you drop your own at addon_configs/<slug>/knob.yaml,
# so add-on updates bring config changes with them.
CONFIG=/app/knob.default.yaml
[ -f /config/knob.yaml ] && CONFIG=/config/knob.yaml
mkdir -p /config/firmware

export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

ARGS="--config $CONFIG --firmware-dir /config/firmware --state /data/state.json"
if [ "$(jq -r '.dry_run' /data/options.json 2>/dev/null)" = "true" ]; then
  ARGS="$ARGS --dry-run"
fi
cd /app
exec python3 -m ha_knob.server $ARGS
