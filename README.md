# HA++ House Knob

A Home Assistant controller for the Waveshare ESP32-S3 knob, using roon-knob's
principles: thin firmware, all logic in a bridge, OTA served by the bridge.
Protocol: [docs/PROTOCOL.md](docs/PROTOCOL.md) (hk/1).

## Run the bridge

```bash
# .envrc (git-ignored) holds HA_URL=http://<ha>:8123 and HA_TOKEN=<long-lived token>
cd house_knob
set -a; . ../.envrc; set +a
uv run python -m ha_knob.server --config knob.yaml --dry-run   # ws://<host>:8765/knob/ws
open http://localhost:8765/sim/            # browser simulator of the round screen
uv run pytest -q
```

What the knob controls is defined in [house_knob/knob.yaml](house_knob/knob.yaml): climate groups
(House, Terrace) with heat/cool entity sets, lights, and covers.

## Layout

| Path | What |
|---|---|
| `house_knob/ha_knob/controls.py` | Control model + climate group logic (pure, tested) |
| `house_knob/ha_knob/ha.py` | HA WebSocket client (state cache, service calls) |
| `house_knob/ha_knob/server.py` | hk/1 WebSocket server, OTA endpoints |
| `house_knob/ha_knob/sim/` | Browser simulator speaking hk/1 |
| `firmware/` | Fork of roon-knob v2.8.4 (own git repo, branch `ha-knob`) |

## Home Assistant add-on

`ha++` is an HA add-on repository (`repository.yaml`); `house_knob/` is the add-on
(Dockerfile, `config.yaml` manifest, `run.sh`). On HA the bridge talks to Core via
the Supervisor (no token), keeps its config at `addon_configs/<slug>/knob.yaml`, and
serves firmware for OTA from `addon_configs/<slug>/firmware/`.
