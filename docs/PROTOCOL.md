# House Knob protocol (hk/1)

Purpose-built for house control; reuses roon-knob's
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
{"t":"adjust","seq":17,"id":"indoors","value":1.5}     // absolute value of the control's gauge; bridge debounces ~350 ms
{"t":"toggle","seq":18,"id":"light.ambiance"}          // lights: on/off. climate: next of Off → Heat → Cool
{"t":"mode","seq":19,"id":"light.ambiance","mode":"Relax"}   // a light preset (or a climate mode, directly)
{"t":"action","seq":20,"id":"indoors","name":"save_default"} // fold the nudge into the offsets
```

Covers take `adjust` with 0 or 100. Climate `toggle` is applied 1.5 s after the last
one, so cycling through Cool on the way to Off never starts the ACs. Every command is
answered with `{"t":"ack","seq":…,"ok":…,"message":…}`; a message, when present, is
shown briefly on the knob.

## What `value` means

| Control | `value` | Range |
|---|---|---|
| Light | brightness % | 0–100, step 5 |
| Cover group | 0 = down / closed, 100 = up / open | step 100 |
| Climate, relative group (Indoors heat) | nudge applied to every device, in lockstep | −5…+5 |
| Climate, absolute group (all cooling, Outdoors heat) | the target set on every device | the group's min…max |

## Knob UI

The interaction model is "touch and turn": eight wedges select, the hub toggles, the
ring adjusts. It is specified by its tests (`firmware/tests/test_hk_tt.c`) and can be
tried in a browser at `/sim/` on a running bridge. The other concepts considered are
under `/sim/concepts/`.

## Climate logic (bridge)

| Control | Heat drives | Cool drives |
|---|---|---|
| Indoors | floors Dnevni, Dnevni 2, Hodnik. Relative nudge; schedule via HA helpers (set point + per-floor offset, `input_boolean.indoors_schedule`) | ACs Dnevni, Spavaća, Klinci. Absolute target |
| Outdoors | Heater, Wonderwall ×2 (`heat_cool` mode). Absolute target | terrace AC. Absolute target |

Switching mode turns the other group off. Indoors heat shows a warning while a roof
window is open. See [DEPLOY.md](DEPLOY.md) for the helpers and the schedule.
