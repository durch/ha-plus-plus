# Firmware plan — hk/1 on the v2.8.4 fork

Fork: `ha++/firmware` (branch `ha-knob`, from roon-knob tag v2.8.4 — what the
knob runs today). Toolchain: ESP-IDF v6.1 at `~/esp/esp-idf-v6.1`.
Baseline (unchanged v2.8.4) builds: `hiphi_dial.bin` 2.18 MB of a 2.5 MB OTA slot.

## Approach: swap the two app-specific pieces, keep everything else

| Keep as-is | Replace / add |
|---|---|
| Display, LVGL, touch, encoder drivers (`idf_app/main/platform_*`) | `bridge_client_start()` → `hk_client_start()` (new `common/hk_client.c`, `esp_websocket_client`) |
| Input pipeline (`controller_input` → action handler) | Action handler → `hk_handle_action()` (new) |
| WiFi, captive-portal provisioning, config web page, mDNS | Media UI → `hk_ui.c` (new LVGL screens: section list, control dial) |
| OTA + bootloader rollback (`common/ota_update.c`) | Bridge URL default/discovery → `_haknob._tcp` |
| Display dim/sleep, battery | |

The seam is `controller_input_set_action_handler()` in `common/app_main.c`. The
existing input layer already turns hardware into semantic actions:

| Action today | hk/1 meaning |
|---|---|
| `COMMAND ADJUST_VOLUME_STEPS(n)` (knob turn, velocity-aware) | `adjust` value += n × step (or scroll lists) |
| `COMMAND TOGGLE_PLAYBACK` (tap) | `toggle` |
| `COMMAND NEXT/PREVIOUS_TRACK` (swipe) | `mode` next/prev (climate) |
| `OPEN_ZONE_PICKER` (knob press) | enter / back |
| `SHOW_SETTINGS` (long-press top) | existing settings screen (WiFi, bridge URL, OTA) |

`bridge_client.c` stays linked (other modules call into it) but is never started.

## OTA path for the first flash (no USB)

v2.8.4 accepts an image only if: same project name (`hiphi_dial`), version newer
than 2.8.4, same channel (no `-suffix` → stable). So we build as `3.0.0`.

1. Snapshot house state; point knob's Bridge URL at our bridge (the knob's config web page).
2. Bridge serves `/firmware/version` → `{"version":"3.0.0","file":...}` and the binary.
3. Trigger: Settings → Check for update on the knob (or the bridge's config `install` request).
4. Rollback: new image is only marked valid after 45 s up; a crash/reboot loop before that
   reverts to v2.8.4 automatically. A build that boots but misbehaves stays — so OTA code is
   left untouched and the bridge can push a fix the same way.

## Steps

1. `hk_client.c`: WebSocket connect/reconnect, parse `layout`/`state`, send input. Unit-test the
   JSON handling on host (no hardware).
2. `hk_ui.c`: section list + control dial (arc, primary/secondary, mode chips, warning banner),
   matching the browser simulator.
3. Wire into `app_main.c` behind a build flag; build; size check.
4. OTA to the knob, verify, iterate.
