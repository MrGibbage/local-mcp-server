"""Tests for tool-wide output redaction (_with_output_redaction / _redact_output)
and the writer-tool marker guard — 2026-11-24 follow-up to the Sportarr
/downloadclient password leak: ssh_exec, docker logs, loki, http_get, ha_*,
read_file etc. returned third-party credentials verbatim. Fake values only."""
from __future__ import annotations

import inspect
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

FAKE = "FAKEsecret7Q2xFAKE"
R = "[REDACTED-BY-PROXY]"


# --- _redact_output_text --------------------------------------------------

@pytest.mark.parametrize("line", [
    f"password: {FAKE}",
    f"  db_password: '{FAKE}'",
    f"SABNZBD_API_KEY={FAKE}",
    f"export GITHUB_TOKEN={FAKE}",
    f"api_key = {FAKE}",
    f"nzb_key = {FAKE}",
    f'"clientSecret": "{FAKE}",',
    f"+  smtp_password: {FAKE}",
    f"webhook_url: https://example.invalid/{FAKE}",
    f"2026-11-24 fetch https://idx.example/api?t=get&apikey={FAKE}",
    f"connecting to nntps://user:{FAKE}@news.example:563",
    f"curl -H 'Authorization: Bearer {FAKE}' http://x",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA" + FAKE + "\n-----END OPENSSH PRIVATE KEY-----",
])
def test_output_text_redacted(line):
    out, n = server._redact_output_text("before\n" + line + "\nafter")
    assert FAKE not in out
    assert n >= 1
    assert out.startswith("before\n") and out.endswith("\nafter")


@pytest.mark.parametrize("line", [
    "password: ${SAB_PASSWORD}",
    "api_key: {{ vault_api_key }}",
    "token: lookup('env', 'X')",
    "password_file: /run/secrets/db",
    "api_key_file: ./keys/x",
    "password_required: true",
    "token_ttl: 3600",
    "secret: null",
    "password: ''",
    "password:",
    "has_password: yes-it-does",
    "use_token: something",
    "next_page_token: abc123",
    "password: ********",
    f"password: {R}",
    "private_key: |",
    "starting sabnzbd on port 8080",
])
def test_output_text_safe_values_kept(line):
    out, n = server._redact_output_text(line)
    assert out == line and n == 0


# --- wrapper behaviour ----------------------------------------------------

def make_tool(result, name="fake_tool"):
    def fake_tool(path: str, host: str | None = None) -> dict:
        """doc"""
        return result
    fake_tool.__name__ = name
    return server._with_output_redaction(fake_tool)


def test_wrapper_redacts_and_counts():
    t = make_tool({"stdout": f"PLEX_TOKEN={FAKE}\nok", "stderr": "", "exit_code": 0})
    r = t("x")
    assert FAKE not in str(r)
    assert r["stdout"].endswith("\nok")
    assert r["exit_code"] == 0
    assert r["output_secrets_redacted"] == 1


def test_wrapper_redacts_structured_results():
    # e.g. ha_get_state: camera entities carry an access_token attribute.
    t = make_tool({"entity_id": "camera.door", "attributes": {
        "access_token": FAKE, "entity_picture": f"/api/camera_proxy/camera.door?token={FAKE}",
        "friendly_name": "Door"}})
    r = t("x")
    assert FAKE not in str(r)
    assert r["attributes"]["friendly_name"] == "Door"
    assert r["output_secrets_redacted"] == 2


def test_wrapper_clean_output_unchanged():
    res = {"stdout": "hello\nworld", "exit_code": 0, "ok": True}
    assert make_tool(dict(res))("x") == res


def test_wrapper_list_and_str_results():
    assert FAKE not in str(make_tool([{"password": FAKE}])("x"))
    assert FAKE not in make_tool(f"token: {FAKE}")("x")


def test_wrapper_non_text_results_pass_through():
    obj = object()
    assert make_tool(obj)("x") is obj


def test_exempt_tools_not_double_processed():
    res = {"data": {"password": FAKE}}
    assert make_tool(res, name="homelab_api_get")("x") == res


def test_wrapper_preserves_signature_and_doc():
    t = make_tool({})
    assert list(inspect.signature(t).parameters) == ["path", "host"]
    assert t.__doc__ == "doc"


def test_registered_tools_are_wrapped():
    for name in ("ssh_exec", "read_file", "http_get", "loki_query", "ha_get_state",
                 "ha_render_template", "opnsense_read_file", "docker_logs"):
        fn = getattr(server, name)
        assert getattr(fn, "__wrapped__", None) is not None, name


# --- writer guard ---------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs", [
    ("write_file", {"path": "/tmp/x", "content": f"password: {R}\n"}),
    ("patch_file", {"path": "/tmp/x", "old_string": "a", "new_string": f"key={R}"}),
    ("regex_patch_file", {"path": "/tmp/x", "pattern": "a", "replacement": "[SCRUBBED:quoted-field#1]"}),
    ("write_file", {"path": "/tmp/x", "content": "[REDACTED-possible-secret]"}),
])
def test_writer_tools_refuse_markers(monkeypatch, tool, kwargs):
    run = MagicMock()
    monkeypatch.setattr(server, "_run", run)
    monkeypatch.setattr(server, "_ssh_connect", run)
    r = getattr(server, tool)(**kwargs)
    assert r["ok"] is False and "redaction marker" in r["error"]
    run.assert_not_called()


def test_writer_guard_positional_args(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(server, "_ssh_connect", run)
    r = server.write_file("/tmp/x", f"x {R}")
    assert r["ok"] is False
    run.assert_not_called()


def test_writer_guard_ignores_old_string():
    # A marker in old_string can only fail to match; it must not be refused here.
    assert "new_string" in server._WRITER_ARGS["patch_file"]
    assert "old_string" not in server._WRITER_ARGS["patch_file"]
