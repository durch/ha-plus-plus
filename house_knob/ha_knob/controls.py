"""Control model: turns HA entity states into hk/1 `state` messages and
knob input (adjust / toggle / mode) into HA service calls.

Pure logic. `states` is a dict entity_id -> HA state object (dict with
"state" and "attributes"); `call(domain, service, data)` is an async callable.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable  # noqa: F401

Call = Callable[[str, str, dict], Awaitable[Any]]
States = dict[str, dict]

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
    # Desired room temperature per mode, held by the bridge (devices' own setpoints
    # are just actuators that get bumped in lockstep). Persisted via `store`.
    targets: dict[str, float] = field(default_factory=dict)
    store: Callable[[], None] | None = None
    # Exact (unrounded) per-device setpoints, so 0.5° knob steps accumulate on
    # devices that only take whole degrees.
    exact: dict[str, float] = field(default_factory=dict)

    def entities(self) -> set[str]:
        ids = {m.entity for g in (self.heat, self.cool) for m in g.members}
        return ids | set(self.warn_if_open) | ({self.sensor} if self.sensor else set())

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

    def _target(self, states: States, mode: str) -> float:
        group = self._group(mode)
        if mode in self.targets:
            return self.targets[mode]
        room = self._room_temp(states)
        if room is not None:
            base = room
        elif self.sensor:
            base = 21.0 if mode == "heat" else 24.0  # sensor offline: neutral starting point
        else:
            base = self._value(states, group)
        return _snap(base, group.min, group.max, group.step)

    def _current_temp(self, states: States, group: ModeGroup) -> float | None:
        if self.sensor:  # authoritative sensor only; device readings are differently calibrated
            return self._room_temp(states)
        temps = [_attr(states, m.entity, "current_temperature") for m in group.members
                 if _avail(states, m.entity)]
        temps = [float(t) for t in temps if isinstance(t, (int, float))]
        return sum(temps) / len(temps) if temps else None

    def state(self, states: States) -> dict:
        mode = self.current_mode(states)
        shown = mode if mode != "off" else self._default_mode()
        group = self._group(shown)
        value = self._target(states, shown)
        sp = self._setpoints(states, group)
        now = self._current_temp(states, group)
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
        spread = " (sensor offline)" if self.sensor and self._room_temp(states) is None else ""
        return {
            "t": "state", "id": self.id, "kind": self.kind, "name": self.name, "icon": self.icon,
            "available": len(unavailable) < len(group.members),
            "on": mode != "off", "mode": mode, "modes": ["off", "heat", "cool"],
            "value": value,
            # Lockstep moves are relative; the range just bounds the representative value.
            "min": group.min, "max": group.max, "step": group.step, "unit": "°C",
            "adjustable": True,
            "primary": _fmt_temp(value) if mode != "off" else "Off",
            "secondary": (f"Now {_fmt_temp(now)}" + spread) if mode != "off"
                         else f"Now {_fmt_temp(now)}{spread} · {shown} {_fmt_temp(value)}",
            "warning": warning,
        }

    async def adjust(self, states: States, value: float, call: Call) -> None:
        """Shift every device of the shown mode by (value - current group value)."""
        mode = self.current_mode(states)
        shown = mode if mode != "off" else self._default_mode()
        group = self._group(shown)
        new = _snap(float(value), group.min, group.max, group.step)
        delta = new - self._target(states, shown)
        self.targets[shown] = new
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
            for group in (self.heat, self.cool):
                await self._off(states, group, call)
            return
        other = self._group("cool" if mode == "heat" else "heat")
        await self._off(states, other, call)
        for m in self._group(mode).members:
            if _avail(states, m.entity) and states[m.entity].get("state") != m.hvac_mode:
                await call("climate", "set_hvac_mode", {"entity_id": m.entity, "hvac_mode": m.hvac_mode})
        self.last_mode = mode

    async def _off(self, states: States, group: ModeGroup, call: Call) -> None:
        ids = [m.entity for m in group.members
               if _avail(states, m.entity) and states[m.entity].get("state") != "off"]
        if ids:
            await call("climate", "set_hvac_mode", {"entity_id": ids, "hvac_mode": "off"})

    async def toggle(self, states: States, call: Call) -> None:
        mode = self.current_mode(states)
        if mode == "off":
            await self._apply(states, self._default_mode(), call)
        else:
            self.last_mode = mode
            await self._apply(states, "off", call)

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
            primary = "Moving…"
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
