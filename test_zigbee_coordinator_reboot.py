"""zigbee_coordinator_reboot: power-cycle the tubesZB via its Z-Wave plug, wait
for its TCP port, restart Zigbee2MQTT (it exits after ~4 failed reconnects,
seen 2026-10-24), wait for the bridge. Power must always be restored."""
from __future__ import annotations

import os

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import pytest  # noqa: E402
import requests  # noqa: E402

import server  # noqa: E402

CFG = {
    "power_switch": "switch.coordinator_plug",
    "host": "192.168.1.50",
    "port": 6638,
    "z2m_addon": "45df7312_zigbee2mqtt",
    "bridge_entity": "binary_sensor.zigbee2mqtt_bridge_connection_state",
}


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.fixture
def env(monkeypatch):
    clock = {"t": 1000.0}
    calls: list[tuple] = []
    state = {"port_after": 0, "bridge": ["off", "on"], "fail_off": False}

    monkeypatch.setattr(server, "_zigbee_last_reboot", None)
    monkeypatch.setattr(server, "_load_config", lambda: {"zigbee_coordinator": dict(CFG)})
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(server.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def port_open(host, port, timeout=2.0):
        calls.append(("port", host, port))
        state["port_after"] -= 1
        return state["port_after"] < 0

    def ha(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path == "/services/switch/turn_off" and state["fail_off"]:
            raise requests.ConnectionError("boom")
        if method == "GET":
            return Resp({"state": state["bridge"].pop(0) if len(state["bridge"]) > 1 else state["bridge"][0]})
        return Resp([])

    monkeypatch.setattr(server, "_tcp_port_open", port_open)
    monkeypatch.setattr(server, "_ha_request", ha)
    return calls, state, clock


def posts(calls):
    return [(c[1], c[2]) for c in calls if c[0] == "POST"]


def test_happy_path_order(env):
    calls, state, _ = env
    state["port_after"] = 2
    out = server.zigbee_coordinator_reboot()
    assert out["ok"] is True
    assert posts(calls) == [
        ("/services/switch/turn_off", {"entity_id": "switch.coordinator_plug"}),
        ("/services/switch/turn_on", {"entity_id": "switch.coordinator_plug"}),
        ("/services/hassio/addon_restart", {"addon": "45df7312_zigbee2mqtt"}),
    ]


def test_power_restored_when_turn_off_fails(env):
    calls, state, _ = env
    state["fail_off"] = True
    out = server.zigbee_coordinator_reboot()
    assert out["ok"] is False
    assert ("/services/switch/turn_on", {"entity_id": "switch.coordinator_plug"}) in posts(calls)
    assert not any(p == "/services/hassio/addon_restart" for p, _ in posts(calls))


def test_no_addon_restart_if_port_never_opens(env):
    calls, state, _ = env
    state["port_after"] = 10_000
    out = server.zigbee_coordinator_reboot()
    assert out["ok"] is False
    assert "did not come back" in out["error"]
    assert not any(p == "/services/hassio/addon_restart" for p, _ in posts(calls))


def test_bridge_timeout_reported(env):
    _, state, _ = env
    state["bridge"] = ["off"]
    out = server.zigbee_coordinator_reboot()
    assert out["ok"] is False
    assert "did not reconnect" in out["error"]


def test_cooldown(env):
    calls, _, clock = env
    assert server.zigbee_coordinator_reboot()["ok"] is True
    n = len(calls)
    second = server.zigbee_coordinator_reboot()
    assert second["ok"] is False and "recently" in second["error"]
    assert len(calls) == n  # nothing touched
    clock["t"] += 601
    assert server.zigbee_coordinator_reboot()["ok"] is True


def test_refuses_incomplete_or_non_switch_config(env, monkeypatch):
    monkeypatch.setattr(server, "_load_config", lambda: {"zigbee_coordinator": {"host": "x"}})
    assert "missing" in server.zigbee_coordinator_reboot()["error"]
    bad = dict(CFG, power_switch="lock.front_door")
    monkeypatch.setattr(server, "_load_config", lambda: {"zigbee_coordinator": bad})
    assert "switch.*" in server.zigbee_coordinator_reboot()["error"]
