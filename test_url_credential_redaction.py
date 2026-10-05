"""Tests for _redact_url_credentials / _api_parse_response — third-party
credentials embedded in URLs inside proxied response bodies (2026-10-04 incident:
Sonarr /history downloadUrl leaked indexer API keys)."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

FAKE = "FAKEkey1234567890abcdefFAKE"


@pytest.mark.parametrize("url", [
    f"https://api.nzbgeek.info/api?t=get&id=abc123&apikey={FAKE}",
    f"https://drunkenslug.com/getnzb/abc.nzb&i=190780&r={FAKE}",
    f"https://indexer.example/api?apikey={FAKE}&t=get",
    f"http://plex.local/library?X-Plex-Token={FAKE}",
    f"https://x.example/rss?passkey={FAKE}",
    f"https://x.example/a?api_key={FAKE}",
    f"https://x.example/a?token={FAKE}",
])
def test_plain_urls_redacted(url):
    out, n = server._redact_url_credentials(url)
    assert FAKE not in out
    assert n == 1
    assert "[REDACTED-BY-PROXY]" in out


def test_dotnet_escaped_ampersand_redacted():
    # System.Text.Json writes & as & in raw JSON text.
    raw = '{"downloadUrl":"https://api.nzbgeek.info/api?t=get\\u0026id=abc\\u0026apikey=' + FAKE + '"}'
    out, n = server._redact_url_credentials(raw)
    assert FAKE not in out
    assert n == 1
    # Still valid JSON afterwards, and the redaction survives parsing.
    assert json.loads(out)["downloadUrl"].endswith("apikey=[REDACTED-BY-PROXY]")


def test_non_credential_params_untouched():
    url = "https://drunkenslug.com/getnzb/abc.nzb&i=190780&t=get&id=abc123"
    out, n = server._redact_url_credentials(url)
    assert out == url
    assert n == 0


def test_parse_response_redacts_and_reports_count():
    body = {"records": [{"data": {"downloadUrl": f"https://api.nzbgeek.info/api?t=get&id=1&apikey={FAKE}"}}]}
    resp = MagicMock(ok=True, status_code=200, text=json.dumps(body))
    result = server._api_parse_response({"_token": "proxy-own-token"}, resp)
    assert FAKE not in json.dumps(result)
    assert result["url_credentials_redacted"] == 1


def test_parse_response_still_scrubs_own_token():
    resp = MagicMock(ok=True, status_code=200, text='{"apiKey": "proxy-own-token"}')
    result = server._api_parse_response({"_token": "proxy-own-token"}, resp)
    assert result["data"]["apiKey"] == "[REDACTED-BY-PROXY]"
    assert "url_credentials_redacted" not in result
