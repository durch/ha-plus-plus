# House Knob protocol (hk/1) — draft

Status: DRAFT for review. Purpose-built for house control; reuses roon-knob's
*principles*, not its API.

## Principles kept from roon-knob

| Principle | How it shows up here |
|---|---|
| Thin firmware | Knob renders state and sends input. No HA knowledge, no entity ids, no grouping logic. |
| Bridge owns logic | HA connection, groups (house/terrace climate), interlocks (windows open while heating), debouncing all live in the bridge. |
| Bridge serves OTA | `GET /firmware/version`, `GET /firmware/download` (same shape v2.8.4 already calls, so the first custom flash can go over WiFi). |
| Zero-config discovery | Bridge advertises mDNS `_haknob._tcp`; manual URL override in knob settings. |
| Predict locally, reconcile | Knob moves the arc immediately on rotation; bridge's state is authoritative and wins after settle. |

## What changes vs roon-knob

| roon-knob | hk/1 | Why |
|---|---|---|
| Knob polls `/now_playing` (+ UDP fast path) | One WebSocket, bridge **pushes** state | House state changes elsewhere (phone, automations); push = instant, no poll traffic. |
| "Zone" + media fields (line1-3, volume) | Typed **controls** (`climate`, `light`, `cover`, `action`) | Knob can draw a thermostat as a thermostat, not as a fake track. |
| Flat zone picker | **Sections** → controls (Climate / Lights / Windows) | ~30 controls; flat list is too long for a round screen. |
| Artwork pipeline | Icon names (Material Symbols) | No images to fetch/decode. |

## Transport

`ws://<bridge>:8765/knob/ws?knob_id=<mac6>&fw=<version>` — JSON text frames.
Knob reconnects with backoff (1s → 30s). On (re)connect the bridge sends
`hello`, `layout`, then `state` for every control.

## Bridge → knob

```jsonc
{"t":"hello","proto":1,"bridge":"0.1.0","settings":{"dim_s":30,"sleep_s":120}}

{"t":"layout","rev":"a1b2c3d4","sections":[
  {"id":"climate","name":"Climate","icon":"thermostat","controls":["house","terrace"]},
  {"id":"lights","name":"Lights","icon":"lightbulb","controls":["light.kuca", "..."]},
  {"id":"windows","name":"Windows","icon":"window","controls":["windows_all","cover.velux_window_roof_window", "..."]}
]}

{"t":"state","id":"house","kind":"climate","name":"House",
 "available":true,"on":true,
 "mode":"heat","modes":["off","heat","cool"],
 "value":21.0,"min":15,"max":28,"step":0.5,"unit":"°C",
 "primary":"21.0°","secondary":"Now 19.5°",
 "warning":"3 windows open"}            // optional, knob shows amber banner

{"t":"state","id":"light.kuca","kind":"light","name":"Kuća",
 "available":true,"on":true,"value":60,"min":0,"max":100,"step":5,"unit":"%",
 "primary":"60%","secondary":null}

{"t":"state","id":"cover.velux_window_roof_window","kind":"cover","name":"Kuhinja window",
 "available":true,"on":true,"value":100,"min":0,"max":100,"step":10,"unit":"%",
 "primary":"Open","secondary":null}

{"t":"ack","seq":17,"ok":false,"message":"Terrace heater unavailable"}
```

`value`/`min`/`max`/`step` is the one adjustable quantity per control
(setpoint, brightness, position). `adjustable:false` disables rotation.

## Knob → bridge

```jsonc
{"t":"adjust","seq":17,"id":"house","value":21.5}   // absolute; bridge debounces ~400 ms
{"t":"toggle","seq":18,"id":"light.kuca"}           // on/off, open/close, climate on (last mode)/off
{"t":"mode","seq":19,"id":"house","mode":"cool"}
{"t":"focus","id":"house"}                          // optional: bridge may prioritise updates
{"t":"status","battery":82,"charging":false,"rssi":-61}
```

## Knob UI (round 360×360)

- **Home**: sections as a ring/list; rotate to choose, tap to enter.
- **Control screen**: arc = `value` within `min..max`; centre = `primary`; below = `secondary`; amber banner = `warning`.
  - Rotate: adjust value (velocity-sensitive, as roon-knob).
  - Tap: toggle.
  - Swipe left/right: cycle `modes` (climate only).
  - Press knob: back to section list.

## Climate logic (bridge)

| Control | Heat drives | Cool drives |
|---|---|---|
| House | floor: Dnevni, Dnevni 2, Hodnik | ACs: Klima Dnevni, Spavaća, Klinci |
| Terrace | Heater, Wonderwall Art Smart ×2 (`heat_cool` mode) | AC `580d0d34e617` |

Switching mode turns the other group off. `value` is the shared setpoint for the
active group. House heat shows a warning while any roof window is open.
