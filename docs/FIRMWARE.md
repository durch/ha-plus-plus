# Firmware notes — hk/1 on the roon-knob v2.8.4 fork

Fork: `ha++/firmware` (branch `ha-knob`, from roon-knob tag v2.8.4 — what the
knob runs today). Toolchain: ESP-IDF v6.1 at `~/esp/esp-idf-v6.1`.
Baseline (unchanged v2.8.4) builds: `hiphi_dial.bin` 2.18 MB of a 2.5 MB OTA slot.

## What is ours, what is stock

| Ours (`firmware/common/hk_*`) | Stock, untouched |
|---|---|
| `hk_model` – parses hk/1 layout and state | Display, LVGL, touch and encoder drivers |
| `hk_tt` – the touch-and-turn interaction (pure C, host-tested) | WiFi, captive-portal provisioning, config web page |
| `hk_ui` – the house screen (wedges, hub, icons) | OTA and bootloader rollback |
| `hk_client` – WebSocket, threads, input hooks | Display dim / sleep, battery, settings screens |

Seams into stock code: `app_main.c` starts `hk_client` instead of the Roon bridge
client; `platform_input_idf.c` offers encoder ticks to `hk_client_encoder` first;
`ui_network.c` hands WiFi events to the UI task. The house screen is its own LVGL
screen and is shown from boot; the stock UI appears for WiFi setup and when asked
for (rest on the idle hub for 1.5 s). The music UI is still compiled in but never shown.

Fonts: `scripts/hk_font_gen.py` (Pillow) generates `hk_icons_40` and `hk_digits_56`.
Host tests: `sh tests/run_hk_tests.sh`.

## OTA

The knob was first flashed over USB (it ran an older roon-knob build whose bootloader
had no rollback). From here on updates go over WiFi: an image is accepted only with
the same project name (`hiphi_dial`), a higher version on the same channel (no
`-suffix`), and it is marked valid after 45 s up; a crash before that reverts.
See [DEPLOY.md](DEPLOY.md) for the steps.
