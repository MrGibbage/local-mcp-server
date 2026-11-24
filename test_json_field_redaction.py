"""Tests for _redact_json / _redact_text / param_denylist / the writer guard —
third-party credentials an upstream app stores and echoes back in JSON fields
(2026-11-24 incident: Sportarr GET /downloadclient returned the SABnzbd
client's password in plaintext). All values here are fakes."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

FAKE = "FAKEsecret7Q2xFAKE"
R = "[REDACTED-BY-PROXY]"


def dump(obj) -> str:
    return json.dumps(obj)


# --- key rule -------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "password", "Password", "passwd", "pass", "pwd", "secret", "token",
    "apiKey", "api_key", "API-KEY", "apiToken", "accessToken", "refreshToken",
    "clientSecret", "client_secret", "privateKey", "cookie", "cookies",
    "authorization", "botToken", "webhookUrl", "nzb_key", "plexToken",
    "pms_token", "usenetPassword", "email_pwd", "jwt_secret", "x-api-key",
    "secretKey", "passkey", "discordWebhookUrl", "sslCertPassword",
])
def test_secret_keys_redacted(key):
    out, n = server._redact_json({key: FAKE, "host": "sab.local"})
    assert out[key] == R
    assert out["host"] == "sab.local"
    assert n == 1


@pytest.mark.parametrize("key", [
    "passwordRequired", "tokenType", "passwordHint",
    "bypass", "compass", "nextPageToken", "pageToken", "username", "host",
    "tokenExpiry", "apiKeyName", "key", "name", "passthrough",
])
def test_non_secret_keys_untouched(key):
    out, n = server._redact_json({key: "some-ordinary-value"})
    assert out[key] == "some-ordinary-value"
    assert n == 0


@pytest.mark.parametrize("value", [True, False, 0, 123, None, "", "   ", "********", "*", R,
                                   "[SCRUBBED:quoted-field#abc]"])
def test_non_string_empty_and_masked_values_kept(value):
    out, n = server._redact_json({"password": value, "apiKey": value})
    assert out == {"password": value, "apiKey": value}
    assert n == 0


def test_boolean_flags_on_secret_shaped_keys_kept():
    # hasPassword / isApiKeySet match the key rule; their bool value protects them.
    data = {"hasPassword": True, "isApiKeySet": False, "passwordLength": 12}
    out, n = server._redact_json(data)
    assert out == data and n == 0


def test_list_of_strings_under_secret_key():
    out, n = server._redact_json({"cookies": [FAKE, "", FAKE + "2"]})
    assert out["cookies"] == [R, "", R]
    assert n == 2


def test_nested_dicts_and_lists():
    data = {"items": [{"settings": {"auth": {"password": FAKE, "user": "u"}}}]}
    out, n = server._redact_json(data)
    assert FAKE not in dump(out)
    assert out["items"][0]["settings"]["auth"]["user"] == "u"
    assert n == 1


# --- *arr provider schema (name/value fields) -------------------------------

def arr_downloadclient(password=FAKE, apikey=FAKE + "K"):
    return [{
        "id": 1, "name": "SABnzbd", "implementation": "Sabnzbd", "enable": True,
        "fields": [
            {"order": 0, "name": "host", "label": "Host", "value": "192.168.0.181", "type": "textbox"},
            {"order": 1, "name": "port", "value": 18080, "type": "textbox"},
            {"order": 2, "name": "apiKey", "label": "API Key", "value": apikey,
             "type": "textbox", "privacy": "apiKey"},
            {"order": 3, "name": "username", "value": "skip", "privacy": "userName"},
            {"order": 4, "name": "password", "label": "Password", "value": password,
             "type": "password", "privacy": "password"},
            {"order": 5, "name": "useSsl", "value": False},
        ],
    }]


def test_arr_fields_redacted():
    out, n = server._redact_json(arr_downloadclient())
    fields = {f["name"]: f["value"] for f in out[0]["fields"]}
    assert fields["password"] == R
    assert fields["apiKey"] == R
    assert fields["host"] == "192.168.0.181"
    assert fields["username"] == "skip"
    assert fields["port"] == 18080
    assert fields["useSsl"] is False
    assert n == 2


def test_arr_masked_fields_left_alone():
    out, n = server._redact_json(arr_downloadclient(password="********", apikey=""))
    fields = {f["name"]: f["value"] for f in out[0]["fields"]}
    assert fields["password"] == "********"
    assert fields["apiKey"] == ""
    assert n == 0


def test_privacy_flag_alone_triggers_redaction():
    # Field name that the key rule wouldn't catch, but the schema marks secret.
    out, n = server._redact_json([{"name": "signIn", "value": FAKE, "privacy": "password"}])
    assert out[0]["value"] == R and n == 1


@pytest.mark.parametrize("name", ["authToken", "accessToken", "notificationSettings.botToken",
                                  "telegram_bot_token", "apiKey"])
def test_name_value_shapes(name):
    # Plex notification authToken, Spotify import list, Tautulli notifier config.
    out, n = server._redact_json({"config": [{"name": name, "value": FAKE}]})
    assert out["config"][0]["value"] == R and n == 1


def test_key_value_shape_n8n_header():
    data = {"parameters": {"headerParameters": {"parameters": [
        {"name": "Authorization", "value": "Bearer " + FAKE},
        {"name": "Accept", "value": "application/json"},
    ]}}}
    out, n = server._redact_json(data)
    assert FAKE not in dump(out)
    assert "application/json" in dump(out)


def test_name_value_non_secret_kept():
    out, n = server._redact_json([{"name": "host", "value": "x"}, {"key": "folder", "value": "/tv"}])
    assert out == [{"name": "host", "value": "x"}, {"key": "folder", "value": "/tv"}]
    assert n == 0


# --- string patterns --------------------------------------------------------

@pytest.mark.parametrize("s", [
    f"https://user:{FAKE}@news.example.com:563",
    f"ntfy://skip:{FAKE}@ntfy.example/topic",
    f"mailtos://me:{FAKE}@smtp.gmail.com",
    f"tgram://{FAKE}/123456789",
    f"discord://123456/{FAKE}",
    f"pover://user@{FAKE}",
    f"https://discord.com/api/webhooks/123456/{FAKE}",
    f"https://hooks.slack.com/services/T000/B000/{FAKE}",
    f"https://api.telegram.org/bot123:{FAKE}/sendMessage",
    f"Authorization: Bearer {FAKE}",
    f"Authorization: Basic {FAKE}",
    f"udp://tracker.example/abcdef0123456789abcdef0123456789{FAKE[:0]}/announce",
    f"magnet:?xt=urn:btih:x&tr=https%3A%2F%2Ftr.example%2Fabcdef0123456789abcdef0123456789%2Fannounce",
    f"<password>{FAKE}</password>",
    f"<nzb_key>{FAKE}</nzb_key>",
])
def test_text_patterns(s):
    out, n = server._redact_text(s)
    assert n >= 1
    assert FAKE not in out
    assert "abcdef0123456789abcdef0123456789" not in out


@pytest.mark.parametrize("s", [
    "https://example.com:8443/path?x=1",
    "http://192.168.0.181:18080/sabnzbd/",
    "json://webhook.example/path",
    "Bearer auth is configured",
    "<host>sab.local</host>",
    "<password></password>",
    "Download completed: Show.S01E01.mkv",
])
def test_text_patterns_leave_ordinary_strings(s):
    out, n = server._redact_text(s)
    assert out == s and n == 0


def test_strings_inside_json_fields_use_text_patterns():
    data = {"notification_urls": [f"tgram://{FAKE}/1", "json://hook.example/x"],
            "message": f"sent to https://u:{FAKE}@h/"}
    out, n = server._redact_json(data)
    assert FAKE not in dump(out)
    assert "json://hook.example/x" in out["notification_urls"]
    assert n == 2


def test_json_inside_string_redacted_recursively():
    inner = json.dumps({"password": FAKE, "host": "h"})
    out, n = server._redact_json({"settings": inner})
    assert FAKE not in out["settings"]
    assert json.loads(out["settings"]) == {"password": R, "host": "h"}
    assert n == 1


def test_json_string_without_secrets_kept_verbatim():
    inner = '{ "host" : "h" }'
    out, n = server._redact_json({"settings": inner})
    assert out["settings"] == inner and n == 0


# --- _api_parse_response integration -------------------------------------

CFG = {"base_url": "http://svc.local/api", "auth_style": "header",
       "auth_header": "X-Api-Key", "_token": "OWNTOKENFAKE1234567890"}


def resp(text, ok=True, status=200):
    return MagicMock(ok=ok, status_code=status, text=text)


def test_parse_response_redacts_and_counts():
    r = server._api_parse_response(CFG, resp(dump(arr_downloadclient())))
    assert FAKE not in dump(r)
    assert r["json_fields_redacted"] == 2
    assert "url_credentials_redacted" not in r


def test_parse_response_all_three_passes():
    body = dump({"own": CFG["_token"],
                 "downloadUrl": f"https://idx.example/api?t=get&apikey={FAKE}",
                 "password": FAKE})
    r = server._api_parse_response(CFG, resp(body))
    s = dump(r)
    assert FAKE not in s and CFG["_token"] not in s
    assert r["url_credentials_redacted"] == 1
    assert r["json_fields_redacted"] == 1


def test_parse_response_clean_body_has_no_counts():
    r = server._api_parse_response(CFG, resp(dump([{"id": 1, "title": "x"}])))
    assert r == {"ok": True, "status": 200, "data": [{"id": 1, "title": "x"}]}


def test_parse_response_non_json_text():
    text = f"line 1\npassword: {FAKE}\nfetch https://u:{FAKE}@h/x\n<api_key>{FAKE}</api_key>\n"
    r = server._api_parse_response(CFG, resp(text))
    assert FAKE not in r["data"]
    assert r["text_secrets_redacted"] == 3
    assert "line 1" in r["data"]


# --- writer guard -----------------------------------------------------------

@pytest.fixture
def http(monkeypatch):
    cfg = dict(CFG, post_allowlist=["/downloadclient"])
    monkeypatch.setattr(server, "_api_svc_cfg", lambda svc: dict(cfg))
    calls = {}
    for verb in ("get", "post", "put", "patch", "delete"):
        m = MagicMock(return_value=resp("{}"))
        monkeypatch.setattr(server._requests, verb, m)
        calls[verb] = m
    return calls


@pytest.mark.parametrize("marker", [R, "[REDACTED-possible-secret]", "[SCRUBBED:quoted-field#fd98b]"])
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_mutate_refuses_redacted_body(http, marker, method):
    body = arr_downloadclient(password=marker)[0]
    r = server.homelab_api_mutate("svc", method, "/downloadclient/1", body, confirmed=True)
    assert not r["ok"] and "redaction marker" in r["error"]
    for m in http.values():
        m.assert_not_called()


def test_post_refuses_redacted_body(http):
    r = server.homelab_api_post("svc", "/downloadclient", {"password": R})
    assert not r["ok"] and "redaction marker" in r["error"]
    http["post"].assert_not_called()


def test_mutate_clean_body_still_sent(http):
    r = server.homelab_api_mutate("svc", "PUT", "/downloadclient/1", {"enable": False}, confirmed=True)
    assert r["ok"]
    http["put"].assert_called_once()


# --- param_denylist ---------------------------------------------------------

SAB = {
    "base_url": "http://sab.local:8080", "auth_style": "query_param",
    "auth_param": "apikey", "_token": "SABOWNFAKE1234567890",
    "param_denylist": [{
        "param": "mode",
        "values": ["get_config", "set_config", "config"],
        "unless": {"section": ["misc"], "keyword": ["complete_dir", "download_dir"]},
    }],
}


@pytest.fixture
def sab(monkeypatch):
    monkeypatch.setattr(server, "_api_svc_cfg", lambda svc: dict(SAB))
    m = MagicMock(return_value=resp('{"queue": {}}'))
    monkeypatch.setattr(server._requests, "get", m)
    return m


@pytest.mark.parametrize("path,params", [
    ("/api", {"mode": "get_config"}),
    ("/api", {"mode": "GET_CONFIG", "output": "json"}),
    ("/api", {"Mode": "get_config"}),
    ("/api", {"mode": "get_config", "section": "servers"}),
    ("/api", {"mode": "get_config", "section": "misc"}),                       # whole misc: has keys
    ("/api", {"mode": "get_config", "section": "misc", "keyword": "nzb_key"}),
    ("/api", {"mode": "get_config", "section": "misc", "keyword": ["complete_dir", "api_key"]}),
    ("/api", {"mode": "get_config", "section": ["misc", "servers"], "keyword": "complete_dir"}),
    ("/api?mode=get_config", None),
    ("/api?output=json&mode=get_config", {}),
    ("/api?mode=%67et_config", None),
    ("/api?mode=%2567et_config", None),
    ("/api?mode=queue", {"mode": "get_config"}),                               # duplicate param
    ("/api", {"mode": "config", "name": "set_apikey"}),
])
def test_param_denied(sab, path, params):
    r = server.homelab_api_get("sabnzbd", path, params)
    assert not r["ok"] and "param_denylist" in r["error"]
    sab.assert_not_called()


@pytest.mark.parametrize("path,params", [
    ("/api", {"mode": "queue"}),
    ("/api", {"mode": "history", "limit": 5}),
    ("/api", {"mode": "version"}),
    ("/api", {"mode": "get_config", "section": "misc", "keyword": "complete_dir"}),
    ("/api", {"mode": "get_config", "section": "MISC", "keyword": "download_dir"}),
    ("/api?mode=get_config&section=misc&keyword=complete_dir", None),
])
def test_param_allowed(sab, path, params):
    r = server.homelab_api_get("sabnzbd", path, params)
    assert r["ok"], r
    sab.assert_called_once()


def test_no_param_denylist_is_noop():
    server._api_check_param_denylist({}, "/api?mode=get_config", {"mode": "get_config"})
