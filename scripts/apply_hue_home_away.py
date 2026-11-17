"""One-off HA migration: Hue bridge automations follow Home / Away with fixed rules.

Home: Night, Sunset and Morning are switched on; the others keep their state.
Away: everything except Morning is switched off; Morning keeps its state.
Replaces the earlier remember-and-restore version. Run from house_knob/ with HA_URL and HA_TOKEN set.
"""

import json
import os
import urllib.request

B = os.environ["HA_URL"]
H = {"Authorization": "Bearer " + os.environ["HA_TOKEN"], "Content-Type": "application/json"}
PREFIX = "switch.hue_bridge_automation_"
MEMO = "input_text.hue_automations_before_away"
AWAY_ID = "house_mode_away_underfloor_dormant"
HOME_ID = "house_mode_home_underfloor_restore"

AWAY_OFF = {"action": "switch.turn_off", "target": {"entity_id": (
    "{{ states.switch | selectattr('entity_id', 'search', '^%s') "
    "| rejectattr('entity_id', 'eq', '%smorning') | map(attribute='entity_id') | list }}" % (PREFIX, PREFIX))}}
HOME_ON = {"action": "switch.turn_on", "target": {"entity_id": [PREFIX + n for n in ("night", "sunset", "morning")]}}


def req(path, data=None):
    r = urllib.request.Request(B + path, headers=H, data=json.dumps(data).encode() if data is not None else None)
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode() or "{}")


def is_hue(action):
    return PREFIX in json.dumps(action) or MEMO in json.dumps(action)


def main():
    for aid, new in ((AWAY_ID, AWAY_OFF), (HOME_ID, HOME_ON)):
        cfg = req(f"/api/config/automation/config/{aid}")
        cfg["actions"] = [a for a in cfg["actions"] if not is_hue(a)] + [new]
        req(f"/api/config/automation/config/{aid}", cfg)
        print(aid, "actions:", len(cfg["actions"]))


if __name__ == "__main__":
    main()
