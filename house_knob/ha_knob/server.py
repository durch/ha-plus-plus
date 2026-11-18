"""hk/1 bridge: WebSocket server for knobs, backed by Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path

import aiohttp
import yaml
from aiohttp import WSMsgType, web

from . import __version__
from .controls import ClimateControl, Layout, build
from .ha import HAClient

log = logging.getLogger("bridge")
ROOT = Path(__file__).resolve().parent
DEBOUNCE_S = 0.35
PANEL_WAIT_S = 25   # how long /panel/state holds a request when nothing has changed


class _Reply:
    """Stands in for a knob's WebSocket when a command arrives over HTTP: keeps the ack."""

    def __init__(self) -> None:
        self.ack: dict | None = None

    async def send_json(self, msg: dict) -> None:
        if msg.get("t") == "ack":
            self.ack = msg


class Bridge:
    def __init__(self, layout: Layout, ha_url: str, token: str, firmware_dir: Path | None = None,
                 settings: dict | None = None):
        self.layout = layout
        self.settings = settings or {}   # knob-side settings sent in hello (screen rotation)
        self.firmware_dir = firmware_dir
        self.knobs: set[web.WebSocketResponse] = set()
        watch = set().union(*(c.entities() for c in layout.controls.values()))
        self.ha = HAClient(ha_url, token, watch, self._on_ha_change)
        self.ha.discover = lambda states: set().union(
            *(c.discover(states) for c in layout.controls.values() if hasattr(c, "discover")))
        self._pending_adjust: dict[str, float] = {}
        self._adjust_tasks: dict[str, asyncio.Task] = {}
        self._changed = asyncio.Event()   # wakes panels waiting in /panel/state
        layout_json = json.dumps(layout.sections, sort_keys=True).encode()
        self.layout_msg = {"t": "layout", "rev": hashlib.sha1(layout_json).hexdigest()[:8],
                           "sections": layout.sections}

    # ── state fan-out ──
    def _state(self, cid: str) -> dict:
        return self.layout.controls[cid].state(self.ha.states)

    async def _broadcast(self, msg: dict) -> None:
        self._changed.set()
        self._changed = asyncio.Event()
        for ws in list(self.knobs):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                self.knobs.discard(ws)

    async def _on_ha_change(self, eid: str) -> None:
        for cid, ctl in self.layout.controls.items():
            if eid == "*" or eid in ctl.entities():
                await self._broadcast(self._state(cid))

    # ── knob input ──
    async def _handle(self, ws: web.WebSocketResponse, msg: dict) -> None:
        t, cid, seq = msg.get("t"), msg.get("id"), msg.get("seq")
        if t == "status":
            log.info("knob status %s", {k: v for k, v in msg.items() if k != "t"})
            return
        if t == "focus":
            return
        ctl = self.layout.controls.get(cid)
        if not ctl:
            await ws.send_json({"t": "ack", "seq": seq, "ok": False, "message": "Unknown control"})
            return
        if not self.ha.connected.is_set():
            await ws.send_json({"t": "ack", "seq": seq, "ok": False, "message": "Home Assistant offline"})
            return
        try:
            if t == "adjust":
                self._schedule_adjust(cid, float(msg["value"]))
            elif t == "toggle":
                await ctl.toggle(self.ha.states, self.ha.call)
            elif t == "mode":
                await ctl.set_mode(self.ha.states, str(msg["mode"]), self.ha.call)
            elif t == "action":
                note = await ctl.action(self.ha.states, str(msg["name"]), self.ha.call)
                await ws.send_json({"t": "ack", "seq": seq, "ok": True, "message": note})
                await self._broadcast(self._state(cid))
                return
            else:
                raise ValueError(f"unknown message type {t!r}")
            await ws.send_json({"t": "ack", "seq": seq, "ok": True})
            # Controls may hold bridge-side state (remembered setpoints) — push it.
            await self._broadcast(self._state(cid))
        except Exception as e:  # noqa: BLE001
            log.warning("%s on %s failed: %s", t, cid, e)
            await ws.send_json({"t": "ack", "seq": seq, "ok": False, "message": str(e)[:80]})

    def _schedule_adjust(self, cid: str, value: float) -> None:
        """Knob sends a stream of absolute values while turning; apply the last one."""
        self._pending_adjust[cid] = value
        if task := self._adjust_tasks.get(cid):
            task.cancel()

        async def apply():
            await asyncio.sleep(DEBOUNCE_S)
            value = self._pending_adjust.pop(cid)
            try:
                await self.layout.controls[cid].adjust(self.ha.states, value, self.ha.call)
                await self._broadcast(self._state(cid))
            except Exception as e:  # noqa: BLE001
                log.warning("adjust %s=%s failed: %s", cid, value, e)

        self._adjust_tasks[cid] = asyncio.create_task(apply())

    # ── HTTP ──
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        knob = request.query.get("knob_id", "?")
        log.info("knob %s connected (fw %s) from %s", knob, request.query.get("fw"), request.remote)
        await ws.send_json({"t": "hello", "proto": 1, "bridge": __version__,
                            "ha": self.ha.connected.is_set(), "dry_run": self.ha.dry_run,
                            "settings": self.settings})
        await ws.send_json(self.layout_msg)
        for cid in self.layout.controls:
            await ws.send_json(self._state(cid))
        self.knobs.add(ws)
        try:
            async for raw in ws:
                if raw.type == WSMsgType.TEXT:
                    try:
                        await self._handle(ws, json.loads(raw.data))
                    except (ValueError, KeyError) as e:
                        await ws.send_json({"t": "ack", "ok": False, "message": f"bad message: {e}"})
        finally:
            self.knobs.discard(ws)
            log.info("knob %s disconnected", knob)
        return ws

    # ── panel (e-ink browsers: plain HTTP, no WebSocket) ──
    def _panel_snapshot(self) -> dict:
        controls = []
        for cid, ctl in self.layout.controls.items():
            state = self._state(cid)
            if labels := getattr(ctl, "labels", None):   # covers: what the two ends are called
                state["labels"] = list(labels)
            controls.append(state)
        rev = hashlib.sha1(json.dumps(controls, sort_keys=True).encode()).hexdigest()[:8]
        return {"rev": rev, "ha": self.ha.connected.is_set(), "dry_run": self.ha.dry_run,
                "controls": controls}

    async def panel_state(self, request: web.Request) -> web.Response:
        """GET /panel/state?rev=<last seen>: answers at once when the house differs from
        `rev`, otherwise holds the request until it does (at most PANEL_WAIT_S)."""
        seen = request.query.get("rev")
        deadline = asyncio.get_running_loop().time() + PANEL_WAIT_S
        while True:
            changed = self._changed          # grab before reading state: no missed wake-up
            snap = self._panel_snapshot()
            left = deadline - asyncio.get_running_loop().time()
            if snap["rev"] != seen or left <= 0:
                return web.json_response(snap, headers={"Cache-Control": "no-store"})
            try:
                # Pending taps expire by the clock, not by an HA event: re-check every second.
                await asyncio.wait_for(changed.wait(), min(left, 1.0))
            except asyncio.TimeoutError:
                pass

    async def panel_cmd(self, request: web.Request) -> web.Response:
        """POST /panel/cmd: one hk/1 knob message (adjust / toggle / mode / action) as
        JSON; answers with its ack and the house as it is shown right after."""
        try:
            msg = json.loads(await request.text())
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except ValueError as e:
            return web.json_response({"ok": False, "message": f"bad message: {e}"}, status=400)
        reply = _Reply()
        try:
            await self._handle(reply, msg)
        except (ValueError, KeyError) as e:
            reply.ack = {"ok": False, "message": f"bad message: {e}"}
        ack = reply.ack or {"ok": True}
        return web.json_response({"ok": bool(ack.get("ok")), "message": ack.get("message"),
                                  **self._panel_snapshot()})

    async def firmware_version(self, request: web.Request) -> web.Response:
        meta = self.firmware_dir and self.firmware_dir / "version.json"
        if not meta or not meta.exists():
            return web.json_response({"error": "no firmware"}, status=404)
        return web.json_response(json.loads(meta.read_text()))

    async def firmware_download(self, request: web.Request) -> web.StreamResponse:
        meta = self.firmware_dir and self.firmware_dir / "version.json"
        if not meta or not meta.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(self.firmware_dir / json.loads(meta.read_text())["file"])

    async def _admin(self, request: web.Request) -> None:
        """Admin endpoints change devices, so they need a valid Home Assistant token
        (`Authorization: Bearer <long-lived token>`), checked against HA itself."""
        auth = request.headers.get("Authorization", "")
        core = os.environ.get("HA_CORE_URL", "http://homeassistant:8123")
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with (aiohttp.ClientSession(timeout=timeout) as session,
                        session.get(f"{core}/api/", headers={"Authorization": auth}) as r):
                ok = auth.startswith("Bearer ") and r.status == 200
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            raise web.HTTPUnauthorized(text="admin endpoints need a Home Assistant token")

    async def firmware_upload(self, request: web.Request) -> web.Response:
        """PUT /admin/firmware?version=3.0.1 with the .bin as the body: stores it where
        the knob's updater looks (/firmware/version, /firmware/download)."""
        await self._admin(request)
        version = request.query.get("version", "")
        if not self.firmware_dir or not version.replace(".", "").isdigit():
            return web.json_response({"ok": False, "error": "need ?version=x.y.z and a firmware dir"}, status=400)
        data = await request.read()
        if len(data) < 100_000 or data[0] != 0xE9:   # ESP image magic
            return web.json_response({"ok": False, "error": "not an ESP firmware image"}, status=400)
        self.firmware_dir.mkdir(parents=True, exist_ok=True)
        (self.firmware_dir / "hiphi_dial.bin").write_bytes(data)
        (self.firmware_dir / "version.json").write_text(
            json.dumps({"version": version, "file": "hiphi_dial.bin", "size": len(data)}))
        log.info("firmware %s stored (%d bytes)", version, len(data))
        return web.json_response({"ok": True, "version": version, "size": len(data)})

    async def ota(self, request: web.Request) -> web.Response:
        """POST /admin/ota: ask every connected knob to check for and install an update."""
        await self._admin(request)
        await self._broadcast({"t": "ota"})
        return web.json_response({"ok": True, "knobs": len(self.knobs)})

    async def adopt(self, request: web.Request) -> web.Response:
        """Point a knob at a bridge by writing its Bridge URL through the knob's own
        config page: POST /admin/adopt?knob=<ip>&bridge=http://<this bridge>:8765.
        For when the knob is reachable from the bridge's host but not from where you sit
        (client isolation on the IoT WiFi)."""
        await self._admin(request)
        knob, bridge_url = request.query.get("knob", ""), request.query.get("bridge", "")
        try:
            if not ipaddress.ip_address(knob).is_private or not bridge_url.startswith("http://"):
                raise ValueError
        except ValueError:
            return web.json_response({"ok": False, "error": "need knob=<private ip>&bridge=http://..."}, status=400)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.post(f"http://{knob}/config", data={"bridge": bridge_url}) as r:
                    saved = r.status == 200
                async with session.get(f"http://{knob}/") as r:
                    page = await r.text()
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"knob not reachable: {e}"}, status=502)
        log.info("adopt: knob %s -> %s (saved=%s)", knob, bridge_url, saved)
        return web.json_response({"ok": saved, "knob_shows_bridge": bridge_url in page})

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ha": self.ha.connected.is_set(),
                                  "knobs": len(self.knobs), "controls": len(self.layout.controls)})

    def app(self) -> web.Application:
        app = web.Application(client_max_size=4 * 1024 * 1024)   # room for a firmware image
        app.router.add_get("/knob/ws", self.ws_handler)
        app.router.add_get("/firmware/version", self.firmware_version)
        app.router.add_get("/firmware/download", self.firmware_download)
        app.router.add_get("/healthz", self.health)
        app.router.add_post("/admin/adopt", self.adopt)
        app.router.add_put("/admin/firmware", self.firmware_upload)
        app.router.add_post("/admin/ota", self.ota)
        # The simulator is the knob's real interaction model in a browser; the other
        # concepts explored on the way are kept beside it for reference.
        app.router.add_get("/panel", lambda r: web.HTTPFound("/panel/"))
        app.router.add_get("/panel/", lambda r: web.FileResponse(
            ROOT / "panel" / "index.html", headers={"Cache-Control": "no-cache"}))
        app.router.add_get("/panel/state", self.panel_state)
        app.router.add_post("/panel/cmd", self.panel_cmd)
        app.router.add_get("/", lambda r: web.HTTPFound("/sim/"))
        app.router.add_get("/sim/", lambda r: web.FileResponse(ROOT / "sim" / "concepts" / "touch-and-turn.html"))
        app.router.add_get("/sim/concepts/", lambda r: web.FileResponse(ROOT / "sim" / "concepts" / "index.html"))
        app.router.add_static("/sim/concepts/", ROOT / "sim" / "concepts")

        async def start_ha(app):
            app["ha_task"] = asyncio.create_task(self.ha.run())

        async def stop_ha(app):
            app["ha_task"].cancel()

        app.on_startup.append(start_ha)
        app.on_cleanup.append(stop_ha)
        return app


def attach_store(layout: Layout, path: Path) -> None:
    """Persist climate room targets across restarts."""
    climates = {cid: c for cid, c in layout.controls.items() if isinstance(c, ClimateControl)}
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    for cid, ctl in climates.items():
        ctl.targets.update({k: float(v) for k, v in (saved.get(cid) or {}).items()})

    def save() -> None:
        try:
            path.write_text(json.dumps({cid: c.targets for cid, c in climates.items()}))
        except OSError as e:
            log.warning("could not save state to %s: %s", path, e)

    for ctl in climates.values():
        ctl.store = save


def main() -> None:
    p = argparse.ArgumentParser(description="House Knob bridge (hk/1)")
    p.add_argument("--config", default="knob.yaml")
    p.add_argument("--firmware-dir", default=None)
    p.add_argument("--state", default="state.json", help="where bridge-held targets persist")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--fake", metavar="SNAPSHOT",
                   help="no Home Assistant at all: start from a snapshot file, accept everything")
    p.add_argument("--dry-run", action="store_true",
                   help="read real HA state, but never change anything in HA")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = yaml.safe_load(Path(args.config).read_text())
    ha_url = os.environ.get("HA_URL", "http://offline") if args.fake else os.environ["HA_URL"]
    token = os.environ.get("HA_TOKEN", "") if args.fake else os.environ["HA_TOKEN"]
    layout = build(config)
    attach_store(layout, Path(args.state))
    bridge = Bridge(layout, ha_url, token,
                    Path(args.firmware_dir) if args.firmware_dir else None,
                    config.get("knob", {}))
    bridge.ha.dry_run = args.dry_run
    if args.fake:
        bridge.ha.load_fake(json.loads(Path(args.fake).read_text()))
        log.warning("FAKE: no Home Assistant; state from %s, every command succeeds locally", args.fake)
    if args.dry_run:
        log.warning("DRY RUN: knob input changes a local copy only, nothing is sent to HA")
    listen = config.get("listen", {})
    web.run_app(bridge.app(), host=listen.get("host", "0.0.0.0"), port=listen.get("port", 8765))


if __name__ == "__main__":
    main()
