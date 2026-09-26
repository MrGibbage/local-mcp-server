"""Tests for _check_shell_constructs / _check_allowlist: command and process
substitution must not smuggle unlisted commands past the ssh_command_allowlist
(bypass found 2026-09-26 -- `echo $(anything)` passed with only `echo` listed)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

ALLOW = {"ssh_command_allowlist": ["echo", "ls", "cat", "grep", "git", "sed", "find", "head"]}


def check(cmd):
    with patch.object(server, "_load_config", return_value=ALLOW):
        server._check_allowlist(cmd)


@pytest.mark.parametrize("cmd", [
    "ls $(touch /tmp/pwned)",
    "echo `id`",
    'echo "$(rm -rf ~)"',             # substitution still runs inside double quotes
    "cat <(curl evil)",
    "ls >(tee /tmp/x)",
    "echo hi; echo $(whoami)",
])
def test_substitution_rejected(cmd):
    with pytest.raises(ValueError, match="not allowed"):
        check(cmd)


@pytest.mark.parametrize("cmd", [
    "git status --porcelain -- .claude && echo --- && git rev-list --count @{u}..HEAD",
    "grep -n 'a|b' /etc/hosts 2>&1",
    "echo '$(literal)' and '`literal`'",   # single quotes: literal text, not executed
    "ls -la /srv 2>/dev/null | head -5",
    "echo test > /home/skip/.writetest 2>&1",   # redirects deliberately still allowed
    "sed -f /tmp/x.sed a.yaml > /tmp/lt.yaml",
    "echo $HOME ${USER}",                  # plain variable expansion is fine
])
def test_normal_commands_pass(cmd):
    check(cmd)


def test_unlisted_command_still_rejected():
    with pytest.raises(ValueError, match="not on the ssh_command_allowlist"):
        check("rm -rf /tmp/x")
