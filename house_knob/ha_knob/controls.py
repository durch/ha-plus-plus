"""Control model: turns HA entity states into hk/1 `state` messages and
knob input (adjust / toggle / mode) into HA service calls.

Pure logic. `states` is a dict entity_id -> HA state object (dict with
"state" and "attributes"); `call(domain, service, data)` is an async callable.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable  # noqa: F401

Call = Callable[[str, str, dict], Awaitable[Any]]
States = dict[str, dict]

OFFSET_RANGE = 5.0     # how far the knob can nudge a climate group, °C
MODE_SETTLE_S = 1.5    # taps cycle modes; devices switch once tapping stops

UNAVAILABLE = {"unavailable", "unknown", None}


def _avail(states: States, eid: str) -> bool:
    s = states.get(eid)
    return bool(s) and s.get("state") not in UNAVAILABLE


def _attr(states: States, eid: str, key: str, default=None):
    s = states.get(eid) or {}
    return (s.get("attributes") or {}).get(key, default)


def _friendly(states: States, eid: str) -> str:
    return (_attr(states, eid, "friendly_name") or eid.split(".", 1)[1]).strip()


def _fmt_temp(v: float | None) -> str:
    return "–" if v is None else f"{v:.1f}°"


def _snap(value: float, lo: float, hi: float, step: float) -> float:
    value = min(max(value, lo), hi)
    snapped = round((value - lo) / step) * step + lo
    return round(min(max(snapped, lo), hi), 3)


class Control:
    id: str
    name: str
    icon: str
    kind: str

    def entities(self) -> set[str]:
        raise NotImplementedError

    def state(self, states: States) -> dict:
        raise NotImplementedError

    async def adjust(self, states: States, value: float, call: Call) -> None:
        raise NotImplementedError

    async def toggle(self, states: States, call: Call) -> None:
        raise NotImplementedError

    async def set_mode(self, states: States, mode: str, call: Call) -> None:
        raise ValueError(f"{self.id} has no modes")

    async def action(self, states: States, name: str, call: Call) -> str | None:
        raise ValueError(f"{self.id} has no action {name!r}")


# ── Climate ─────────────────────────────────────────────────────────────────


@dataclass
class Member:
    entity: str
    hvac_mode: str


@dataclass
class ModeGroup:
    mode: str  # "heat" | "cool"
    members: list[Member]
    min: float
    max: float
    step: float


@dataclass
class ClimateControl(Control):
    """Two-mode climate group. Each backing device keeps its own setpoint; the knob
    moves all devices of the active mode in lockstep (+N° on the knob = +N° on every
    device), so floor thermostats and ACs keep their natural relative offsets."""

    id: str
    name: str
    icon: str
    heat: ModeGroup
    cool: ModeGroup
    warn_if_open: list[str] = field(default_factory=list)
    sensor: str | None = None     # room temperature sensor: the temperature that matters
    kind: str = "climate"
    last_mode: str | None = None
    # Per-mode offset: how far the knob has nudged the devices (lockstep) from their
    # baseline. Persisted via `store`; reset by "Save as default".
    targets: dict[str, float] = field(default_factory=dict)
    store: Callable[[], None] | None = None
    # "Save as default": write devices' current setpoints into the schedule helpers
    # their automations read. [{entity, day, night}], plus house_mode/away helper.
    pending: tuple[str, float] | None = None   # (mode, monotonic deadline)
    _settle: Any = None
    # Heating schedule (HA helpers): a switch the schedule automations check, one set
    # point per period, one offset per thermostat. Device setpoint = set point + offset.
    # {switch, wake, sleep, away, offsets: {climate entity: input_number}}
    schedule: dict = field(default_factory=dict)
    house_mode: str = "input_select.house_mode"
    # Exact (unrounded) per-device setpoints, so 0.5° knob steps accumulate on
    # devices that only take whole degrees.
    exact: dict[str, float] = field(default_factory=dict)

    def entities(self) -> set[str]:
        ids = {m.entity for g in (self.heat, self.cool) for m in g.members}
        return (ids | set(self.warn_if_open) | ({self.sensor} if self.sensor else set())
                | self._schedule_entities())

    def _group(self, mode: str) -> ModeGroup:
        return self.heat if mode == "heat" else self.cool

    def current_mode(self, states: States) -> str:
        for group in (self.heat, self.cool):
            for m in group.members:
                s = states.get(m.entity)
                if s and s.get("state") == m.hvac_mode:
                    return group.mode
        return "off"

    def _default_mode(self) -> str:
        if self.last_mode:
            return self.last_mode
        month = _dt.date.today().month
        return "cool" if 5 <= month <= 9 else "heat"

    def _setpoints(self, states: States, group: ModeGroup) -> dict[str, float]:
        out = {}
        for m in group.members:
            t = _attr(states, m.entity, "temperature")
            if _avail(states, m.entity) and isinstance(t, (int, float)):
                out[m.entity] = float(t)
        return out

    def _value(self, states: States, group: ModeGroup) -> float:
        sp = self._setpoints(states, group)
        if not sp:
            return _snap((group.min + group.max) / 2, group.min, group.max, group.step)
        return _snap(sum(sp.values()) / len(sp), group.min, group.max, group.step)

    def _room_temp(self, states: States) -> float | None:
        if self.sensor and _avail(states, self.sensor):
            try:
                return float(states[self.sensor]["state"])
            except (TypeError, ValueError):
                return None
        return None

    def _current_temp(self, states: States, group: ModeGroup) -> float | None:
        if self.sensor:  # authoritative sensor only; device readings are differently calibrated
            return self._room_temp(states)
        temps = [_attr(states, m.entity, "current_temperature") for m in group.members
                 if _avail(states, m.entity)]
        temps = [float(t) for t in temps if isinstance(t, (int, float))]
        return sum(temps) / len(temps) if temps else None

    def shown_mode(self, states: States) -> str:
        """Mode to display: a just-tapped mode wins until the devices catch up."""
        actual = self.current_mode(states)
        if self.pending:
            mode, deadline = self.pending
            if actual == mode or time.monotonic() > deadline:
                self.pending = None
            else:
                return mode
        return actual

    def state(self, states: States) -> dict:
        mode = self.shown_mode(states)
        group = self._group(mode if mode != "off" else self._default_mode())
        now = self._current_temp(states, group)
        offset = self.targets.get(mode, 0.0) if mode != "off" else 0.0
        unavailable = [_friendly(states, m.entity) for m in group.members
                       if not _avail(states, m.entity)]
        warning = None
        if mode == "heat":
            open_ = [e for e in self.warn_if_open
                     if (states.get(e) or {}).get("state") in ("open", "opening")]
            if open_:
                warning = f"{len(open_)} window{'s' if len(open_) > 1 else ''} open"
        if not warning and unavailable and len(unavailable) < len(group.members):
            warning = f"{len(unavailable)} unavailable"
        if mode == "off":
            secondary = "Off"
        elif abs(offset) < 1e-6:
            secondary = "Turn to adjust"
        else:
            secondary = f"{'Warmer' if offset > 0 else 'Cooler'} {offset:+.1f}°"
        return {
            "t": "state", "id": self.id, "kind": self.kind, "name": self.name, "icon": self.icon,
            "available": len(unavailable) < len(group.members),
            "on": mode != "off", "mode": mode, "modes": ["off", "heat", "cool"],
            # The knob is relative: `value` is how far the devices have been nudged
            # from where they were; the big number is the room temperature.
            "value": offset, "min": -OFFSET_RANGE, "max": OFFSET_RANGE, "step": group.step,
            "unit": "°C", "adjustable": mode != "off",
            "primary": _fmt_temp(now),
            "secondary": secondary,
            "warning": warning,
            "actions": ["save_default"] if self.schedule else [],
        }

    def _schedule_entities(self) -> set[str]:
        sc = self.schedule
        if not sc:
            return set()
        return {self.house_mode, sc["switch"], sc["wake"], sc["sleep"], sc["away"], *sc["offsets"].values()}

    def _period(self, states: States) -> str:
        if (states.get(self.house_mode) or {}).get("state") == "Away":
            return "away"
        return "wake" if 6 <= _dt.datetime.now().hour < 23 else "sleep"

    @staticmethod
    def _num(states: States, eid: str, default: float | None = None) -> float | None:
        try:
            return float(states[eid]["state"])
        except (KeyError, TypeError, ValueError):
            return default

    async def action(self, states: States, name: str, call: Call) -> str | None:
        """Save as default: fold the knob's nudge into every thermostat's offset, so each
        later wake / sleep / away run keeps it."""
        if name != "save_default" or not self.schedule:
            raise ValueError(f"{self.id} has no action {name!r}")
        if self.shown_mode(states) != "heat":
            raise ValueError("Only while heating")
        nudge = self.targets.get("heat", 0.0)
        if abs(nudge) < 1e-6:
            return "Nothing to save"
        for helper in self.schedule["offsets"].values():
            cur = self._num(states, helper, 0.0)
            await call("input_number", "set_value", {"entity_id": helper, "value": round(cur + nudge, 1)})
        self.targets.pop("heat", None)
        if self.store:
            self.store()
        return f"Saved {nudge:+.1f}°"

    async def _schedule_on(self, states: States, on: bool, call: Call) -> None:
        if self.schedule:
            await call("input_boolean", "turn_on" if on else "turn_off",
                       {"entity_id": self.schedule["switch"]})

    async def adjust(self, states: States, value: float, call: Call) -> None:
        """Nudge every device of the active mode by the change in offset (lockstep)."""
        mode = self.shown_mode(states)
        if mode == "off":
            return
        group = self._group(mode)
        new = _snap(float(value), -OFFSET_RANGE, OFFSET_RANGE, group.step)
        delta = new - self.targets.get(mode, 0.0)
        self.targets[mode] = new
        if self.store:
            self.store()
        if abs(delta) < 1e-6:
            return
        for m in group.members:
            if not _avail(states, m.entity):
                continue
            cur = _attr(states, m.entity, "temperature")
            if not isinstance(cur, (int, float)):
                continue
            step = float(_attr(states, m.entity, "target_temp_step") or group.step)
            lo = float(_attr(states, m.entity, "min_temp", group.min))
            hi = float(_attr(states, m.entity, "max_temp", group.max))
            exact = self.exact.get(m.entity)
            if exact is None or abs(round(exact / step) * step - float(cur)) > 1e-6:
                exact = float(cur)  # changed outside the knob: re-base
            exact = min(max(exact + delta, lo), hi)
            self.exact[m.entity] = exact
            target = round(round(exact / step) * step, 2)
            if abs(target - float(cur)) > 1e-6:
                await call("climate", "set_temperature", {"entity_id": m.entity, "temperature": target})

    async def _apply(self, states: States, mode: str, call: Call) -> None:
        """Make `mode` active: switch its devices on (keeping their setpoints), the other group off."""
        if mode == "off":
            await self._schedule_on(states, False, call)   # Off is final: no schedule runs
            for group in (self.heat, self.cool):
                await self._off(states, group, call)
            return
        other = self._group("cool" if mode == "heat" else "heat")
        await self._off(states, other, call)
        await self._schedule_on(states, mode == "heat", call)
        base = self._num(states, self.schedule[self._period(states)]) if (self.schedule and mode == "heat") else None
        for m in self._group(mode).members:
            if not _avail(states, m.entity):
                continue
            offset_helper = self.schedule.get("offsets", {}).get(m.entity) if base is not None else None
            if offset_helper:
                # Entering Heat: straight to the schedule's value for this time of day.
                target = base + self._num(states, offset_helper, 0.0)
                await call("climate", "set_temperature",
                           {"entity_id": m.entity, "temperature": target, "hvac_mode": m.hvac_mode})
            elif states[m.entity].get("state") != m.hvac_mode:
                await call("climate", "set_hvac_mode", {"entity_id": m.entity, "hvac_mode": m.hvac_mode})
        if mode == "heat" and self.schedule:
            self.targets.pop("heat", None)   # fresh from the schedule: no nudge yet
            if self.store:
                self.store()
        self.last_mode = mode

    async def _off(self, states: States, group: ModeGroup, call: Call) -> None:
        ids = [m.entity for m in group.members
               if _avail(states, m.entity) and states[m.entity].get("state") != "off"]
        if ids:
            await call("climate", "set_hvac_mode", {"entity_id": ids, "hvac_mode": "off"})

    async def toggle(self, states: States, call: Call) -> None:
        """Tap cycles Off -> Heat -> Cool -> Off. The devices are only switched once the
        taps stop, so passing through Cool on the way to Off never starts the ACs."""
        order = ["off", "heat", "cool"]
        nxt = order[(order.index(self.shown_mode(states)) + 1) % 3]
        self.pending = (nxt, time.monotonic() + MODE_SETTLE_S + 8)
        if self._settle:
            self._settle.cancel()

        async def settle():
            await asyncio.sleep(MODE_SETTLE_S)
            if self.current_mode(states) != nxt:
                await self._apply(states, nxt, call)

        self._settle = asyncio.get_running_loop().create_task(settle())

    async def set_mode(self, states: States, mode: str, call: Call) -> None:
        if mode not in ("off", "heat", "cool"):
            raise ValueError(f"unknown mode {mode!r}")
        await self._apply(states, mode, call)


# ── Lights ──────────────────────────────────────────────────────────────────


@dataclass
class LightControl(Control):
    entity: str
    name_override: str | None = None
    icon: str = "lightbulb"
    scenes: dict[str, str] = field(default_factory=dict)  # preset name -> scene entity
    kind: str = "light"
    active_scene: str | None = None

    @property
    def id(self) -> str:  # type: ignore[override]
        return self.entity

    def name_for(self, states: States) -> str:
        return self.name_override or _friendly(states, self.entity)

    def _dimmable(self, states: States) -> bool:
        modes = _attr(states, self.entity, "supported_color_modes") or []
        return any(m != "onoff" for m in modes)

    def entities(self) -> set[str]:  # scenes are watched too
        return {self.entity, *self.scenes.values()}

    def state(self, states: States) -> dict:
        s = states.get(self.entity) or {}
        on = s.get("state") == "on"
        bri = _attr(states, self.entity, "brightness")
        pct = round(bri / 255 * 100) if (on and isinstance(bri, (int, float))) else (100 if on else 0)
        dimmable = self._dimmable(states)
        members = _attr(states, self.entity, "lights") or []
        out = {
            "t": "state", "id": self.id, "kind": self.kind,
            "name": self.name_for(states), "icon": self.icon,
            "available": _avail(states, self.entity),
            "on": on, "value": pct, "min": 0, "max": 100, "step": 5, "unit": "%",
            "adjustable": dimmable,
            "primary": f"{pct}%" if on and dimmable else ("On" if on else "Off"),
            "secondary": f"{len(members)} lights" if len(members) > 1 else None,
            "warning": None,
        }
        if self.scenes:
            out["modes"] = list(self.scenes)
            # Hue changes the light after a scene; only claim the preset while still on.
            out["mode"] = self.active_scene if on and self.active_scene else ""
            if out["mode"]:
                out["secondary"] = out["mode"]
        return out

    async def set_mode(self, states: States, mode: str, call: Call) -> None:
        if mode not in self.scenes:
            raise ValueError(f"unknown preset {mode!r}")
        await call("scene", "turn_on", {"entity_id": self.scenes[mode]})
        self.active_scene = mode

    async def adjust(self, states: States, value: float, call: Call) -> None:
        pct = int(_snap(float(value), 0, 100, 5))
        if pct <= 0:
            await call("light", "turn_off", {"entity_id": self.entity})
        else:
            await call("light", "turn_on", {"entity_id": self.entity, "brightness_pct": pct})

    async def toggle(self, states: States, call: Call) -> None:
        await call("light", "toggle", {"entity_id": self.entity})


# ── Covers ──────────────────────────────────────────────────────────────────


@dataclass
class CoverControl(Control):
    id: str
    name: str
    icon: str
    members: list[str]
    updown: bool = False          # group of N units moved together: just up / down
    labels: tuple[str, str] = ("Closed", "Open")
    kind: str = "cover"

    def entities(self) -> set[str]:
        return set(self.members)

    def _positions(self, states: States) -> list[int]:
        out = []
        for e in self.members:
            if not _avail(states, e):
                continue
            p = _attr(states, e, "current_position")
            if isinstance(p, (int, float)):
                out.append(int(p))
            else:
                out.append(100 if states[e]["state"] in ("open", "opening") else 0)
        return out

    def state(self, states: States) -> dict:
        pos = self._positions(states)
        value = round(sum(pos) / len(pos)) if pos else 0
        moving = any((states.get(e) or {}).get("state") in ("opening", "closing") for e in self.members)
        is_open = any(p > 0 for p in pos)
        secondary = None
        if self.updown:
            n_open = sum(1 for p in pos if p > 0)
            primary = self.labels[1] if n_open == len(pos) and pos else (self.labels[0] if n_open == 0 else "Mixed")
            if 0 < n_open < len(pos):
                secondary = f"{n_open} of {len(pos)} {self.labels[1].lower()}"
        elif len(self.members) > 1:
            n_open = sum(1 for p in pos if p > 0)
            primary = "All closed" if n_open == 0 else f"{n_open} of {len(pos)} open"
        else:
            primary = "Closed" if value == 0 else ("Open" if value == 100 else f"{value}%")
        if moving:
            primary = "Moving"
        return {
            "t": "state", "id": self.id, "kind": self.kind, "name": self.name, "icon": self.icon,
            "available": bool(pos), "on": is_open,
            "value": (100 if is_open else 0) if self.updown else value,
            "min": 0, "max": 100, "step": 100 if self.updown else 10, "unit": "%",
            "adjustable": True, "primary": primary, "secondary": secondary, "warning": None,
        }

    def _live(self, states: States) -> list[str]:
        return [e for e in self.members if _avail(states, e)]

    async def adjust(self, states: States, value: float, call: Call) -> None:
        pos = int(_snap(float(value), 0, 100, 100 if self.updown else 10))
        if ids := self._live(states):
            await call("cover", "set_cover_position", {"entity_id": ids, "position": pos})

    async def toggle(self, states: States, call: Call) -> None:
        ids = self._live(states)
        if not ids:
            return
        service = "close_cover" if self.state(states)["on"] else "open_cover"
        await call("cover", service, {"entity_id": ids})


# ── Config loading ──────────────────────────────────────────────────────────


def _members(raw: list, default_mode: str) -> list[Member]:
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append(Member(item, default_mode))
        else:
            out.append(Member(item["entity"], item.get("hvac_mode", default_mode)))
    return out


def _group(raw: dict, mode: str) -> ModeGroup:
    return ModeGroup(mode, _members(raw["entities"], mode),
                     float(raw["min"]), float(raw["max"]), float(raw["step"]))


@dataclass
class Layout:
    controls: dict[str, Control]
    sections: list[dict]


def build(config: dict) -> Layout:
    controls: dict[str, Control] = {}
    sections = []

    ids = []
    for c in config.get("climate", []):
        ctl = ClimateControl(c["id"], c["name"], c.get("icon", "thermostat"),
                             _group(c["heat"], "heat"), _group(c["cool"], "cool"),
                             list(c.get("warn_if_open", [])), c.get("sensor"))
        ctl.schedule = dict(c.get("schedule", {}))
        controls[ctl.id] = ctl
        ids.append(ctl.id)
    if ids:
        sections.append({"id": "climate", "name": "Climate", "icon": "thermostat", "controls": ids})

    ids = []
    for item in config.get("lights", []):
        if isinstance(item, str):
            ctl = LightControl(item)
        else:
            ctl = LightControl(item["entity"], item.get("name"), item.get("icon", "lightbulb"),
                               dict(item.get("presets", {})))
        controls[ctl.id] = ctl
        ids.append(ctl.id)
    if ids:
        sections.append({"id": "lights", "name": "Lights", "icon": "lightbulb", "controls": ids})

    ids = []
    for item in config.get("covers", []):
        if "entities" in item:
            ctl = CoverControl(item["id"], item["name"], item.get("icon", "window"), list(item["entities"]),
                               bool(item.get("updown")), tuple(item.get("labels", ("Closed", "Open"))))
        else:
            ctl = CoverControl(item["entity"], item["name"], item.get("icon", "window"), [item["entity"]])
        controls[ctl.id] = ctl
        ids.append(ctl.id)
    if ids:
        sections.append({"id": "windows", "name": "Windows", "icon": "window", "controls": ids})

    return Layout(controls, sections)
