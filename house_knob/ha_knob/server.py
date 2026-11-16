"""hk/1 bridge: WebSocket server for knobs, backed by Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

import yaml
from aiohttp import WSMsgType, web

from . import __version__
from .controls import ClimateControl, Layout, build
from .ha import HAClient

log = logging.getLogger("bridge")
ROOT = Path(__file__).resolve().parent
DEBOUNCE_S = 0.35


class Bridge:
    def __init__(self, layout: Layout, ha_url: str, token: str, firmware_dir: Path | None = None):
        self.layout = layout
        self.firmware_dir = firmware_dir
        self.knobs: set[web.WebSocketResponse] = set()
        watch = set().union(*(c.entities() for c in layout.controls.values()))
        self.ha = HAClient(ha_url, token, watch, self._on_ha_change)
        self._pending_adjust: dict[str, float] = {}
        self._adjust_tasks: dict[str, asyncio.Task] = {}
        layout_json = json.dumps(layout.sections, sort_keys=True).encode()
        self.layout_msg = {"t": "layout", "rev": hashlib.sha1(layout_json).hexdigest()[:8],
                           "sections": layout.sections}

    # ── state fan-out ──
    def _state(self, cid: str) -> dict:
        return self.layout.controls[cid].state(self.ha.states)

    async def _broadcast(self, msg: dict) -> None:
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
                            "ha": self.ha.connected.is_set(), "dry_run": self.ha.dry_run})
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

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ha": self.ha.connected.is_set(),
                                  "knobs": len(self.knobs), "controls": len(self.layout.controls)})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/knob/ws", self.ws_handler)
        app.router.add_get("/firmware/version", self.firmware_version)
        app.router.add_get("/firmware/download", self.firmware_download)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/", lambda r: web.HTTPFound("/sim/"))
        app.router.add_get("/sim/", lambda r: web.FileResponse(ROOT / "sim" / "index.html"))

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
    p.add_argument("--dry-run", action="store_true",
                   help="read real HA state, but never change anything in HA")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = yaml.safe_load(Path(args.config).read_text())
    ha_url = os.environ["HA_URL"]
    token = os.environ["HA_TOKEN"]
    layout = build(config)
    attach_store(layout, Path(args.state))
    bridge = Bridge(layout, ha_url, token,
                    Path(args.firmware_dir) if args.firmware_dir else None)
    bridge.ha.dry_run = args.dry_run
    if args.dry_run:
        log.warning("DRY RUN: knob input changes a local copy only, nothing is sent to HA")
    listen = config.get("listen", {})
    web.run_app(bridge.app(), host=listen.get("host", "0.0.0.0"), port=listen.get("port", 8765))


if __name__ == "__main__":
    main()
