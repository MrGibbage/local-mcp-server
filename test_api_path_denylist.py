"""Tests for the per-service path_denylist — endpoints whose responses carry
credentials the proxy can't redact (Bazarr /system/settings, 2026-10-19)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

CFG = {
    "base_url": "http://svc.local:6767/api",
    "auth_style": "header",
    "auth_header": "X-API-KEY",
    "_token": "k",
    "path_denylist": ["/system/settings", "/plex"],
    "post_allowlist": ["/system"],
}


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(server, "_api_svc_cfg", lambda svc: dict(CFG))
    calls = {}
    for verb in ("get", "post", "put", "patch", "delete"):
        m = MagicMock(return_value=MagicMock(ok=True, status_code=200, text="{}"))
        monkeypatch.setattr(server._requests, verb, m)
        calls[verb] = m
    return calls


@pytest.mark.parametrize("path", [
    "/system/settings",
    "/system/settings/",
    "/system/settings?x=1",
    "/system/settings#frag",
    "/SYSTEM/Settings",
    "//system//settings",
    "/system/./settings",
    "/system/tasks/../settings",
    "/system/%73ettings",
    "/system/%2573ettings",
    "/system%2Fsettings",
    "\\system\\settings",
    "/api/system/settings",      # duplicate base prefix, stripped by normalize
    "/system/settings/general",  # below a denied entry
    "/plex/apikey",
    "/plex/oauth/pin",
])
def test_denied_paths_make_no_request(http, path):
    r = server.homelab_api_get("bazarr", path)
    assert not r["ok"]
    assert "path_denylist" in r["error"]
    http["get"].assert_not_called()


@pytest.mark.parametrize("path", [
    "/system/status",
    "/system/settingsx",   # prefix match is per segment, not per character
    "/episodes/wanted",
    "/plexus",
])
def test_allowed_paths_go_through(http, path):
    r = server.homelab_api_get("bazarr", path)
    assert r["ok"]
    http["get"].assert_called_once()


def test_post_to_denied_path_blocked_even_when_allowlisted(http):
    r = server.homelab_api_post("bazarr", "/system/settings", {"a": 1})
    assert not r["ok"] and "path_denylist" in r["error"]
    http["post"].assert_not_called()


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_mutate_to_denied_path_blocked(http, method):
    r = server.homelab_api_mutate("bazarr", method, "/system/settings", confirmed=True)
    assert not r["ok"] and "path_denylist" in r["error"]
    http[method.lower()].assert_not_called()


def test_no_denylist_means_no_restriction(http, monkeypatch):
    cfg = {k: v for k, v in CFG.items() if k != "path_denylist"}
    monkeypatch.setattr(server, "_api_svc_cfg", lambda svc: dict(cfg))
    assert server.homelab_api_get("other", "/system/settings")["ok"]
