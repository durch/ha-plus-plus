# Deploying the House Knob

## Before the house: on Home Assistant (can be done remotely)

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/durch/ha-plus-plus`.
2. Install **House Knob bridge**. Leave `dry_run` **on**, start it, open the log.
   It should say `connected to HA` and list no missing entities.
3. Open `http://<ha-address>:8765/sim/concepts/touch-and-turn.html` to drive the
   bridge from a browser. In dry run nothing in the house changes.

The add-on has not been built on a real Supervisor yet. If it fails to build or
start, the add-on log is the thing to send back.

## At the house

1. **Knob on the house WiFi.** Power it on. If it does not know the network it opens
   its own setup network; join it and add the house WiFi. (Saved networks are kept,
   so it still works at the other location.)
2. **Point the knob at HA.** Browse to the knob's address (shown in its settings:
   rest a finger on the centre for 1.5 s while nothing is selected) and set
   Bridge URL to `http://<ha-lan-address>:8765`. The knob shows the wedges within
   a few seconds.
3. **Try it in dry run** first: every gesture shows up in the add-on log as `DRY-RUN …`.
4. **Take a snapshot, then go live.** From the Mac (Tailscale):
   `cd house_knob && set -a && . ../.envrc && set +a && uv run python -m ha_knob.snapshot save`
   then switch the add-on's `dry_run` off and restart it.
5. **Check on site**
   - Netatmo Oxymoron powered on → Indoors shows a temperature instead of "–".
   - Terrace Netatmo module, once installed → set `sensor:` for Outdoors in `knob.yaml`.
   - Hodnik's Tuya floor calibration is 0 (all three offsets assume 0).
   - House mode → Home, then Indoors → Heat: floors go to 25° by day / 22° at night.
   - Off on the knob switches `input_boolean.indoors_schedule` off; Heat switches it on.
   - Terrace heater and the second Wonderwall are "unavailable" in HA today.

## Undo

- Restore the house: `uv run python -m ha_knob.snapshot restore ../snapshots/<file>.json`
- Stop the knob changing anything: set `dry_run` on, or stop the add-on.
- Old floor schedule: the four per-room automations are switched off, not deleted;
  originals of the rewritten ones are in `backups/` on the Mac.
- Old knob firmware: `backups/knob-flash-before-3.0.0.bin` (USB flash).

## Updating the knob's firmware later

Build with a higher version (`idf.py -DHK_VERSION=3.0.1 build`), copy
`hiphi_dial.bin` plus a `version.json` (`{"version":"3.0.1","file":"hiphi_dial.bin","size":…}`)
into the add-on's `firmware/` folder (`addon_configs/…_house_knob/firmware/`). On the knob:
rest on the centre 1.5 s → Settings → check for update. A build that crashes within
45 s rolls back by itself.
