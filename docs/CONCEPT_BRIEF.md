# House Knob interaction concepts — shared brief

You are designing ONE interaction concept for a physical house controller and
delivering it as a clickable browser mock. Other designers are doing different
concepts in parallel; yours must be genuinely its own idea, not a variant of a
rim-of-icons menu.

## The device

- Round touchscreen, 360 × 360 px, **46 mm across** (1 px ≈ 0.128 mm). AMOLED, black background is free.
- A rotary encoder ring around it: **turns only, no push**, detented, velocity-aware.
- Touch is position-aware (x, y), single finger. Gestures the firmware can detect: tap,
  long-press (400 ms), swipe in 4 directions, and "finger is currently down at (x, y)"
  while the knob turns.
- It sits on a table or wall in a living room. People use it in passing, often in the dark,
  often without reading. The owner's words: zones must be **big and easy to tap**; the
  current menu-style navigation is "needlessly clunky"; "think outside the box".
- Touch targets: no target smaller than ~70 px (9 mm) in either direction. Bigger is better.
  The corners outside the circle do not exist.

## What it controls (exactly these 8 things, bespoke to one house)

| id | name | kind | what the user does |
|---|---|---|---|
| `indoors` | Indoors | climate | Off / Heat / Cool; nudge warmer or cooler (relative, ±5° in 0.5 steps); "Save" the nudge |
| `outdoors` | Outdoors | climate | Off / Heat / Cool; nudge ±5° in 1° steps |
| `light.terasa` | Terasa | light (7 lamps) | on/off, dim 0–100 %, pick a preset (7 scenes) |
| `light.ambiance` | Ambiance | light (3 lamps) | on/off, dim, preset (8 scenes) |
| `light.reflektor` | Reflektor | light (1 lamp) | on/off, dim, preset (3 scenes) |
| `windows` | Windows | cover | all three roof windows: Open / Closed |
| `blinds` | Blinds | cover | Up / Down |
| `shutters` | Shutters | cover | Up / Down |

Climate shows the **room temperature** as its main number (may be "–" when the sensor is
off); the knob adjusts a relative nudge, not a target temperature.

## The protocol your mock must speak (already running)

Open `ws://<location.host>/knob/ws?knob_id=<your-concept>&fw=sim`. JSON text frames.

Bridge → you:
- `{"t":"hello","dry_run":true}`
- `{"t":"layout","sections":[{"id","name","controls":[ids…]}, …]}` (flatten for the 8 ids, in order)
- `{"t":"state","id","kind","name","available","on","mode","modes":[…],"value","min","max","step","unit","adjustable","primary","secondary","warning","actions":[…]}`
  - climate: `mode` ∈ off/heat/cool, `value` = nudge (−5…+5), `primary` = room temp text, `adjustable` false when off,
    `actions` contains `"save_default"` for indoors.
  - light: `value` = brightness %, `modes` = preset names, `mode` = active preset or "".
  - cover: `on` = open/up, `value` 0 or 100.
- `{"t":"ack","seq","ok","message"}` — show `message` briefly when present.

You → bridge (each with a fresh `"seq"`):
- `{"t":"adjust","id","value"}` absolute value (send while turning; bridge debounces). Covers: 0 or 100.
- `{"t":"toggle","id"}` lights: on/off. (Do not use toggle for climate; use `mode`.)
- `{"t":"mode","id","mode"}` climate mode, or a light preset name.
- `{"t":"action","id","name":"save_default"}`

The bridge runs in **dry run**: it reads the real house but changes nothing, and echoes
your commands back as state. So the mock is fully live and safe.

A working reference implementation of the protocol (and one concept, "focus ring") is
`house_knob/ha_knob/sim/concepts/focus-ring.html`. Read it for the wire details, then
do something different.

## Deliverable

One self-contained HTML file at `house_knob/ha_knob/sim/concepts/<your-slug>.html`:
- A 360 × 360 round "device" on the page. Mouse wheel (and ↑/↓ keys) = knob turn.
  Mouse click / press-and-hold / drag on the device = touch. If your concept uses
  "finger held while turning", holding the mouse button while scrolling the wheel must work.
- All hit-testing by geometry on (x, y) inside the circle, written so it could be ported
  to C on an ESP32 (zones as sectors / bands / rectangles; no DOM-element click handlers
  for the device's own zones).
- Below the device: a 3–5 line plain explanation of how to use it, and a small
  "DRY RUN" indicator from the hello message.
- At the very top of the file, an HTML comment block with: concept name, the one-sentence
  idea, how **turn**, **tap**, and **value adjustment** work, and honest **pros / cons**
  (include what gets worse than a plain menu).
- Fonts the firmware has today: Lato (~22 px), Noto Sans (~28 px), Montserrat 48 px digits;
  Material Symbols icons can be added later. Use Google Fonts for these in the mock.
  Visual polish is welcome (use the impeccable design skill: run the `impeccable` skill
  and follow it) but the interaction model is what is being judged.
- Do not edit any other file. Do not run or restart the bridge (it is running on
  http://localhost:8765; your page will be served at
  `http://localhost:8765/sim/concepts/<your-slug>.html` after a bridge restart that
  the lead will do). You may open your file to check it only through that URL once told,
  so verify by careful reading and, if you have a browser tool, by loading the file from
  disk with the WebSocket URL pointed at `ws://localhost:8765/knob/ws` when
  `location.protocol === 'file:'`.

## Final report (short)

Slug, the idea in one sentence, how turn / tap / adjust work, 3 pros, 3 cons, and the
smallest touch target in mm.
