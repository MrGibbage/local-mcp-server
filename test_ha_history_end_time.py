"""ha_get_history / ha_get_logbook must send end_time. HA defaults end_time to
start + 1 day when it's omitted, so any hours > 24 silently returned only the
first day of the window (found 2026-11-11: a 72h history query came back with a
single stale entry from 3 days earlier)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from urllib.parse import unquote

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import pytest  # noqa: E402

import server  # noqa: E402


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.fixture
def calls(monkeypatch):
    recorded: list[dict] = []

    def fake(method, path, **kwargs):
        recorded.append({"method": method, "path": path, **kwargs})
        return Resp(recorded[-1].get("_payload", []))

    monkeypatch.setattr(server, "_ha_request", fake)
    return recorded


def _span_hours(start_iso: str, end_iso: str) -> float:
    return (datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)) / timedelta(hours=1)


@pytest.mark.parametrize("hours", [1, 24, 72, 168])
def test_history_sends_end_time_spanning_full_window(calls, hours):
    out = server.ha_get_history("remote.x", hours=hours)
    assert out["ok"]
    params = calls[0]["params"]
    assert "end_time" in params
    start_iso = unquote(calls[0]["path"].rsplit("/", 1)[1])
    assert _span_hours(start_iso, params["end_time"]) == pytest.approx(hours, abs=0.01)


def test_history_empty_result_explains_recorder_exclusion(calls):
    out = server.ha_get_history("media_player.excluded", hours=72)
    assert out["count"] == 0
    assert "recorder" in out["note"]


def test_history_nonempty_has_no_note(monkeypatch):
    payload = [[{"state": "on", "last_changed": "2026-11-11T12:00:00+00:00"}]]
    monkeypatch.setattr(server, "_ha_request", lambda *a, **k: Resp(payload))
    out = server.ha_get_history("switch.tv", hours=72)
    assert out["count"] == 1
    assert "note" not in out


@pytest.mark.parametrize("hours", [4, 72])
def test_logbook_sends_end_time(calls, hours):
    out = server.ha_get_logbook(hours=hours)
    assert out["ok"]
    assert "end_time" in calls[0]["params"]
