"""Tests for homelab_api_mutate request construction — DELETE with and without
a JSON body (*arr bulk endpoints need one; 2026-10-06 Sonarr /queue/bulk 400)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(server, "_api_svc_cfg", lambda svc: {"name": svc})
    monkeypatch.setattr(
        server, "_api_build_request",
        lambda cfg, path, params: ("http://svc.local/api/v3" + path, {"X-Api-Key": "k"}, {}, None),
    )
    monkeypatch.setattr(server, "_api_parse_response", lambda cfg, resp: {"ok": True})
    calls = {}
    for verb in ("delete", "put", "patch"):
        m = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(server._requests, verb, m)
        calls[verb] = m
    return calls


def test_delete_with_body_sends_json(stub):
    body = {"ids": [1, 2, 3]}
    r = server.homelab_api_mutate("sonarr", "DELETE", "/queue/bulk", body=body, confirmed=True)
    assert r["ok"]
    kw = stub["delete"].call_args.kwargs
    assert kw["json"] == body
    assert kw["headers"]["Content-Type"] == "application/json"


def test_delete_with_empty_ids_still_sends_body(stub):
    server.homelab_api_mutate("sonarr", "DELETE", "/blocklist/bulk", body={"ids": []}, confirmed=True)
    assert stub["delete"].call_args.kwargs["json"] == {"ids": []}


def test_delete_without_body_sends_no_body(stub):
    server.homelab_api_mutate("sonarr", "DELETE", "/queue/5", confirmed=True)
    kw = stub["delete"].call_args.kwargs
    assert "json" not in kw and "data" not in kw
    assert "Content-Type" not in kw["headers"]


def test_put_without_body_still_sends_empty_object(stub):
    server.homelab_api_mutate("sonarr", "PUT", "/x", confirmed=True)
    assert stub["put"].call_args.kwargs["json"] == {}


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH"])
def test_unconfirmed_makes_no_request(stub, method):
    r = server.homelab_api_mutate("sonarr", method, "/queue/bulk", body={"ids": [1]})
    assert not r["ok"]
    for m in stub.values():
        m.assert_not_called()
