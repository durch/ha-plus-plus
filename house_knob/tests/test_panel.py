"""The e-ink panel's HTTP face of the bridge: same controls as the knob, no WebSocket."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from ha_knob import server
from ha_knob.controls import build

HERE = Path(__file__).parent
CONFIG = yaml.safe_load((HERE.parent / "knob.yaml").read_text())


def st(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


SNAPSHOT = {"states": [
    st("cover.velux_window_roof_window", "closed", current_position=0),
    st("cover.velux_window_roof_window_2", "closed", current_position=0),
    st("cover.velux_window_roof_window_3", "closed", current_position=0),
    st("light.ambiance", "off", supported_color_modes=["xy"]),
    st("input_select.house_mode", "Away"),
]}


@pytest.fixture
async def client():
    bridge = server.Bridge(build(CONFIG), "http://offline", "")
    bridge.ha.load_fake(SNAPSHOT)
    async with TestClient(TestServer(bridge.app())) as c:
        yield c


def control(snap, cid):
    return next(c for c in snap["controls"] if c["id"] == cid)


async def test_state_lists_every_control(client):
    snap = await (await client.get("/panel/state")).json()
    assert {c["id"] for c in snap["controls"]} >= {"indoors", "outdoors", "windows", "house_mode"}
    assert control(snap, "windows")["labels"] == ["Closed", "Open"]
    assert snap["dry_run"] is True


async def test_command_is_a_knob_message(client):
    r = await client.post("/panel/cmd", data=json.dumps({"t": "toggle", "id": "windows"}))
    snap = await r.json()
    assert snap["ok"] is True
    assert control(snap, "windows")["on"] is True


async def test_bad_commands_are_refused(client):
    assert (await client.post("/panel/cmd", data="nope")).status == 400
    r = await client.post("/panel/cmd", data=json.dumps({"t": "toggle", "id": "nothing"}))
    assert (await r.json())["ok"] is False


async def test_state_waits_for_a_change(client, monkeypatch):
    monkeypatch.setattr(server, "PANEL_WAIT_S", 5)
    rev = (await (await client.get("/panel/state")).json())["rev"]
    waiting = asyncio.create_task(client.get("/panel/state", params={"rev": rev}))
    await asyncio.sleep(0.2)
    assert not waiting.done()          # nothing changed: the request is held
    await client.post("/panel/cmd", data=json.dumps({"t": "toggle", "id": "windows"}))
    snap = await (await asyncio.wait_for(waiting, 3)).json()
    assert snap["rev"] != rev and control(snap, "windows")["on"] is True


async def test_page_is_served(client):
    r = await client.get("/panel/")
    assert r.status == 200 and "XMLHttpRequest" in await r.text()
