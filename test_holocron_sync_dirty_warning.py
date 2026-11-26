"""holocron_sync_push must flag uncommitted work. The sync script has never
committed since its 2026-09-21 redesign, but the tool's docstring still said
"add -A, commit ... push" — so on 2026-11-26 a caller got ok=true with
"Sync complete - behind=0 ahead=0 dirty=1" and took the edit as published."""
from __future__ import annotations

import os

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import pytest  # noqa: E402

import server  # noqa: E402

# _tool wraps registered tools with output redaction; reach the plain function.
sync = getattr(server.holocron_sync_push, "__wrapped__", server.holocron_sync_push)


@pytest.fixture
def fake_ssh(monkeypatch):
    state = {"stdout": ""}
    monkeypatch.setattr(server, "_resolve_holocron_host",
                        lambda h: {"repo": "/r", "sync_script": "/s.sh", "windows": False})
    monkeypatch.setattr(server, "_resolve_host", lambda h: (h, {}))
    monkeypatch.setattr(server, "_ssh_exec",
                        lambda cfg, cmd, **kw: {"exit_code": 0, "stdout": state["stdout"], "stderr": ""})
    return state


def test_dirty_files_produce_warning(fake_ssh):
    fake_ssh["stdout"] = "Sync complete - behind=0 ahead=0 dirty=1\n"
    out = sync("docker-server", confirmed=True)
    assert out["ok"] is True
    assert out["dirty"] == 1
    assert "NOT" in out["warning"] and "never commits" in out["warning"]


def test_clean_tree_has_no_warning(fake_ssh):
    fake_ssh["stdout"] = "Pushed 1 commit(s).\nSync complete - behind=0 ahead=0 dirty=0\n"
    out = sync("docker-server", confirmed=True)
    assert "warning" not in out and "dirty" not in out


def test_unparseable_output_has_no_warning(fake_ssh):
    fake_ssh["stdout"] = "No connectivity to GitHub - skipping.\n"
    assert "warning" not in sync("docker-server", confirmed=True)


def test_parser_uses_final_summary_line():
    assert server._sync_dirty_count("dirty=9 noise\nSync complete - behind=2 ahead=0 dirty=3") == 3


def test_docstring_no_longer_claims_it_commits():
    doc = server.holocron_sync_push.__doc__ or sync.__doc__
    assert "NEVER commits" in doc
    assert "add -A, commit" not in doc
