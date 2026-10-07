"""Query params: Python bools must reach the service as lowercase true/false.
n8n rejected includeData=True on 2026-10-07 ('Expected true | false')."""
from __future__ import annotations

import os

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

CFG = {"base_url": "http://svc.local/api/v1", "auth_style": "header", "auth_header": "X-Key", "_token": "k"}


def build(params):
    return server._api_build_request(CFG, "/executions", params)[2]


def test_bools_lowercased():
    assert build({"includeData": True, "failed_only": False}) == {"includeData": "true", "failed_only": "false"}


def test_bools_inside_lists():
    assert build({"flags": [True, False, "x"]}) == {"flags": ["true", "false", "x"]}


def test_other_types_untouched():
    assert build({"limit": 5, "q": "abc", "n": None}) == {"limit": 5, "q": "abc", "n": None}


def test_query_param_auth_still_injected():
    cfg = dict(CFG, auth_style="query_param", auth_param="apikey")
    assert server._api_build_request(cfg, "/x", {"a": True})[2] == {"a": "true", "apikey": "k"}


def test_none_params():
    assert build(None) == {}
