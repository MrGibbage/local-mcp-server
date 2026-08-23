"""Tests for the secret-path guard in server.py — _check_secret_path and its
use by read_file (see docs/security.md and CLAUDE.md for the guard's role)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# server.py calls _load_config() at import time; point it at the example config
# so tests don't require a real config.yaml.
os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402


# ---------------------------------------------------------------------------
# _check_secret_path — Claude Code settings files, any host, any path style
# ---------------------------------------------------------------------------

class TestSecretPathGuardClaudeSettings:
    @pytest.mark.parametrize("path", [
        r"C:\Users\skip\.claude\settings.json",
        "C:/Users/skip/.claude/settings.json",
        "/home/skip/.claude/settings.json",
        "/home/skip/.claude/settings.local.json",
        "/mnt/c/Users/skip/.claude/settings.json",
        r"C:\Users\skip\.claude.json",
        "/home/skip/.claude.json",
        "/mnt/c/Users/skip/.claude.json",
    ])
    def test_blocks_settings_and_dotclaude_json(self, path):
        with pytest.raises(ValueError, match="secret-path guard"):
            server._check_secret_path(path)

    @pytest.mark.parametrize("path", [
        "/home/skip/.claude/keybindings.json",
        "/home/skip/.claude/hooks/pre-commit",
        r"C:\Users\skip\.claude\commands\foo.md",
        "/srv/local-mcp-server/config.yaml",
        "/home/skip/project/settings.json",  # settings.json but not under .claude/
    ])
    def test_allows_unrelated_files(self, path):
        server._check_secret_path(path)  # must not raise


# ---------------------------------------------------------------------------
# read_file — end-to-end block via SFTP, using a fake settings.json with a
# live-looking "env" block, plus an unrelated file passing through untouched.
# ---------------------------------------------------------------------------

class TestReadFileSecretPathIntegration:
    def test_read_file_blocks_settings_json_with_env_block(self):
        """A settings.json containing an 'env' block of live tokens must never
        reach SFTP — the guard should short-circuit before any connection."""
        with patch.object(server, "_ssh_connect") as mock_connect:
            result = server.read_file(
                r"C:\Users\skip\.claude\settings.json", host="laptop"
            )

        assert result["content"] is None
        assert "secret-path guard" in result["error"]
        mock_connect.assert_not_called()

    def test_read_file_allows_unrelated_file(self):
        fake_sftp = MagicMock()
        fake_file = MagicMock()
        fake_file.__enter__.return_value.read.return_value = b'{"hooks": {}}'
        fake_sftp.file.return_value = fake_file
        fake_client = MagicMock()
        fake_client.open_sftp.return_value = fake_sftp

        with patch.object(server, "_ssh_connect", return_value=fake_client):
            result = server.read_file(
                r"C:\Users\skip\.claude\keybindings.json", host="laptop"
            )

        assert "error" not in result
        assert result["content"] == '{"hooks": {}}'


# ---------------------------------------------------------------------------
# _run — a command-string match on _SECRET_PATH_PATTERNS (e.g. a --cookies
# flag pointing at /run/secrets/...) must still return a "host" key. Every
# caller of _run (docker_exec, docker_restart, tail_file, ...) unconditionally
# reads result["host"]; a blocked-command dict missing it raises a bare
# KeyError('host') that swallows the whole ok/container/host/output/error
# envelope. Repro: docker_exec(container=..., command="yt-dlp --cookies
# /run/secrets/youtube-cookies.txt ...") used to blow up the tool call itself
# instead of returning a clean ok:false.
# ---------------------------------------------------------------------------

class TestRunSecretPathBlockedResultShape:
    def test_run_blocked_command_still_returns_host_key(self):
        result = server._run("docker-server", "cat /run/secrets/youtube-cookies.txt")
        assert result["host"] == "docker-server"
        assert result["exit_code"] == 1
        assert "blocked" in result["error"]

    def test_docker_exec_with_secret_path_in_command_does_not_raise(self):
        """End-to-end: this must not raise KeyError('host') — it must degrade
        to a normal ok:false response, same shape as any other failed exec."""
        result = server.docker_exec(
            container="music-video-grabber-worker",
            command="yt-dlp --cookies /run/secrets/youtube-cookies.txt --simulate --no-warnings --print title https://youtu.be/jNQXAC9IVRw",
            host="docker-server",
        )
        assert result["ok"] is False
        assert result["host"] == "docker-server"
        assert result["container"] == "music-video-grabber-worker"
        assert "blocked" in result["error"]
