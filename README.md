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
# No HA reachable (e.g. testing the physical knob away from the house):
uv run python -m ha_knob.server --config knob.yaml --fake ../snapshots/<snapshot>.json
uv run pytest -q
```

What the knob controls is defined in [house_knob/knob.yaml](house_knob/knob.yaml): climate groups
(House, Terrace) with heat/cool entity sets, lights, and covers.

## Layout

| Path | What |
|---|---|
| `house_knob/` | The bridge, packaged as a Home Assistant add-on (`config.yaml`, `Dockerfile`, `run.sh`) |
| `house_knob/knob.yaml` | What the knob controls: climate groups, lights and presets, cover groups, schedule helpers |
| `house_knob/ha_knob/controls.py` | Control model and climate logic (pure, tested) |
| `house_knob/ha_knob/ha.py` | HA WebSocket client; `--dry-run` and `--fake` modes |
| `house_knob/ha_knob/server.py` | hk/1 WebSocket server, firmware OTA endpoints, simulator |
| `house_knob/ha_knob/snapshot.py` | Save / restore the state of everything the knob can touch |
| `house_knob/ha_knob/sim/concepts/` | Browser simulator (`touch-and-turn.html`) and the concepts considered |
| `scripts/` | One-off HA migrations |
| `docs/` | [Protocol](docs/PROTOCOL.md), [deploying](docs/DEPLOY.md), [firmware notes](docs/FIRMWARE.md) |

The knob's firmware is a private fork of roon-knob and is not in this repository.

## Bridge modes

| Mode | Reads | Writes |
|---|---|---|
| (default) | live HA | live HA |
| `--dry-run` | live HA | nothing: commands change a local copy only |
| `--fake <snapshot.json>` | a snapshot file | nothing; needs no HA at all |
