"""One-off HA migration for the Indoors floor schedule (bespoke to this house).

Creates the set point / offset / switch helpers, the wake and sleep automations,
rewrites the Away / Home-restore automations to use them, switches the four old
per-room automations off and removes the five interim per-room helpers.

Run from house_knob/ with HA_URL and HA_TOKEN set:
    uv run python ../scripts/apply_schedule_helpers.py
Originals of the rewritten automations are expected in ../backups/.
"""

import asyncio
import json
import os
import urllib.request

import aiohttp

URL = os.environ["HA_URL"].replace("http", "ws", 1) + "/api/websocket"
B = os.environ["HA_URL"]
H = {"Authorization": "Bearer " + os.environ["HA_TOKEN"], "Content-Type": "application/json"}

NUM = [
    ("indoors_wake", "Indoors wake", 25.0, 10, 35),
    ("indoors_sleep", "Indoors sleep", 22.0, 10, 35),
    ("indoors_away", "Indoors away", 18.0, 5, 35),
    # Offsets assume Tuya floor calibration 0 on every thermostat.
    ("offset_dnevni", "Offset Dnevni", 0.0, -8, 8),
    ("offset_dnevni_2", "Offset Dnevni 2", 0.0, -8, 8),
    ("offset_hodnik", "Offset Hodnik", 0.0, -8, 8),
]
FLOORS = [("climate.dnevni", "offset_dnevni"), ("climate.dnevni_2", "offset_dnevni_2"),
          ("climate.hodnik", "offset_hodnik")]
OLD_AUTOMATIONS = ["automation.dnevni_underfloor_day_target", "automation.dnevni_underfloor_night_target",
                   "automation.dnevni_2_underfloor_day_target", "automation.dnevni_2_underfloor_night_target"]
OLD_HELPERS = ["dnevni_floor_day", "dnevni_floor_night", "dnevni_2_floor_day", "dnevni_2_floor_night", "floor_away"]

SCHED = {"condition": "state", "entity_id": "input_boolean.indoors_schedule", "state": "on"}
HOME = {"condition": "state", "entity_id": "input_select.house_mode", "state": "Home"}


def acts(set_point: str) -> list[dict]:
    return [{"action": "climate.set_temperature", "target": {"entity_id": entity},
             "data": {"temperature": "{{ states('input_number.%s')|float + states('input_number.%s')|float(0) }}"
                      % (set_point, offset)}}
            for entity, offset in FLOORS]


def post(automation_id: str, config: dict) -> None:
    config = {"id": automation_id, **config}
    req = urllib.request.Request(f"{B}/api/config/automation/config/{automation_id}",
                                 data=json.dumps(config).encode(), headers=H, method="POST")
    print(automation_id, urllib.request.urlopen(req, timeout=20).read().decode())


async def main() -> None:
    async with aiohttp.ClientSession() as session, session.ws_connect(URL) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": os.environ["HA_TOKEN"]})
        print((await ws.receive_json())["type"])
        mid = 1

        async def call(payload: dict) -> dict:
            nonlocal mid
            mid += 1
            await ws.send_json({"id": mid, **payload})
            while True:
                msg = await ws.receive_json()
                if msg.get("id") == mid:
                    return msg

        async def service(domain: str, name: str, data: dict) -> bool:
            r = await call({"type": "call_service", "domain": domain, "service": name, "service_data": data})
            return r["success"]

        have = {x["id"] for x in (await call({"type": "input_number/list"}))["result"]}
        for hid, name, val, lo, hi in NUM:
            if hid not in have:
                r = await call({"type": "input_number/create", "name": name, "min": lo, "max": hi, "step": 0.5,
                                "unit_of_measurement": "°C", "mode": "box", "icon": "mdi:heating-coil",
                                "initial": val})
                print("create", hid, r["success"], r.get("error"))
            print(" set", hid, val, await service("input_number", "set_value",
                                                  {"entity_id": f"input_number.{hid}", "value": val}))

        bools = {x["id"] for x in (await call({"type": "input_boolean/list"}))["result"]}
        if "indoors_schedule" not in bools:
            r = await call({"type": "input_boolean/create", "name": "Indoors schedule",
                            "icon": "mdi:calendar-clock", "initial": True})
            print("create indoors_schedule", r["success"], r.get("error"))
            await service("input_boolean", "turn_on", {"entity_id": "input_boolean.indoors_schedule"})

        post("indoors_floor_wake", {
            "alias": "Indoors floors - wake",
            "description": "06:00: every floor = wake set point + its offset (House Knob)",
            "triggers": [{"trigger": "time", "at": "06:00:00"}], "conditions": [HOME, SCHED],
            "actions": acts("indoors_wake"), "mode": "single"})
        post("indoors_floor_sleep", {
            "alias": "Indoors floors - sleep",
            "description": "23:00: every floor = sleep set point + its offset (House Knob)",
            "triggers": [{"trigger": "time", "at": "23:00:00"}], "conditions": [HOME, SCHED],
            "actions": acts("indoors_sleep"), "mode": "single"})

        away = json.load(open("../backups/automation-house_mode_away_underfloor_dormant.json"))
        away["conditions"] = [SCHED]
        away["actions"] = acts("indoors_away")
        post("house_mode_away_underfloor_dormant", away)

        home = json.load(open("../backups/automation-house_mode_home_underfloor_restore.json"))
        home["conditions"] = [SCHED]
        home["actions"] = [{"choose": [{"conditions": [{"condition": "time", "after": "06:00:00",
                                                        "before": "23:00:00"}],
                                        "sequence": acts("indoors_wake")}],
                            "default": acts("indoors_sleep")}]
        post("house_mode_home_underfloor_restore", home)

        print("old automations off", await service("automation", "turn_off", {"entity_id": OLD_AUTOMATIONS}))
        for hid in OLD_HELPERS:
            r = await call({"type": "input_number/delete", "input_number_id": hid})
            print("delete", hid, r["success"], r.get("error"))


asyncio.run(main())
