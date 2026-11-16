"""Save / restore the state of every entity the knob controls.

    uv run python -m ha_knob.snapshot save    [--file snapshots/<ts>.json]
    uv run python -m ha_knob.snapshot restore  snapshots/<file>.json [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

from .controls import build

ROOT = Path(__file__).resolve().parent.parent
UNAVAILABLE = {"unavailable", "unknown"}


def _api(method: str, path: str, body: dict | None = None):
    url = os.environ["HA_URL"].rstrip("/") + path
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {os.environ['HA_TOKEN']}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def entities(config_path: Path) -> list[str]:
    layout = build(yaml.safe_load(config_path.read_text()))
    return sorted(set().union(*(c.entities() for c in layout.controls.values())))


def save(config_path: Path, out: Path | None) -> Path:
    wanted = set(entities(config_path))
    states = [s for s in _api("GET", "/api/states") if s["entity_id"] in wanted]
    out = out or ROOT.parent / "snapshots" / f"{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"saved_at": dt.datetime.now().isoformat(), "states": states}, indent=1))
    return out


def restore_calls(states: list[dict]) -> list[tuple[str, str, dict]]:
    calls = []
    for s in states:
        eid, st, a = s["entity_id"], s["state"], s.get("attributes", {})
        domain = eid.split(".")[0]
        if st in UNAVAILABLE:
            continue
        if domain == "climate":
            if st == "off":
                calls.append(("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "off"}))
            else:
                data = {"entity_id": eid, "hvac_mode": st}
                if a.get("temperature") is not None:
                    data["temperature"] = a["temperature"]
                calls.append(("climate", "set_temperature", data))
        elif domain == "light":
            if st == "off":
                calls.append(("light", "turn_off", {"entity_id": eid}))
            else:
                data = {"entity_id": eid}
                if a.get("brightness") is not None:
                    data["brightness"] = a["brightness"]
                if a.get("color_mode") == "color_temp" and a.get("color_temp_kelvin"):
                    data["color_temp_kelvin"] = a["color_temp_kelvin"]
                elif a.get("color_mode") == "xy" and a.get("xy_color"):
                    data["xy_color"] = a["xy_color"]
                calls.append(("light", "turn_on", data))
        elif domain == "cover":
            pos = a.get("current_position")
            if pos is None:
                pos = 100 if st in ("open", "opening") else 0
            calls.append(("cover", "set_cover_position", {"entity_id": eid, "position": pos}))
    return calls


def restore(path: Path, dry_run: bool) -> None:
    snap = json.loads(path.read_text())
    for domain, service, data in restore_calls(snap["states"]):
        print(f"{'would call' if dry_run else 'call'} {domain}.{service} {data}")
        if not dry_run:
            _api("POST", f"/api/services/{domain}/{service}", data)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save")
    s.add_argument("--file", type=Path)
    r = sub.add_parser("restore")
    r.add_argument("file", type=Path)
    r.add_argument("--dry-run", action="store_true")
    p.add_argument("--config", type=Path, default=ROOT / "knob.yaml")
    args = p.parse_args()
    if args.cmd == "save":
        print(save(args.config, args.file))
    else:
        restore(args.file, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
