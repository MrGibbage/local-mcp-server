"""
Homelab MCP Server
------------------
An MCP server that exposes homelab management tools over SSE transport.
Connects to remote hosts via SSH using paramiko.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import paramiko
import yaml
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")


def _load_config() -> dict:
    path = Path(CONFIG_PATH)
    if not path.exists():
        print(f"ERROR: config file not found at {path.resolve()}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


CONFIG: dict = _load_config()

_server_cfg = CONFIG.get("server", {})
HOST: str = _server_cfg.get("host", "0.0.0.0")
PORT: int = int(_server_cfg.get("port", 8080))
DEFAULT_HOST: str | None = CONFIG.get("default_host")
ALLOWLIST: list[str] | None = CONFIG.get("ssh_command_allowlist")  # None = unrestricted

# ---------------------------------------------------------------------------
# FastMCP initialisation
# ---------------------------------------------------------------------------

mcp = FastMCP("Homelab MCP", host=HOST, port=PORT)

# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def _resolve_host(host: str | None) -> tuple[str, dict]:
    """Return (host_name, host_config_dict), falling back to default_host."""
    name = host or DEFAULT_HOST
    if name is None:
        raise ValueError("No host specified and no default_host configured.")
    hosts: dict = CONFIG.get("hosts", {})
    if name not in hosts:
        available = list(hosts.keys())
        raise ValueError(f"Host '{name}' not found in config. Available: {available}")
    return name, hosts[name]


def _check_allowlist(command: str) -> None:
    """Raise ValueError if the command's first token is not on the allowlist."""
    if ALLOWLIST is None:
        return
    # Extract the base command name (strip path prefix)
    first_token = command.strip().split()[0] if command.strip() else ""
    base = first_token.split("/")[-1]
    if base not in ALLOWLIST:
        raise ValueError(
            f"Command '{base}' is not on the ssh_command_allowlist. "
            f"Allowed: {ALLOWLIST}"
        )


def _ssh_exec(host_cfg: dict, command: str) -> dict[str, Any]:
    """Open a fresh SSH connection, run command, return stdout/stderr/exit_code."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs: dict[str, Any] = {
            "hostname": host_cfg["hostname"],
            "username": host_cfg.get("user", "root"),
            "timeout": 30,
        }
        key_path = host_cfg.get("key_path")
        if key_path:
            connect_kwargs["key_filename"] = str(key_path)
        port = host_cfg.get("port", 22)
        if port != 22:
            connect_kwargs["port"] = int(port)

        client.connect(**connect_kwargs)
        _, stdout, stderr = client.exec_command(command, timeout=120)
        exit_code = stdout.channel.recv_exit_status()
        return {
            "stdout": stdout.read().decode("utf-8", errors="replace").strip(),
            "stderr": stderr.read().decode("utf-8", errors="replace").strip(),
            "exit_code": exit_code,
        }
    except Exception as exc:  # noqa: BLE001
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}
    finally:
        client.close()


def _run(host: str | None, command: str) -> dict[str, Any]:
    """Resolve host, run command, return result dict."""
    host_name, host_cfg = _resolve_host(host)
    result = _ssh_exec(host_cfg, command)
    result["host"] = host_name
    result["command"] = command
    return result


# ---------------------------------------------------------------------------
# Tools — Host discovery
# ---------------------------------------------------------------------------


@mcp.tool()
def list_hosts() -> dict:
    """Return all configured hosts so the model knows what targets are available."""
    hosts = CONFIG.get("hosts", {})
    return {
        "default_host": DEFAULT_HOST,
        "hosts": {
            name: {
                "hostname": cfg.get("hostname"),
                "user": cfg.get("user"),
                "port": cfg.get("port", 22),
            }
            for name, cfg in hosts.items()
        },
    }


# ---------------------------------------------------------------------------
# Tools — Generic SSH
# ---------------------------------------------------------------------------


@mcp.tool()
def ssh_exec(command: str, host: Optional[str] = None) -> dict:
    """
    Run an arbitrary shell command on a named host via SSH.

    Returns stdout, stderr, exit_code, host, and command.
    If ssh_command_allowlist is set in config.yaml, only listed base commands
    are permitted.
    """
    try:
        _check_allowlist(command)
        return _run(host, command)
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1, "host": host, "command": command}


# ---------------------------------------------------------------------------
# Tools — Docker
# ---------------------------------------------------------------------------


@mcp.tool()
def docker_ps(host: Optional[str] = None) -> dict:
    """
    List running Docker containers on a host.

    Returns a list of containers with name, image, status, and ports.
    """
    try:
        result = _run(host, "docker ps --format '{{json .}}'")
        if result["exit_code"] != 0:
            return result
        containers = []
        for line in result["stdout"].splitlines():
            line = line.strip()
            if line:
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    containers.append({"raw": line})
        result["containers"] = containers
        result["count"] = len(containers)
        return result
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_logs(container: str, host: Optional[str] = None, tail: int = 100) -> dict:
    """
    Fetch recent logs from a Docker container.

    Args:
        container: Container name or ID.
        host: Named host from config (defaults to default_host).
        tail: Number of log lines to return (default 100).
    """
    try:
        return _run(host, f"docker logs --tail {int(tail)} {container} 2>&1")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_restart(container: str, host: Optional[str] = None) -> dict:
    """Restart a Docker container by name or ID."""
    try:
        return _run(host, f"docker restart {container}")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_stop(container: str, host: Optional[str] = None) -> dict:
    """Stop a running Docker container."""
    try:
        return _run(host, f"docker stop {container}")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_start(container: str, host: Optional[str] = None) -> dict:
    """Start a stopped Docker container."""
    try:
        return _run(host, f"docker start {container}")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_pull(image: str, host: Optional[str] = None) -> dict:
    """
    Pull a Docker image on a host.

    Args:
        image: Image name, e.g. 'nginx:latest' or 'ghcr.io/linuxserver/plex'.
        host: Named host from config (defaults to default_host).
    """
    try:
        return _run(host, f"docker pull {image}")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_compose_up(path: str, host: Optional[str] = None) -> dict:
    """
    Run 'docker compose up -d' in the given directory on a host.

    Args:
        path: Absolute path to the directory containing docker-compose.yml.
        host: Named host from config (defaults to default_host).
    """
    try:
        return _run(host, f"docker compose -f {path}/docker-compose.yml up -d 2>&1")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_compose_down(path: str, host: Optional[str] = None) -> dict:
    """
    Run 'docker compose down' in the given directory on a host.

    Args:
        path: Absolute path to the directory containing docker-compose.yml.
        host: Named host from config (defaults to default_host).
    """
    try:
        return _run(host, f"docker compose -f {path}/docker-compose.yml down 2>&1")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


# ---------------------------------------------------------------------------
# Tools — systemctl
# ---------------------------------------------------------------------------


@mcp.tool()
def systemctl_status(service: str, host: Optional[str] = None) -> dict:
    """Return the systemctl status of a service on the named host."""
    try:
        return _run(host, f"systemctl status {service} --no-pager")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def systemctl_restart(service: str, host: Optional[str] = None) -> dict:
    """Restart a systemd service on the named host."""
    try:
        return _run(host, f"systemctl restart {service}")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


# ---------------------------------------------------------------------------
# Tools — File I/O
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(path: str, host: Optional[str] = None) -> dict:
    """
    Read the contents of a file on a remote host over SSH (SFTP).

    Returns the file content as a string, or an error message.
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"content": None, "error": str(exc), "host": host, "path": path}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs: dict[str, Any] = {
            "hostname": host_cfg["hostname"],
            "username": host_cfg.get("user", "root"),
            "timeout": 30,
        }
        key_path = host_cfg.get("key_path")
        if key_path:
            connect_kwargs["key_filename"] = str(key_path)
        port = host_cfg.get("port", 22)
        if port != 22:
            connect_kwargs["port"] = int(port)

        client.connect(**connect_kwargs)
        sftp = client.open_sftp()
        with sftp.file(path, "r") as f:
            content = f.read().decode("utf-8", errors="replace")
        sftp.close()
        return {"content": content, "error": None, "host": host_name, "path": path}
    except Exception as exc:  # noqa: BLE001
        return {"content": None, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@mcp.tool()
def write_file(path: str, content: str, host: Optional[str] = None) -> dict:
    """
    Write (overwrite) a file on a remote host over SSH (SFTP).

    WARNING: This replaces the file entirely. Make sure to read it first if you
    only intend to make partial changes.
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "host": host, "path": path}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs: dict[str, Any] = {
            "hostname": host_cfg["hostname"],
            "username": host_cfg.get("user", "root"),
            "timeout": 30,
        }
        key_path = host_cfg.get("key_path")
        if key_path:
            connect_kwargs["key_filename"] = str(key_path)
        port = host_cfg.get("port", 22)
        if port != 22:
            connect_kwargs["port"] = int(port)

        client.connect(**connect_kwargs)
        sftp = client.open_sftp()
        with sftp.file(path, "w") as f:
            f.write(content.encode("utf-8"))
        sftp.close()
        return {"success": True, "error": None, "host": host_name, "path": path}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Tools — System info
# ---------------------------------------------------------------------------


@mcp.tool()
def disk_usage(host: Optional[str] = None) -> dict:
    """Return disk usage summary (df -h) for the named host."""
    try:
        return _run(host, "df -h")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def memory_usage(host: Optional[str] = None) -> dict:
    """Return memory usage summary (free -h) for the named host."""
    try:
        return _run(host, "free -h")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting Homelab MCP server on {HOST}:{PORT} (SSE transport)", flush=True)
    mcp.run(transport="sse")
