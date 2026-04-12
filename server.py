"""
Homelab MCP Server
------------------
An MCP server that exposes homelab management tools over SSE transport.
Connects to remote hosts via SSH using paramiko.
"""

from __future__ import annotations

import json
import os
import stat as _stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import paramiko
import requests as _requests
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

mcp = FastMCP("Homelab MCP", host=HOST, port=PORT, auth=None)

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
def ssh_exec(command: str, host: Optional[str] = None, max_lines: int = 200) -> dict:
    """
    Run an arbitrary shell command on a named host via SSH.

    Returns stdout, stderr, exit_code, host, and command.
    If ssh_command_allowlist is set in config.yaml, only listed base commands
    are permitted.

    Args:
        command: Shell command to run.
        host: Named host from config (defaults to default_host).
        max_lines: Truncate stdout to this many lines (default 200). Use 0 for unlimited.
    """
    try:
        _check_allowlist(command)
        result = _run(host, command)
        result["command"] = command
        if max_lines and result["stdout"]:
            lines = result["stdout"].splitlines()
            if len(lines) > max_lines:
                result["stdout"] = "\n".join(lines[-max_lines:])
                result["truncated"] = True
        return result
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
    fmt = '{"Name":"{{.Names}}","Image":"{{.Image}}","Status":"{{.Status}}","Ports":"{{.Ports}}"}'
    try:
        result = _run(host, f"docker ps --format '{fmt}'")
        if result["exit_code"] != 0:
            return {"error": result["stderr"], "exit_code": result["exit_code"]}
        containers = []
        for line in result["stdout"].splitlines():
            line = line.strip()
            if line:
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    containers.append({"raw": line})
        return {"containers": containers, "count": len(containers), "exit_code": 0}
    except ValueError as exc:
        return {"error": str(exc), "exit_code": -1}


@mcp.tool()
def docker_logs(container: str, host: Optional[str] = None, tail: int = 100) -> dict:
    """
    Fetch recent logs from a Docker container.

    Args:
        container: Container name or ID.
        host: Named host from config (defaults to default_host).
        tail: Number of log lines to return (default 100, max 500).
    """
    try:
        capped_tail = min(int(tail), 500)
        return _run(host, f"docker logs --tail {capped_tail} {container} 2>&1")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@mcp.tool()
def docker_restart(container: str, host: Optional[str] = None) -> dict:
    """Restart a Docker container by name or ID."""
    try:
        result = _run(host, f"docker restart {container}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@mcp.tool()
def docker_stop(container: str, host: Optional[str] = None) -> dict:
    """Stop a running Docker container."""
    try:
        result = _run(host, f"docker stop {container}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@mcp.tool()
def docker_start(container: str, host: Optional[str] = None) -> dict:
    """Start a stopped Docker container."""
    try:
        result = _run(host, f"docker start {container}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@mcp.tool()
def docker_pull(image: str, host: Optional[str] = None) -> dict:
    """
    Pull a Docker image on a host.

    Args:
        image: Image name, e.g. 'nginx:latest' or 'ghcr.io/linuxserver/plex'.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"docker pull {image}")
        ok = result["exit_code"] == 0
        # Extract just the final status line — skip the noisy per-layer progress lines
        status = ""
        if result["stdout"]:
            for line in reversed(result["stdout"].splitlines()):
                line = line.strip()
                if line:
                    status = line
                    break
        return {"ok": ok, "image": image, "host": result["host"],
                "status": status,
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "image": image, "error": str(exc)}


@mcp.tool()
def docker_inspect(container: str, format: Optional[str] = None, host: Optional[str] = None) -> dict:
    """
    Inspect a Docker container's configuration and runtime state.

    Returns the full JSON inspection data, or a specific field when format is
    provided. Use this to check security options, resource limits, mounts,
    environment variables, and network settings.

    Args:
        container: Container name or ID.
        format: Optional Go template format string, e.g.
                '{{.HostConfig.SecurityOpt}}' or '{{.State.Status}}'.
        host: Named host from config (defaults to default_host).
    """
    try:
        fmt_flag = f" --format '{format}'" if format else ""
        result = _run(host, f"docker inspect{fmt_flag} {container}")
        ok = result["exit_code"] == 0
        if not ok:
            return {"ok": False, "container": container, "host": result["host"],
                    "error": result["stderr"]}
        output = result["stdout"].strip()
        if not format:
            try:
                data = json.loads(output)
                return {"ok": True, "container": container, "host": result["host"],
                        "data": data[0] if isinstance(data, list) and len(data) == 1 else data}
            except json.JSONDecodeError:
                pass
        return {"ok": True, "container": container, "host": result["host"], "output": output}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@mcp.tool()
def docker_stats(container: str, host: Optional[str] = None) -> dict:
    """
    Get a one-shot resource usage snapshot for a Docker container.

    Returns CPU%, memory usage/limit, memory%, and network I/O.

    Args:
        container: Container name or ID.
        host: Named host from config (defaults to default_host).
    """
    fmt = '{"Name":"{{.Name}}","CPU":"{{.CPUPerc}}","MemUsage":"{{.MemUsage}}","MemPerc":"{{.MemPerc}}","NetIO":"{{.NetIO}}","BlockIO":"{{.BlockIO}}"}'
    try:
        result = _run(host, f"docker stats --no-stream --format '{fmt}' {container}")
        ok = result["exit_code"] == 0
        if not ok:
            return {"ok": False, "container": container, "host": result["host"],
                    "error": result["stderr"]}
        output = result["stdout"].strip()
        try:
            data = json.loads(output)
            return {"ok": True, "container": container, "host": result["host"], "stats": data}
        except json.JSONDecodeError:
            return {"ok": True, "container": container, "host": result["host"], "raw": output}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@mcp.tool()
def docker_compose_up(path: str, host: Optional[str] = None) -> dict:
    """
    Run 'docker compose up -d' in the given directory on a host.

    Args:
        path: Absolute path to the directory containing docker-compose.yml.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"docker compose -f {path}/docker-compose.yml up -d 2>&1")
        ok = result["exit_code"] == 0
        lines = result["stdout"].splitlines()
        return {"ok": ok, "host": result["host"], "path": path,
                "stdout": "\n".join(lines[-50:]) if lines else "",
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@mcp.tool()
def docker_compose_down(path: str, host: Optional[str] = None) -> dict:
    """
    Run 'docker compose down' in the given directory on a host.

    Args:
        path: Absolute path to the directory containing docker-compose.yml.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"docker compose -f {path}/docker-compose.yml down 2>&1")
        ok = result["exit_code"] == 0
        lines = result["stdout"].splitlines()
        return {"ok": ok, "host": result["host"], "path": path,
                "stdout": "\n".join(lines[-50:]) if lines else "",
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


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
        result = _run(host, f"systemctl restart {service}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "service": service, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "service": service, "error": str(exc)}


@mcp.tool()
def systemctl_list(host: Optional[str] = None, state: Optional[str] = None) -> dict:
    """
    List systemd service units on a host, with optional state filter.

    Useful for discovering what is running, failed, or inactive without
    needing to know service names in advance.

    Args:
        host: Named host from config (defaults to default_host).
        state: Filter by unit state — e.g. "failed", "active", "inactive",
               "running". Omit to list all loaded service units.
    """
    try:
        cmd = "systemctl list-units --type=service --no-pager --plain --no-legend"
        if state:
            cmd += f" --state={state}"
        result = _run(host, cmd)
        if result["exit_code"] != 0:
            return {"ok": False, "error": result["stderr"], "host": result["host"]}
        units = []
        for line in result["stdout"].splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            units.append({
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            })
        return {"units": units, "count": len(units), "host": result["host"]}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — File I/O
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(path: str, host: Optional[str] = None, max_bytes: int = 51200,
              use_sudo: bool = False) -> dict:
    """
    Read the contents of a file on a remote host over SSH (SFTP).

    Returns the file content as a string, or an error message.

    Args:
        path: Absolute path to the file on the remote host.
        host: Named host from config (defaults to default_host).
        max_bytes: Maximum bytes to read (default 50 KB). Use 0 for unlimited.
        use_sudo: If True, read via 'sudo cat' instead of SFTP. Use for root-owned
                  files that the SSH user cannot access directly. Requires passwordless
                  sudo on the target host.
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"content": None, "error": str(exc), "host": host, "path": path}

    if use_sudo:
        try:
            result = _run(host, f"sudo cat {path}")
            if result["exit_code"] != 0:
                return {"content": None, "error": result["stderr"],
                        "host": host_name, "path": path}
            content = result["stdout"]
            out: dict[str, Any] = {"content": content, "host": host_name, "path": path}
            if max_bytes and len(content.encode()) > max_bytes:
                out["content"] = content.encode()[:max_bytes].decode("utf-8", errors="replace")
                out["truncated"] = True
            return out
        except ValueError as exc:
            return {"content": None, "error": str(exc), "host": host_name, "path": path}

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
            raw = f.read(max_bytes if max_bytes else -1)
        sftp.close()
        content = raw.decode("utf-8", errors="replace")
        result: dict[str, Any] = {"content": content, "host": host_name, "path": path}
        if max_bytes and len(raw) == max_bytes:
            result["truncated"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        return {"content": None, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@mcp.tool()
def write_file(path: str, content: str, host: Optional[str] = None,
               use_sudo: bool = False) -> dict:
    """
    Write (overwrite) a file on a remote host over SSH (SFTP).

    WARNING: This replaces the file entirely. Make sure to read it first if you
    only intend to make partial changes.

    Args:
        path: Absolute path to the file on the remote host.
        content: File content to write (UTF-8).
        host: Named host from config (defaults to default_host).
        use_sudo: If True, write via 'sudo tee' instead of SFTP. Use for root-owned
                  files that the SSH user cannot write directly. Requires passwordless
                  sudo on the target host.
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "host": host, "path": path}

    if use_sudo:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
            stdin, stdout, stderr = client.exec_command(f"sudo tee {path} > /dev/null")
            stdin.write(content.encode("utf-8"))
            stdin.channel.shutdown_write()
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            client.close()
            if exit_code != 0:
                return {"success": False, "error": err, "host": host_name, "path": path}
            return {"success": True, "host": host_name, "path": path}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc), "host": host_name, "path": path}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs = {
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
        return {"success": True, "host": host_name, "path": path}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@mcp.tool()
def patch_file(
    path: str,
    old_string: str,
    new_string: str,
    host: Optional[str] = None,
    replace_all: bool = False,
) -> dict:
    """
    Make a targeted string replacement in a remote file over SSH (SFTP).

    Reads the file, replaces old_string with new_string, and writes it back —
    without loading the full content into the conversation. Prefer this over
    read_file + write_file for small edits to large config files.

    If old_string appears more than once and replace_all is False, the tool
    refuses and returns the match count so you can widen the context to make
    old_string unique.

    Args:
        path: Absolute path to the file on the remote host.
        old_string: Exact string to find (not a regex). Must be unique unless
                    replace_all is True.
        new_string: String to replace it with.
        host: Named host from config (defaults to default_host).
        replace_all: If True, replace every occurrence. Default False.
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "host": host, "path": path}

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

        count = content.count(old_string)
        if count == 0:
            return {"ok": False, "error": "old_string not found in file.", "host": host_name, "path": path}
        if count > 1 and not replace_all:
            return {
                "ok": False,
                "error": (
                    f"old_string found {count} times — refusing to replace ambiguously. "
                    "Expand old_string to include more context to make it unique, "
                    "or set replace_all=True to replace all occurrences."
                ),
                "match_count": count,
                "host": host_name,
                "path": path,
            }

        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        with sftp.file(path, "w") as f:
            f.write(new_content.encode("utf-8"))
        sftp.close()

        return {
            "ok": True,
            "host": host_name,
            "path": path,
            "replacements": count if replace_all else 1,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@mcp.tool()
def tail_file(path: str, lines: int = 50, host: Optional[str] = None) -> dict:
    """
    Return the last N lines of a file on a remote host.

    More efficient than read_file for log files — avoids loading the entire
    file into context when you only need recent entries.

    Args:
        path: Absolute path to the file on the remote host.
        lines: Number of lines to return from the end (default 50, max 500).
        host: Named host from config (defaults to default_host).
    """
    try:
        capped = min(int(lines), 500)
        result = _run(host, f"tail -n {capped} {path}")
        if result["exit_code"] != 0:
            return {"ok": False, "error": result["stderr"], "host": result["host"], "path": path}
        return {
            "content": result["stdout"],
            "lines": len(result["stdout"].splitlines()),
            "host": result["host"],
            "path": path,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "path": path}


@mcp.tool()
def grep_file(path: str, pattern: str, host: Optional[str] = None, context: int = 0) -> dict:
    """
    Search for a pattern in a remote file and return matching lines.

    Returns only the matching lines (with optional context) rather than the
    full file — keeps large file content out of the conversation.

    Args:
        path: Absolute path to the file on the remote host.
        pattern: Search string or basic regex pattern.
        host: Named host from config (defaults to default_host).
        context: Number of lines to show before and after each match (default 0, max 5).
    """
    try:
        ctx = min(int(context), 5)
        ctx_flag = f" -C {ctx}" if ctx > 0 else ""
        result = _run(host, f"grep -n{ctx_flag} {pattern!r} {path}")
        if result["exit_code"] == 1:
            # exit code 1 = no matches (not an error)
            return {"matches": [], "match_count": 0, "host": result["host"], "path": path}
        if result["exit_code"] != 0:
            return {"ok": False, "error": result["stderr"], "host": result["host"], "path": path}
        return {
            "matches": result["stdout"].splitlines(),
            "match_count": len([l for l in result["stdout"].splitlines() if ":" in l]),
            "host": result["host"],
            "path": path,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "path": path}


@mcp.tool()
def stat_file(path: str, host: Optional[str] = None) -> dict:
    """
    Return metadata for a file or directory on a remote host without reading its content.

    Returns exists, type (file/directory/symlink), size_bytes, size_kb, and
    modified time. Use this to check whether a path exists or how large a file
    is before deciding to read it.

    Args:
        path: Absolute path on the remote host.
        host: Named host from config (defaults to default_host).
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "host": host, "path": path}

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
        try:
            attr = sftp.stat(path)
        except FileNotFoundError:
            return {"exists": False, "host": host_name, "path": path}
        finally:
            sftp.close()

        mode = attr.st_mode or 0
        if _stat.S_ISDIR(mode):
            file_type = "directory"
        elif _stat.S_ISLNK(mode):
            file_type = "symlink"
        else:
            file_type = "file"

        size_bytes = attr.st_size or 0
        modified = (
            datetime.fromtimestamp(attr.st_mtime, tz=timezone.utc).isoformat()
            if attr.st_mtime else None
        )
        return {
            "exists": True,
            "type": file_type,
            "size_bytes": size_bytes,
            "size_kb": round(size_bytes / 1024, 1),
            "modified": modified,
            "host": host_name,
            "path": path,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@mcp.tool()
def list_directory(path: str, host: Optional[str] = None, all: bool = True) -> dict:
    """
    List the contents of a directory on a remote host with ownership and permission details.

    Returns a structured list of entries with name, type, permissions, owner, group,
    size, and modified time. Use this to check file ownership, find config files, or
    verify directory contents before reading or writing.

    Args:
        path: Absolute path to the directory on the remote host.
        host: Named host from config (defaults to default_host).
        all: Include hidden files (dot-files). Default True.
    """
    try:
        all_flag = "-la" if all else "-l"
        result = _run(host, f"ls {all_flag} --time-style=long-iso {path} 2>&1")
        ok = result["exit_code"] == 0
        if not ok:
            return {"ok": False, "path": path, "host": result["host"], "error": result["stdout"]}

        entries = []
        for line in result["stdout"].splitlines():
            # Skip "total N" header line
            if line.startswith("total "):
                continue
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms, _, owner, group, size, date, time_, *name_parts = parts
            name = " ".join(name_parts)
            # Resolve symlink display (name -> target)
            if " -> " in name:
                display_name, target = name.split(" -> ", 1)
            else:
                display_name, target = name, None

            entry: dict[str, Any] = {
                "name": display_name,
                "permissions": perms,
                "owner": owner,
                "group": group,
                "size_bytes": int(size) if size.isdigit() else size,
                "modified": f"{date} {time_}",
                "type": "directory" if perms.startswith("d") else
                        "symlink" if perms.startswith("l") else "file",
            }
            if target:
                entry["symlink_target"] = target
            entries.append(entry)

        return {"ok": True, "path": path, "host": result["host"],
                "entries": entries, "count": len(entries)}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@mcp.tool()
def backup_file(path: str, host: Optional[str] = None) -> dict:
    """
    Create a timestamped backup of a file on a remote host before editing it.

    Copies the file to <path>.backup.YYYYMMDD-HHMM in the same directory.
    Run this before patch_file or write_file when editing important config files.

    Args:
        path: Absolute path to the file to back up.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"cp {path} {path}.backup.$(date +%Y%m%d-%H%M)")
        ok = result["exit_code"] == 0
        return {
            "ok": ok,
            "path": path,
            "backup_path": f"{path}.backup.<YYYYMMDD-HHMM>",
            "host": result["host"],
            **({"error": result["stderr"]} if not ok else {}),
        }
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@mcp.tool()
def validate_config(path: str, host: Optional[str] = None) -> dict:
    """
    Validate a YAML or JSON config file on a remote host without restarting any service.

    Reads the file via SFTP and parses it locally — catches syntax errors before
    they cause a failed service restart. File type is detected from the extension
    (.yml/.yaml for YAML, .json for JSON).

    Args:
        path: Absolute path to the config file on the remote host.
        host: Named host from config (defaults to default_host).
    """
    try:
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "host": host, "path": path}

    ext = Path(path).suffix.lower()
    if ext in (".yml", ".yaml"):
        file_type = "yaml"
    elif ext == ".json":
        file_type = "json"
    else:
        return {"ok": False, "error": f"Unsupported extension '{ext}'. Expected .yml, .yaml, or .json.", "path": path}

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

        try:
            if file_type == "yaml":
                yaml.safe_load(content)
            else:
                json.loads(content)
            return {"ok": True, "valid": True, "file_type": file_type, "host": host_name, "path": path}
        except Exception as parse_exc:
            return {
                "ok": True,
                "valid": False,
                "file_type": file_type,
                "error": str(parse_exc),
                "host": host_name,
                "path": path,
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "host": host_name, "path": path}
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
# Tools — BookStack (uses REST API)
# ---------------------------------------------------------------------------


def _bs_cfg() -> tuple[str, dict]:
    """Return (base_url, headers) for BookStack API calls."""
    cfg = CONFIG.get("bookstack", {})
    base_url = cfg.get("url", "").rstrip("/")
    token_id = cfg.get("token_id", "")
    token_secret = cfg.get("token_secret", "")
    if not (base_url and token_id and token_secret):
        raise ValueError(
            "bookstack config missing — set bookstack.url, token_id, token_secret in config.yaml"
        )
    headers = {
        "Authorization": f"Token {token_id}:{token_secret}",
        "Content-Type": "application/json",
    }
    return base_url, headers


@mcp.tool()
def bookstack_search(query: str, count: int = 10) -> dict:
    """
    Search BookStack pages, chapters, and books.

    Returns a list of matching items with id, name, type, url, and a short preview.

    Args:
        query: Search string (supports BookStack filter syntax).
        count: Max results to return (default 10, capped at 30).
    """
    try:
        base_url, headers = _bs_cfg()
        capped = min(int(count), 30)
        resp = _requests.get(
            f"{base_url}/api/search",
            headers=headers,
            params={"query": query, "count": capped},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("data", []):
            results.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "type": item.get("type"),
                "url": item.get("url"),
                "preview": (item.get("preview_html", {}).get("content", "") or "")[:300],
            })
        return {"results": results, "total": data.get("total", len(results))}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_read_page(page_id: int) -> dict:
    """
    Read a BookStack page by ID.

    Returns id, name, content, editor_type ("markdown" or "html"), and updated_at.
    editor_type will be "html" for older pages not yet converted — those need to be
    opened in the BookStack UI and re-saved as Markdown to convert them.
    Content is capped at 50 KB; truncated flag is set if cut.

    Args:
        page_id: Numeric BookStack page ID.
    """
    MAX_BYTES = 51200
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(
            f"{base_url}/api/pages/{page_id}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        md = page.get("markdown") or ""
        html = page.get("html") or ""
        if md:
            editor_type = "markdown"
            raw_content = md
        else:
            editor_type = "html"
            raw_content = html
        truncated = False
        if len(raw_content.encode("utf-8")) > MAX_BYTES:
            raw_content = raw_content.encode("utf-8")[:MAX_BYTES].decode("utf-8", errors="ignore")
            truncated = True
        result: dict[str, Any] = {
            "id": page.get("id"),
            "name": page.get("name"),
            "editor_type": editor_type,
            "content": raw_content,
            "updated_at": page.get("updated_at"),
        }
        if truncated:
            result["truncated"] = True
        return result
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_update_page(page_id: int, markdown: Optional[str] = None, name: Optional[str] = None) -> dict:
    """
    Update an existing BookStack page. Can update content, title, or both.

    At least one of markdown or name must be provided.
    Sending markdown sets the page to Markdown editor mode and will convert
    an HTML page to Markdown in the process.

    Returns ok, id, name, and url on success.

    Args:
        page_id: Numeric BookStack page ID.
        markdown: Full Markdown content to replace the page body with.
        name: New page title.
    """
    if markdown is None and name is None:
        return {"ok": False, "error": "At least one of markdown or name must be provided."}
    try:
        base_url, headers = _bs_cfg()
        payload: dict[str, Any] = {}
        if markdown is not None:
            payload["markdown"] = markdown
        if name is not None:
            payload["name"] = name
        resp = _requests.put(
            f"{base_url}/api/pages/{page_id}",
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        return {
            "ok": True,
            "id": page.get("id"),
            "name": page.get("name"),
            "url": page.get("url"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_create_page(
    book_id: int,
    name: str,
    markdown: str,
    chapter_id: Optional[int] = None,
) -> dict:
    """
    Create a new BookStack page with Markdown content.

    If chapter_id is provided the page is created inside that chapter;
    otherwise it is created at the book's top level.

    Returns ok, id, name, and url on success.

    Args:
        book_id: ID of the book to create the page in.
        name: Page title.
        markdown: Markdown content for the page body.
        chapter_id: Optional chapter ID to nest the page under.
    """
    try:
        base_url, headers = _bs_cfg()
        payload: dict[str, Any] = {
            "book_id": book_id,
            "name": name,
            "markdown": markdown,
        }
        if chapter_id is not None:
            payload["chapter_id"] = chapter_id
        resp = _requests.post(
            f"{base_url}/api/pages",
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        return {
            "ok": True,
            "id": page.get("id"),
            "name": page.get("name"),
            "url": page.get("url"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_list_books() -> dict:
    """
    List all books in BookStack.

    Returns a list of {id, name, description} — useful for navigation and
    for finding book_id values needed by bookstack_create_page.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(
            f"{base_url}/api/books",
            headers=headers,
            params={"count": 100},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        books = [
            {
                "id": b.get("id"),
                "name": b.get("name"),
                "description": (b.get("description") or "").strip(),
            }
            for b in data.get("data", [])
        ]
        return {"books": books, "total": len(books)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_get_book_contents(book_id: int) -> dict:
    """
    Return the chapter and page tree for a specific book.

    Chapters include their nested pages. Top-level pages (not in any chapter)
    are listed separately under "pages".

    Useful for finding chapter_id values before creating a page, or for
    understanding the structure of a book before updating.

    Args:
        book_id: Numeric BookStack book ID.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(
            f"{base_url}/api/books/{book_id}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        book = resp.json()
        chapters = []
        top_pages = []
        for item in book.get("contents", []):
            if item.get("type") == "chapter":
                chapters.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "pages": [
                        {"id": p.get("id"), "name": p.get("name")}
                        for p in item.get("pages", [])
                    ],
                })
            elif item.get("type") == "page":
                top_pages.append({"id": item.get("id"), "name": item.get("name")})
        return {
            "id": book.get("id"),
            "name": book.get("name"),
            "chapters": chapters,
            "pages": top_pages,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_get_page_history(page_id: int, limit: int = 5) -> dict:
    """
    List recent revisions for a BookStack page.

    Returns revision entries with id, name, editor (user display name),
    and created_at timestamp — newest first.

    Args:
        page_id: Numeric BookStack page ID.
        limit: Number of revisions to return (default 5, capped at 20).
    """
    try:
        base_url, headers = _bs_cfg()
        capped = min(int(limit), 20)
        resp = _requests.get(
            f"{base_url}/api/pages/{page_id}/history",
            headers=headers,
            params={"count": capped},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        revisions = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "editor": (r.get("createdBy") or {}).get("name") or r.get("created_by"),
                "created_at": r.get("created_at"),
            }
            for r in data.get("data", [])
        ]
        return {"page_id": page_id, "revisions": revisions}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_delete_page(page_id: int, confirm: bool = False) -> dict:
    """
    Delete a BookStack page.

    Call without confirm=True first — returns the page name for review.
    Re-call with confirm=True to permanently delete.

    Args:
        page_id: Numeric BookStack page ID.
        confirm: Must be True to actually delete.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(f"{base_url}/api/pages/{page_id}", headers=headers, timeout=15)
        resp.raise_for_status()
        page = resp.json()
        name = page.get("name", f"page {page_id}")
        if not confirm:
            return {
                "ok": False,
                "error": "Confirmation required",
                "warning": f"This will permanently delete page '{name}'. Re-call with confirm=true to proceed.",
            }
        del_resp = _requests.delete(f"{base_url}/api/pages/{page_id}", headers=headers, timeout=15)
        del_resp.raise_for_status()
        return {"ok": True, "deleted": {"id": page_id, "name": name}}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_create_chapter(book_id: int, name: str, description: Optional[str] = None) -> dict:
    """
    Create a new chapter inside a book.

    Returns ok, id, name, and book_id on success.

    Args:
        book_id: ID of the book to create the chapter in.
        name: Chapter title.
        description: Optional short description.
    """
    try:
        base_url, headers = _bs_cfg()
        payload: dict[str, Any] = {"book_id": book_id, "name": name}
        if description is not None:
            payload["description"] = description
        resp = _requests.post(f"{base_url}/api/chapters", headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        chapter = resp.json()
        return {
            "ok": True,
            "id": chapter.get("id"),
            "name": chapter.get("name"),
            "book_id": chapter.get("book_id"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_update_chapter(chapter_id: int, name: Optional[str] = None, description: Optional[str] = None) -> dict:
    """
    Update a chapter's title and/or description.

    At least one of name or description must be provided.

    Returns ok, id, name on success.

    Args:
        chapter_id: Numeric BookStack chapter ID.
        name: New chapter title.
        description: New chapter description.
    """
    if name is None and description is None:
        return {"ok": False, "error": "At least one of name or description must be provided."}
    try:
        base_url, headers = _bs_cfg()
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        resp = _requests.put(
            f"{base_url}/api/chapters/{chapter_id}",
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        chapter = resp.json()
        return {"ok": True, "id": chapter.get("id"), "name": chapter.get("name")}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_delete_chapter(chapter_id: int, confirm: bool = False) -> dict:
    """
    Delete a BookStack chapter and all pages inside it.

    Call without confirm=True first — returns chapter name and page count for review.
    Re-call with confirm=True to permanently delete.

    Args:
        chapter_id: Numeric BookStack chapter ID.
        confirm: Must be True to actually delete.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(f"{base_url}/api/chapters/{chapter_id}", headers=headers, timeout=15)
        resp.raise_for_status()
        chapter = resp.json()
        name = chapter.get("name", f"chapter {chapter_id}")
        page_count = len(chapter.get("pages", []))
        if not confirm:
            return {
                "ok": False,
                "error": "Confirmation required",
                "warning": (
                    f"This will permanently delete chapter '{name}' and its {page_count} page(s). "
                    "Re-call with confirm=true to proceed."
                ),
            }
        del_resp = _requests.delete(f"{base_url}/api/chapters/{chapter_id}", headers=headers, timeout=15)
        del_resp.raise_for_status()
        return {"ok": True, "deleted": {"id": chapter_id, "name": name, "pages_deleted": page_count}}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_create_book(name: str, description: Optional[str] = None) -> dict:
    """
    Create a new book in BookStack.

    Returns ok, id, name, and url on success.

    Args:
        name: Book title.
        description: Optional short description.
    """
    try:
        base_url, headers = _bs_cfg()
        payload: dict[str, Any] = {"name": name}
        if description is not None:
            payload["description"] = description
        resp = _requests.post(f"{base_url}/api/books", headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        book = resp.json()
        return {
            "ok": True,
            "id": book.get("id"),
            "name": book.get("name"),
            "url": book.get("url"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_update_book(book_id: int, name: Optional[str] = None, description: Optional[str] = None) -> dict:
    """
    Update a book's title and/or description.

    At least one of name or description must be provided.

    Returns ok, id, name, url on success.

    Args:
        book_id: Numeric BookStack book ID.
        name: New book title.
        description: New book description.
    """
    if name is None and description is None:
        return {"ok": False, "error": "At least one of name or description must be provided."}
    try:
        base_url, headers = _bs_cfg()
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        resp = _requests.put(f"{base_url}/api/books/{book_id}", headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        book = resp.json()
        return {
            "ok": True,
            "id": book.get("id"),
            "name": book.get("name"),
            "url": book.get("url"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_delete_book(book_id: int, confirm: bool = False) -> dict:
    """
    Delete a BookStack book and everything inside it (all chapters and pages).

    Call without confirm=True first — returns book name with chapter and page counts for review.
    Re-call with confirm=True to permanently delete.

    Args:
        book_id: Numeric BookStack book ID.
        confirm: Must be True to actually delete.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(f"{base_url}/api/books/{book_id}", headers=headers, timeout=15)
        resp.raise_for_status()
        book = resp.json()
        name = book.get("name", f"book {book_id}")
        contents = book.get("contents", [])
        chapter_count = sum(1 for i in contents if i.get("type") == "chapter")
        page_count = sum(
            len(i.get("pages", [])) if i.get("type") == "chapter" else 1
            for i in contents
        )
        if not confirm:
            return {
                "ok": False,
                "error": "Confirmation required",
                "warning": (
                    f"This will permanently delete book '{name}' including "
                    f"{chapter_count} chapter(s) and {page_count} page(s). "
                    "Re-call with confirm=true to proceed."
                ),
            }
        del_resp = _requests.delete(f"{base_url}/api/books/{book_id}", headers=headers, timeout=15)
        del_resp.raise_for_status()
        return {
            "ok": True,
            "deleted": {
                "id": book_id,
                "name": name,
                "chapters_deleted": chapter_count,
                "pages_deleted": page_count,
            },
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_get_page_metadata(page_id: int) -> dict:
    """
    Return metadata for a BookStack page without fetching its content.

    Use this to check page size, editor type, location, and timestamps before
    deciding whether to read the full page. Useful for keeping context usage low —
    if size_kb is over 10, consider whether the full content is needed.

    Returns id, name, url, editor_type, size_bytes, size_kb, book_id, chapter_id,
    created_at, updated_at, and revision_count.

    Args:
        page_id: Numeric BookStack page ID.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.get(f"{base_url}/api/pages/{page_id}", headers=headers, timeout=15)
        resp.raise_for_status()
        page = resp.json()
        md = page.get("markdown") or ""
        html = page.get("html") or ""
        if md:
            editor_type = "markdown"
            content = md
        else:
            editor_type = "html"
            content = html
        size_bytes = len(content.encode("utf-8"))
        return {
            "id": page.get("id"),
            "name": page.get("name"),
            "url": page.get("url"),
            "editor_type": editor_type,
            "size_bytes": size_bytes,
            "size_kb": round(size_bytes / 1024, 1),
            "book_id": page.get("book_id"),
            "chapter_id": page.get("chapter_id"),
            "created_at": page.get("created_at"),
            "updated_at": page.get("updated_at"),
            "revision_count": page.get("revision_count"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_move_page(page_id: int, entity_type: str, entity_id: int) -> dict:
    """
    Move a page to a different book or chapter.

    Args:
        page_id: Numeric ID of the page to move.
        entity_type: Destination type — "book" to place the page at the book's
                     top level, or "chapter" to nest it inside a chapter.
        entity_id: ID of the destination book or chapter.
    """
    if entity_type not in ("book", "chapter"):
        return {"ok": False, "error": "entity_type must be 'book' or 'chapter'."}
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.put(
            f"{base_url}/api/pages/{page_id}/move",
            headers=headers,
            json={"entity_type": entity_type, "entity_id": entity_id},
            timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        return {
            "ok": True,
            "id": page.get("id"),
            "name": page.get("name"),
            "url": page.get("url"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def bookstack_move_chapter(chapter_id: int, book_id: int) -> dict:
    """
    Move a chapter to a different book.

    Args:
        chapter_id: Numeric ID of the chapter to move.
        book_id: ID of the destination book.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.put(
            f"{base_url}/api/chapters/{chapter_id}/move",
            headers=headers,
            json={"entity_type": "book", "entity_id": book_id},
            timeout=15,
        )
        resp.raise_for_status()
        chapter = resp.json()
        return {
            "ok": True,
            "id": chapter.get("id"),
            "name": chapter.get("name"),
            "book_id": chapter.get("book_id"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.routing import Mount, Route

    BASE_URL = "https://homelab-mcp.pelorus.org"

    class StripAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            print(f"MIDDLEWARE: {request.method} {request.url.path}", flush=True)
            scope = request.scope
            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", [])
                if k.lower() != b"authorization"
            ]
            return await call_next(request)

    async def oauth_protected_resource(_request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": BASE_URL,
            "authorization_servers": [BASE_URL],
        })

    async def oauth_authorization_server(_request: Request) -> JSONResponse:
        return JSONResponse({
            "issuer": BASE_URL,
            "authorization_endpoint": f"{BASE_URL}/authorize",
            "token_endpoint": f"{BASE_URL}/token",
            "registration_endpoint": f"{BASE_URL}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        })

    async def register(request: Request) -> JSONResponse:
        body = await request.json()
        print(f"REGISTER REQUEST: {body}", flush=True)
        return JSONResponse({
            "client_id": "anonymous",
            "client_id_issued_at": 1700000000,
            "client_secret": "unused",
            "client_secret_expires_at": 0,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "Claude"),
        })

    async def authorize(request: Request) -> RedirectResponse:
        redirect_uri = request.query_params.get("redirect_uri", "")
        state = request.query_params.get("state", "")
        print(f"AUTHORIZE REQUEST: {dict(request.query_params)}", flush=True)
        return RedirectResponse(
            url=f"{redirect_uri}?code=homelab-auth-code&state={state}",
            status_code=302
        )

    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        print(f"TOKEN REQUEST: {dict(form)}", flush=True)
        return JSONResponse({
            "access_token": "homelab-anonymous-token",
            "token_type": "bearer",
            "expires_in": 86400,
        })

    wellknown_routes = [
        Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/token", token, methods=["POST"]),
    ]
    from contextlib import asynccontextmanager

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            yield

    app = Starlette(
        routes=wellknown_routes + [Mount("/", app=mcp_app)],
        lifespan=lifespan
    )
    app.add_middleware(StripAuthMiddleware)

    print(f"Starting Homelab MCP server on {HOST}:{PORT} (Streamable HTTP transport)", flush=True)
    uvicorn.run(app, host=HOST, port=PORT)