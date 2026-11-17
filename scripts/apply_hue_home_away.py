"""One-off HA migration: Hue bridge automations follow Home / Away.

On Away the enabled Hue automations are remembered (input_text.hue_automations_before_away)
and all switched off; on Home exactly those are switched back on.
Run from house_knob/ with HA_URL and HA_TOKEN set.
"""

import asyncio
import json
import os
import urllib.request

import aiohttp

B = os.environ["HA_URL"]
H = {"Authorization": "Bearer " + os.environ["HA_TOKEN"], "Content-Type": "application/json"}
PREFIX = "switch.hue_bridge_automation_"
MEMO = "input_text.hue_automations_before_away"

REMEMBER = {"action": "input_text.set_value", "target": {"entity_id": MEMO}, "data": {"value": (
    "{{ states.switch | selectattr('entity_id', 'search', '^%s') | selectattr('state', 'eq', 'on') "
    "| map(attribute='entity_id') | map('replace', '%s', '') | join(',') }}" % (PREFIX, PREFIX))}}
ALL_OFF = {"action": "switch.turn_off", "target": {"entity_id": (
    "{{ states.switch | selectattr('entity_id', 'search', '^%s') | map(attribute='entity_id') | list }}" % PREFIX)}}
RESTORE = {"if": [{"condition": "template", "value_template": "{{ states('%s') not in ('', 'unknown', 'unavailable') }}" % MEMO}],
           "then": [{"action": "switch.turn_on", "target": {"entity_id": (
               "{{ states('%s').split(',') | map('regex_replace', '^', '%s') | list }}" % (MEMO, PREFIX))}}]}


def req(path, data=None):
    r = urllib.request.Request(B + path, headers=H, data=json.dumps(data).encode() if data is not None else None)
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode() or "{}")


async def ensure_helper():
    url = B.replace("http", "ws", 1) + "/api/websocket"
    async with aiohttp.ClientSession() as s, s.ws_connect(url) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": os.environ["HA_TOKEN"]})
        await ws.receive_json()
        await ws.send_json({"id": 1, "type": "input_text/list"})
        have = {x["id"] for x in (await ws.receive_json())["result"]}
        if "hue_automations_before_away" not in have:
            await ws.send_json({"id": 2, "type": "input_text/create", "name": "Hue automations before away",
                                "max": 255, "icon": "mdi:lightbulb-auto"})
            print("helper created:", (await ws.receive_json())["success"])


def main():
    asyncio.run(ensure_helper())
    away = req("/api/config/automation/config/house_mode_away_underfloor_dormant")
    if REMEMBER not in away["actions"]:
        away["actions"] += [REMEMBER, ALL_OFF]
        req("/api/config/automation/config/house_mode_away_underfloor_dormant", away)
    home = req("/api/config/automation/config/house_mode_home_underfloor_restore")
    if RESTORE not in home["actions"]:
        home["actions"].append(RESTORE)
        req("/api/config/automation/config/house_mode_home_underfloor_restore", home)
    print("away actions:", len(away["actions"]), "| home actions:", len(home["actions"]))


if __name__ == "__main__":
    main()
