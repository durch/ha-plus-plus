"""Minimal Home Assistant WebSocket client: keeps a live state cache for the
entities we care about and calls services. Reconnects forever."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable

import aiohttp

log = logging.getLogger("ha")


class HAClient:
    def __init__(self, url: str, token: str, watch: set[str],
                 on_change: Callable[[str], Awaitable[None]]):
        base = url.rstrip("/").replace("http", "ws", 1)
        # Inside an add-on, Core is reached through the Supervisor proxy, whose
        # WebSocket lives at /core/websocket (not /core/api/websocket).
        self.ws_url = base + ("/websocket" if base.endswith("supervisor/core") else "/api/websocket")
        self.token = token
        self.watch = watch
        self.on_change = on_change
        self.discover: Callable[[list[dict]], set[str]] | None = None
        self.states: dict[str, dict] = {}
        self.connected = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        # Dry run: read real HA state, but apply service calls to a local copy only.
        self.dry_run = False
        self._touched: set[str] = set()

    def load_fake(self, snapshot: dict) -> None:
        """Offline mode: no HA at all. Seed state from a snapshot file and accept every
        command optimistically (an "it all works" bridge for testing the physical knob)."""
        self.fake = True
        self.dry_run = True
        self.states = {s["entity_id"]: s for s in snapshot["states"] if s["entity_id"] in self.watch}

    async def run(self) -> None:
        if getattr(self, "fake", False):
            self.connected.set()
            await self.on_change("*")
            return
        backoff = 1
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self._session(session)
                    backoff = 1
                except Exception as e:  # noqa: BLE001 — reconnect on anything
                    log.warning("HA connection lost: %s", e)
                self.connected.clear()
                self._ws = None
                for f in self._pending.values():
                    if not f.done():
                        f.set_exception(ConnectionError("HA disconnected"))
                self._pending.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _session(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.ws_url, heartbeat=30, max_msg_size=0) as ws:
            msg = await ws.receive_json()
            assert msg["type"] == "auth_required", msg
            await ws.send_json({"type": "auth", "access_token": self.token})
            msg = await ws.receive_json()
            if msg["type"] != "auth_ok":
                raise RuntimeError(f"HA auth failed: {msg.get('message')}")
            log.info("connected to HA %s", msg.get("ha_version"))
            self._ws = ws
            reader = asyncio.create_task(self._read(ws))
            try:
                await self._send({"type": "subscribe_events", "event_type": "state_changed"})
                states = await self._send({"type": "get_states"})
                if self.discover:   # controls may want more entities once they see what exists
                    self.watch |= self.discover(states)
                self.states = {s["entity_id"]: s for s in states if s["entity_id"] in self.watch}
                missing = self.watch - self.states.keys()
                if missing:
                    log.warning("entities not found in HA: %s", sorted(missing))
                self.connected.set()
                await self.on_change("*")
                await reader
            finally:
                reader.cancel()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                break
            msg = raw.json()
            if msg.get("type") == "result":
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if msg.get("success"):
                        fut.set_result(msg.get("result"))
                    else:
                        fut.set_exception(RuntimeError((msg.get("error") or {}).get("message", "HA error")))
            elif msg.get("type") == "event":
                data = msg["event"].get("data") or {}
                eid = data.get("entity_id")
                if eid in self.watch and not (self.dry_run and eid in self._touched):
                    if data.get("new_state"):
                        self.states[eid] = data["new_state"]
                    else:
                        self.states.pop(eid, None)
                    await self.on_change(eid)
        raise ConnectionError("HA websocket closed")

    async def _send(self, payload: dict, timeout: float = 15):
        if not self._ws:
            raise ConnectionError("HA not connected")
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json({"id": mid, **payload})
        return await asyncio.wait_for(fut, timeout)

    async def call(self, domain: str, service: str, data: dict) -> None:
        log.info("%s %s.%s %s", "DRY-RUN" if self.dry_run else "call", domain, service, data)
        if self.dry_run:
            self._fake(domain, service, data)
            return
        await self._send({"type": "call_service", "domain": domain,
                          "service": service, "service_data": data})

    # ── dry run ──
    def _fake(self, domain: str, service: str, data: dict) -> None:
        """Approximate the effect of a service call on the local state copy."""
        ids = data.get("entity_id")
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        if domain == "scene":
            return  # presets: the light stays as-is in a dry run
        for eid in ids:
            s = self.states.get(eid)
            if not s:
                continue
            s = {**s, "attributes": dict(s.get("attributes") or {})}
            a = s["attributes"]
            if domain == "light":
                on = {"turn_on": True, "turn_off": False}.get(service, s["state"] != "on")
                s["state"] = "on" if on else "off"
                if "brightness_pct" in data:
                    a["brightness"] = round(data["brightness_pct"] * 255 / 100)
                elif on and not a.get("brightness"):
                    a["brightness"] = 255
            elif domain == "climate":
                if "hvac_mode" in data:
                    s["state"] = data["hvac_mode"]
                if "temperature" in data:
                    a["temperature"] = data["temperature"]
            elif domain == "cover":
                pos = {"open_cover": 100, "close_cover": 0}.get(service, data.get("position"))
                if pos is not None:
                    a["current_position"] = pos
                    s["state"] = "open" if pos > 0 else "closed"
            self.states[eid] = s
            self._touched.add(eid)
            asyncio.get_running_loop().create_task(self.on_change(eid))
