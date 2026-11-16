from pathlib import Path

import pytest
import yaml

from ha_knob.controls import build

CONFIG = yaml.safe_load((Path(__file__).parent.parent / "knob.yaml").read_text())


def st(state, **attrs):
    return {"state": state, "attributes": attrs}


def house_states(**over):
    s = {
        "climate.dnevni": st("off", temperature=20.0, current_temperature=19.0),
        "climate.dnevni_2": st("off", temperature=20.0, current_temperature=20.0),
        "climate.hodnik": st("off", temperature=20.0, current_temperature=18.0),
        "climate.9424b844f92c": st("off", temperature=26, current_temperature=24),
        "climate.9424b82c5fa0": st("off", temperature=26, current_temperature=24),
        "climate.9424b82e9242": st("unavailable"),
        "cover.velux_window_roof_window": st("closed", current_position=0),
        "cover.velux_window_roof_window_2": st("closed", current_position=0),
        "cover.velux_window_roof_window_3": st("closed", current_position=0),
    }
    s.update(over)
    return s


class Rec:
    def __init__(self):
        self.calls = []

    async def __call__(self, domain, service, data):
        self.calls.append((domain, service, data))


@pytest.fixture
def layout():
    return build(CONFIG)


def test_layout_sections(layout):
    assert [s["id"] for s in layout.sections] == ["climate", "lights", "windows"]
    assert "indoors" in layout.controls and "outdoors" in layout.controls


async def test_set_mode_heat_keeps_setpoints_and_turns_cool_off(layout):
    house = layout.controls["indoors"]
    states = house_states(**{"climate.9424b844f92c": st("cool", temperature=24, current_temperature=24)})
    assert house.current_mode(states) == "cool"
    rec = Rec()
    await house.set_mode(states, "heat", rec)
    assert rec.calls[0] == ("climate", "set_hvac_mode", {"entity_id": ["climate.9424b844f92c"], "hvac_mode": "off"})
    on = [c for c in rec.calls[1:] if c[0] == "climate"]
    assert {c[2]["entity_id"] for c in on} == {"climate.dnevni", "climate.dnevni_2", "climate.hodnik"}
    assert all(c[1] == "set_hvac_mode" and c[2]["hvac_mode"] == "heat" for c in on)


async def test_switch_to_cool_skips_unavailable(layout):
    house = layout.controls["indoors"]
    states = house_states(**{"climate.dnevni": st("heat", temperature=21.0, current_temperature=19.0)})
    rec = Rec()
    await house.set_mode(states, "cool", rec)
    assert rec.calls[0][2] == {"entity_id": ["climate.dnevni"], "hvac_mode": "off"}
    cool = [c for c in rec.calls[1:] if c[0] == "climate"]
    assert {c[2]["entity_id"] for c in cool} == {"climate.9424b844f92c", "climate.9424b82c5fa0"}
    assert all(c[1] == "set_temperature" and c[2]["hvac_mode"] == "cool" for c in cool)


async def test_lockstep_adjust_keeps_relative_offsets(layout):
    house = layout.controls["indoors"]
    states = house_states(**{
        "climate.dnevni": st("heat", temperature=26.5, current_temperature=19.0, target_temp_step=0.5),
        "climate.dnevni_2": st("heat", temperature=24.0, current_temperature=20.0, target_temp_step=0.5),
        "climate.hodnik": st("heat", temperature=20.0, current_temperature=18.0, target_temp_step=0.5),
    })
    v = house.state(states)["value"]  # desired room temp (sensor offline -> 21.0)
    rec = Rec()
    await house.adjust(states, v + 1.0, rec)
    assert {c[2]["entity_id"]: c[2]["temperature"] for c in rec.calls} == {
        "climate.dnevni": 27.5, "climate.dnevni_2": 25.0, "climate.hodnik": 21.0}


async def test_cool_is_absolute_whole_degrees(layout):
    house = layout.controls["indoors"]
    house.targets.clear()
    states = house_states(**{
        "climate.9424b844f92c": st("cool", temperature=24, current_temperature=25, target_temp_step=1),
        "climate.9424b82c5fa0": st("cool", temperature=26, current_temperature=25, target_temp_step=1),
    })
    out = house.state(states)
    assert out["mode"] == "cool" and out["value"] == 25 and out["primary"] == "25°" and out["min"] == 18
    rec = Rec()
    await house.adjust(states, 23.4, rec)
    assert {c[2]["entity_id"]: c[2]["temperature"] for c in rec.calls} == {
        "climate.9424b844f92c": 23.0, "climate.9424b82c5fa0": 23.0}
    assert house.state(states)["value"] == 23


def test_window_warning_only_while_heating(layout):
    house = layout.controls["indoors"]
    open_win = st("open", current_position=100)
    s = house_states(**{"cover.velux_window_roof_window": open_win})
    assert house.state(s)["warning"] is None  # off
    s["climate.dnevni"] = st("heat", temperature=21.0, current_temperature=19.0)
    assert house.state(s)["warning"] == "1 window open"


def test_terrace_wonderwall_uses_heat_cool(layout):
    terrace = layout.controls["outdoors"]
    s = {"climate.wonderwall_art_smart_2": st("heat_cool", temperature=26, current_temperature=15)}
    assert terrace.current_mode(s) == "heat"


async def test_light_group_presets(layout):
    light = layout.controls["light.ambiance"]
    s = {"light.ambiance": st("on", brightness=128, supported_color_modes=["color_temp"],
                              friendly_name="Ambiance", lights=["a", "b", "c"])}
    out = light.state(s)
    assert out["primary"] == "50%" and out["secondary"] == "3 lights"
    assert "Relax" in out["modes"] and out["mode"] == ""
    rec = Rec()
    await light.set_mode(s, "Relax", rec)
    assert rec.calls == [("scene", "turn_on", {"entity_id": "scene.ambiance_relax"})]
    assert light.state(s)["mode"] == "Relax"
    s["light.ambiance"]["state"] = "off"
    assert light.state(s)["mode"] == ""
    rec = Rec()
    await light.adjust(s, 0, rec)
    assert rec.calls == [("light", "turn_off", {"entity_id": "light.ambiance"})]


async def test_cover_groups_up_down(layout):
    blinds = layout.controls["blinds"]
    s = {e: st("open", current_position=100) for e in blinds.members}
    out = blinds.state(s)
    assert out["primary"] == "Up" and out["step"] == 100 and out["value"] == 100
    rec = Rec()
    await blinds.toggle(s, rec)
    assert rec.calls[0][1] == "close_cover" and len(rec.calls[0][2]["entity_id"]) == 3
    s[blinds.members[0]] = st("closed", current_position=0)
    out = blinds.state(s)
    assert out["primary"] == "Mixed" and out["secondary"] == "2 of 3 up"
    rec = Rec()
    await blinds.adjust(s, 0, rec)
    assert rec.calls[0] == ("cover", "set_cover_position", {"entity_id": blinds.members, "position": 0})
    windows = layout.controls["windows"]
    assert windows.state({e: st("closed", current_position=0) for e in windows.members})["primary"] == "Closed"


def test_indoors_shows_room_sensor_and_relative_offset(layout):
    house = layout.controls["indoors"]
    house.targets.clear()
    s = house_states(**{"sensor.oxymoron_temperature": st("21.3"),
                        "climate.dnevni": st("heat", temperature=26.5, current_temperature=19.0)})
    out = house.state(s)
    assert out["primary"] == "21.3°" and out["value"] == 0 and (out["min"], out["max"]) == (-5, 5)
    house.targets["heat"] = 1.0
    assert house.state(s)["value"] == 1.0 and house.state(s)["secondary"] == ""
    s["sensor.oxymoron_temperature"] = st("unavailable")
    assert house.state(s)["primary"] == "–"
    s["climate.dnevni"] = st("off", temperature=26.5)
    out = house.state(s)
    assert out["mode"] == "off" and out["adjustable"] is False


async def test_tap_cycles_modes_and_only_applies_after_settling(layout, monkeypatch):
    import ha_knob.controls as c
    monkeypatch.setattr(c, "MODE_SETTLE_S", 0.05)
    house = layout.controls["indoors"]
    s = house_states(**{"climate.dnevni": st("heat", temperature=26.5)})
    rec = Rec()
    await house.toggle(s, rec)          # heat -> cool (pending)
    assert house.state(s)["mode"] == "cool" and rec.calls == []
    await house.toggle(s, rec)          # cool -> off, before cool was ever applied
    assert house.state(s)["mode"] == "off"
    import asyncio
    await asyncio.sleep(0.15)
    assert rec.calls[0] == ("input_boolean", "turn_off", {"entity_id": "input_boolean.indoors_schedule"})
    assert [(x[1], x[2]["hvac_mode"]) for x in rec.calls if x[0] == "climate"] == [("set_hvac_mode", "off")]
    assert not any(x[2].get("hvac_mode") == "cool" for x in rec.calls)


SCHED = {"input_boolean.indoors_schedule": st("off"), "input_number.indoors_wake": st("25.0"),
         "input_number.indoors_sleep": st("20.0"), "input_number.indoors_away": st("18.0"),
         "input_number.offset_dnevni": st("0.0"), "input_number.offset_dnevni_2": st("0.5"),
         "input_number.offset_hodnik": st("-1.0"), "input_select.house_mode": st("Home")}


async def test_heat_turns_schedule_on_and_applies_set_point_plus_offset(layout, monkeypatch):
    house = layout.controls["indoors"]
    monkeypatch.setattr(type(house), "_period", lambda self, states: "wake")
    s = house_states(**SCHED)
    rec = Rec()
    await house.set_mode(s, "heat", rec)
    assert ("input_boolean", "turn_on", {"entity_id": "input_boolean.indoors_schedule"}) in rec.calls
    temps = {c[2]["entity_id"]: c[2]["temperature"] for c in rec.calls if c[1] == "set_temperature"}
    assert temps == {"climate.dnevni": 25.0, "climate.dnevni_2": 25.5, "climate.hodnik": 24.0}
    rec = Rec()
    await house.set_mode(s, "off", rec)
    assert rec.calls[0] == ("input_boolean", "turn_off", {"entity_id": "input_boolean.indoors_schedule"})


async def test_save_default_adds_nudge_to_every_offset(layout):
    house = layout.controls["indoors"]
    s = house_states(**SCHED, **{"climate.dnevni": st("heat", temperature=26.0)})
    house.targets["heat"] = 1.0
    rec = Rec()
    assert await house.action(s, "save_default", rec) == "Saved +1.0°"
    assert {c[2]["entity_id"]: c[2]["value"] for c in rec.calls} == {
        "input_number.offset_dnevni": 1.0, "input_number.offset_dnevni_2": 1.5, "input_number.offset_hodnik": 0.0}
    assert "heat" not in house.targets


async def test_outdoors_heat_is_absolute(layout):
    out = layout.controls["outdoors"]
    out.targets.clear()
    s = {"climate.wonderwall_art_smart_2": st("heat_cool", temperature=26.0, current_temperature=15.0),
         "climate.heater": st("unavailable"), "climate.wonderwall_art_smart": st("unavailable")}
    state = out.state(s)
    assert state["mode"] == "heat" and state["value"] == 26 and state["primary"] == "26°" and state["min"] == 10
    rec = Rec()
    await out.adjust(s, 22, rec)
    assert rec.calls == [("climate", "set_temperature", {"entity_id": "climate.wonderwall_art_smart_2", "temperature": 22.0})]
