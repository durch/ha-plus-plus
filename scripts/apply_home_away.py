"""One-off HA migration: tie the house's automations to Home / Away (bespoke to this house).

Backs up every automation first (../backups/automations-<date>/), then:
  - rewrites the two house-mode automations (close up on Away, wake up on Home),
  - makes the morning shutters and the evening blinds Home-only,
  - deletes the broken and stale automations.

Run from house_knob/ with HA_URL and HA_TOKEN set:
    uv run python ../scripts/apply_home_away.py
"""

import datetime as dt
import json
import os
import urllib.request
from pathlib import Path

B = os.environ["HA_URL"]
H = {"Authorization": "Bearer " + os.environ["HA_TOKEN"], "Content-Type": "application/json"}

FLOORS = [("climate.dnevni", "offset_dnevni"), ("climate.dnevni_2", "offset_dnevni_2"),
          ("climate.hodnik", "offset_hodnik")]
INDOOR_ACS = ["climate.9424b844f92c", "climate.9424b82c5fa0", "climate.9424b82e9242"]
TERRACE = ["climate.580d0d34e617", "climate.heater", "climate.wonderwall_art_smart",
           "climate.wonderwall_art_smart_2"]
WINDOWS = ["cover.velux_window_roof_window", "cover.velux_window_roof_window_2",
           "cover.velux_window_roof_window_3"]
SHUTTERS_MAIN = ["cover.velux_external_cover_roller_shutter", "cover.velux_external_cover_roller_shutter_2"]
SHUTTER_KIDS = ["cover.velux_external_cover_roller_shutter_3"]
LIGHTS = ["light.terasa", "light.ambiance", "light.reflektor"]

HOME = {"condition": "state", "entity_id": "input_select.house_mode", "state": "Home"}
SCHEDULE_ON = {"condition": "state", "entity_id": "input_boolean.indoors_schedule", "state": "on"}

DELETE = ["automation.dnevni_underfloor_day_target", "automation.dnevni_underfloor_night_target",
          "automation.dnevni_2_underfloor_day_target", "automation.dnevni_2_underfloor_night_target",
          "automation.totoro", "automation.velux_kuhinja_airing",
          "automation.humidity_55", "automation.humidity_52", "automation.apartman_65", "automation.apartman_61",
          "automation.blinds_if_hot", "automation.blinds_if_not_hot"]
HOME_ONLY = ["automation.rolete_ujutro", "automation.rolete_ujutro_klinci",
             "automation.otvori_sjenila_kad_je_kasno"]


def req(path, data=None, method=None):
    r = urllib.request.Request(B + path, headers=H, method=method,
                               data=json.dumps(data).encode() if data is not None else None)
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode() or "{}")


def floors(set_point):
    return [{"action": "climate.set_temperature", "target": {"entity_id": e},
             "data": {"temperature": "{{ states('input_number.%s')|float + states('input_number.%s')|float(0) }}"
                      % (set_point, o)}} for e, o in FLOORS]


def between(after, before, sequence):
    return {"if": [{"condition": "time", "after": after, "before": before}], "then": sequence}


def main():
    states = req("/api/states")
    ids = {s["entity_id"]: s["attributes"].get("id") for s in states if s["entity_id"].startswith("automation.")}
    backup = Path("../backups") / f"automations-{dt.date.today():%Y%m%d}"
    backup.mkdir(parents=True, exist_ok=True)
    for entity, aid in ids.items():
        (backup / f"{entity}.json").write_text(json.dumps(req(f"/api/config/automation/config/{aid}"), indent=1))
    print("backed up", len(ids), "automations to", backup)

    req("/api/config/automation/config/house_mode_away_underfloor_dormant", {
        "id": "house_mode_away_underfloor_dormant",
        "alias": "House mode Away - close up the house",
        "description": "Leaving: heating off (floors to their 18° rest), ACs and terrace heat off, "
                       "roof windows closed, shutters down, lights off. (House Knob)",
        "triggers": [{"trigger": "state", "entity_id": "input_select.house_mode", "to": "Away"}],
        "conditions": [],
        "actions": [
            {"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.indoors_schedule"}},
            *floors("indoors_away"),
            {"action": "climate.turn_off", "target": {"entity_id": INDOOR_ACS + TERRACE}},
            {"action": "cover.close_cover", "target": {"entity_id": WINDOWS}},
            {"action": "cover.set_cover_position", "target": {"entity_id": SHUTTERS_MAIN + SHUTTER_KIDS},
             "data": {"position": 0}},
            {"action": "light.turn_off", "target": {"entity_id": LIGHTS}},
        ],
        "mode": "single"})
    req("/api/config/automation/config/house_mode_home_underfloor_restore", {
        "id": "house_mode_home_underfloor_restore",
        "alias": "House mode Home - wake the house",
        "description": "Arriving: shutters up if it is daytime; floors back on schedule if heating "
                       "is on. Nothing else moves by itself. (House Knob)",
        "triggers": [{"trigger": "state", "entity_id": "input_select.house_mode", "to": "Home"}],
        "conditions": [],
        "actions": [
            {"if": [SCHEDULE_ON], "then": [
                {"choose": [{"conditions": [{"condition": "time", "after": "06:00:00", "before": "23:00:00"}],
                             "sequence": floors("indoors_wake")}],
                 "default": floors("indoors_sleep")}]},
            between("09:00:00", "21:00:00", [
                {"action": "cover.set_cover_position", "target": {"entity_id": SHUTTERS_MAIN},
                 "data": {"position": 100}}]),
            between("10:00:00", "21:00:00", [
                {"action": "cover.set_cover_position", "target": {"entity_id": SHUTTER_KIDS},
                 "data": {"position": 100}}]),
        ],
        "mode": "single"})
    print("rewrote the Away and Home automations")

    for entity in HOME_ONLY:
        cfg = req(f"/api/config/automation/config/{ids[entity]}")
        key = "conditions" if "conditions" in cfg or "condition" not in cfg else "condition"
        conds = list(cfg.get(key) or [])
        if HOME not in conds:
            cfg[key] = conds + [HOME]
            req(f"/api/config/automation/config/{ids[entity]}", cfg)
        print("home-only:", entity)

    req("/api/services/automation/turn_on", {"entity_id": [
        "automation.house_mode_away_underfloor_dormant",
        "automation.house_mode_home_restore_underfloor_schedule"]})

    for entity in DELETE:
        if entity in ids:
            req(f"/api/config/automation/config/{ids[entity]}", method="DELETE")
            print("deleted:", entity)


if __name__ == "__main__":
    main()
