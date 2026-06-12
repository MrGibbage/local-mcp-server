"""
Homelab MCP Server
------------------
An MCP server that exposes homelab management tools over Streamable HTTP transport.
Connects to remote hosts via SSH using paramiko.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re as _re
import shlex
import stat as _stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote as _url_quote

import paramiko
import requests as _requests
import urllib3
import yaml
from mcp.server.fastmcp import FastMCP

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")


CONFIG: dict = {}
_config_mtime: float = 0.0


def _load_config() -> dict:
    """Return the parsed config, reloading from disk only when its mtime changes.

    This lets config.yaml edits take effect on the next tool call without a
    container restart. Tokens are still read from os.environ and require a
    restart to pick up changes — that is expected.
    """
    global CONFIG, _config_mtime
    path = Path(CONFIG_PATH)
    if not path.exists():
        if not CONFIG:
            print(f"ERROR: config file not found at {path.resolve()}", file=sys.stderr)
            sys.exit(1)
        return CONFIG  # transient disappearance mid-edit — keep last good config
    mtime = path.stat().st_mtime
    if mtime != _config_mtime:
        with open(path) as f:
            CONFIG = yaml.safe_load(f) or {}
        _config_mtime = mtime
    return CONFIG


CONFIG = _load_config()

_server_cfg = CONFIG.get("server", {})
HOST: str = _server_cfg.get("host", "0.0.0.0")
PORT: int = int(_server_cfg.get("port", 8080))
# default_host and ssh_command_allowlist are re-read live from CONFIG at call
# time (see _resolve_host / _check_allowlist) so config.yaml edits hot-reload.
DEFAULT_HOST: str | None = CONFIG.get("default_host")
ALLOWLIST: list[str] | None = CONFIG.get("ssh_command_allowlist")  # None = unrestricted

# ---------------------------------------------------------------------------
# Logging — JSON lines for Loki/Promtail
# ---------------------------------------------------------------------------

_LOG_RECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        obj: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "msg": record.message,
        }
        for key, val in record.__dict__.items():
            if key not in _LOG_RECORD_ATTRS:
                obj[key] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logger = logging.getLogger("homelab_mcp")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = _setup_logging()

# ---------------------------------------------------------------------------
# FastMCP initialisation
# ---------------------------------------------------------------------------

mcp = FastMCP("Homelab MCP", host=HOST, port=PORT, auth=None)

# ---------------------------------------------------------------------------
# Tool registration filter — MCP_ENABLED_TOOLS
# ---------------------------------------------------------------------------

_raw_enabled = os.environ.get("MCP_ENABLED_TOOLS")
_ENABLED_TOOLS: frozenset[str] | None = (
    frozenset(t.strip() for t in _raw_enabled.split(",") if t.strip())
    if _raw_enabled else None
)


def _tool(fn):
    if _ENABLED_TOOLS is None or fn.__name__ in _ENABLED_TOOLS:
        return mcp.tool()(fn)
    return fn


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def _resolve_host(host: str | None) -> tuple[str, dict]:
    """Return (host_name, host_config_dict), falling back to default_host."""
    cfg = _load_config()
    name = host or cfg.get("default_host")
    if name is None:
        raise ValueError("No host specified and no default_host configured.")
    hosts: dict = cfg.get("hosts", {})
    if name not in hosts:
        available = list(hosts.keys())
        raise ValueError(f"Host '{name}' not found in config. Available: {available}")
    return name, hosts[name]


def _check_allowlist(command: str, host_cfg: dict | None = None) -> None:
    """Raise ValueError if the command's first token is not on the allowlist.

    Merges the global ssh_command_allowlist with any host-level
    ssh_command_allowlist defined in the host's config block.
    """
    global_allow: list[str] | None = _load_config().get("ssh_command_allowlist")
    host_extra: list[str] = (host_cfg or {}).get("ssh_command_allowlist", [])
    if global_allow is None and not host_extra:
        return
    effective: list[str] = (global_allow or []) + host_extra
    first_token = command.strip().split()[0] if command.strip() else ""
    base = first_token.split("/")[-1]
    if base not in effective:
        raise ValueError(
            f"Command '{base}' is not on the ssh_command_allowlist. "
            f"Allowed: {effective}"
        )


# Sensitive file patterns — these must never be read or written through MCP tools.
_SECRET_PATH_PATTERNS: list[_re.Pattern] = [
    _re.compile(r"/etc/homelab/"),
    _re.compile(r"/srv/local-mcp-server/\.env"),
    _re.compile(r"/srv/local-mcp-server/keys/"),
    _re.compile(r"(^|/)\.env(\.[^/]*)?$"),
    _re.compile(r"\.openclaw/"),
    _re.compile(r"\.ssh/(?!.*\.pub$)"),
    _re.compile(r"/proc/\d+/environ"),
]


def _check_secret_path(path: str) -> None:
    """Raise ValueError if path matches a sensitive file pattern."""
    for pattern in _SECRET_PATH_PATTERNS:
        if pattern.search(path):
            raise ValueError(
                f"blocked: '{path}' matches the secret-path guard. "
                "Credential files must not be read or written through MCP tools."
            )


def _ssh_exec(host_cfg: dict, command: str, timeout: int = 60) -> dict[str, Any]:
    """Open a fresh SSH connection, run command, return stdout/stderr/exit_code.

    Wraps the remote command in `timeout --kill-after=5 <N>` so the remote
    process tree is killed if it runs too long, preventing runaway processes
    from blocking the SSH session pool after the MCP call returns.
    """
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
        # Wrap with remote timeout so the process tree is killed server-side.
        # The paramiko channel timeout is set slightly higher so recv_exit_status
        # sees the normal timeout exit rather than raising a socket timeout.
        wrapped = f"timeout -k 5 {timeout} sh -c {shlex.quote(command)}"
        t0 = time.monotonic()
        _, stdout, stderr = client.exec_command(wrapped, timeout=timeout + 15)
        exit_code = stdout.channel.recv_exit_status()
        duration = round(time.monotonic() - t0, 2)
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        result: dict[str, Any] = {"stdout": out, "stderr": err, "exit_code": exit_code}
        timed_out = exit_code in (124, 137)
        if exit_code == 124:
            result["error"] = f"Command timed out after {timeout}s and was terminated."
        elif exit_code == 137:
            result["error"] = f"Command was force-killed after {timeout}s (SIGKILL)."
        log.log(
            logging.WARNING if timed_out else logging.INFO,
            "ssh_exec timed out" if timed_out else "ssh_exec complete",
            extra={
                "event": "ssh_exec",
                "host": host_cfg.get("hostname"),
                "command_preview": command[:200],
                "duration_s": duration,
                "exit_code": exit_code,
                "timed_out": timed_out,
            },
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("ssh_exec error", extra={
            "event": "ssh_exec",
            "host": host_cfg.get("hostname"),
            "command_preview": command[:200],
            "error": str(exc),
        })
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}
    finally:
        client.close()


def _run(host: str | None, command: str, timeout: int = 60) -> dict[str, Any]:
    """Resolve host, run command, return result dict."""
    for pattern in _SECRET_PATH_PATTERNS:
        if pattern.search(command):
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": 1,
                "error": f"blocked: command targets a secret path. "
                         "Credential files must not be accessed via ssh_exec.",
            }
    host_name, host_cfg = _resolve_host(host)
    result = _ssh_exec(host_cfg, command, timeout=timeout)
    result["host"] = host_name
    return result


# ---------------------------------------------------------------------------
# Proxmox API helpers
# ---------------------------------------------------------------------------


def _resolve_proxmox_node(host: str) -> dict:
    """Return node config dict for a given name or IP address."""
    nodes = _load_config().get("proxmox_nodes", [])
    if not nodes:
        raise ValueError("proxmox_nodes not configured in config.yaml")
    for node in nodes:
        if node.get("name") == host or node.get("host") == host:
            return node
    available = [f"{n.get('name')} ({n.get('host')})" for n in nodes]
    raise ValueError(f"Proxmox node '{host}' not found in config. Available: {available}")


def _proxmox_api(node_cfg: dict, method: str, path: str, **kwargs) -> dict:
    """Make an authenticated Proxmox API request. Returns parsed JSON response."""
    base_url = f"https://{node_cfg['host']}:8006/api2/json"
    env_key = f"{node_cfg['name'].upper()}_API_TOKEN"  # e.g. PROXMOX1_API_TOKEN
    api_token = os.environ.get(env_key, node_cfg.get("api_token", ""))
    if not api_token:
        raise ValueError(
            f"Proxmox token missing — set {env_key} in .env"
        )
    headers = {"Authorization": f"PVEAPIToken={api_token}"}
    resp = _requests.request(
        method,
        f"{base_url}{path}",
        headers=headers,
        verify=False,
        timeout=30,
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


def _proxmox_wait_task(node_cfg: dict, upid: str, timeout: int = 60) -> dict:
    """Poll a Proxmox task UPID until it stops or timeout expires."""
    node = node_cfg["node"]
    encoded_upid = _url_quote(upid, safe="")
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/tasks/{encoded_upid}/status")
        data = resp.get("data", {})
        if data.get("status") == "stopped":
            exitstatus = data.get("exitstatus", "")
            return {
                "finished": True,
                "exitstatus": exitstatus,
                "ok": exitstatus == "OK",
            }
        time.sleep(1)
    return {"finished": False, "ok": False, "error": f"Task did not complete within {timeout}s"}


# ---------------------------------------------------------------------------
# Tools — Host discovery
# ---------------------------------------------------------------------------


@_tool
def list_hosts() -> dict:
    """Return all configured hosts so the model knows what targets are available."""
    cfg = _load_config()
    hosts = cfg.get("hosts", {})
    return {
        "default_host": cfg.get("default_host"),
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


@_tool
def ssh_exec(command: str, host: Optional[str] = None, max_lines: int = 200,
             cwd: Optional[str] = None, timeout: int = 60) -> dict:
    """
    Run an arbitrary shell command on a named host via SSH.

    IMPORTANT: Before using ssh_exec for Docker, file, or system operations, first check
    whether a dedicated MCP tool exists. This server has 60+ specialized tools (docker_ps,
    docker_inspect, list_directory, stat_file, read_file, grep_file, etc.) that are deferred
    and only visible after a ToolSearch call. Example: ToolSearch(query="docker inspect stat")
    loads docker_inspect, stat_file, list_directory, etc. Use ssh_exec only when no dedicated
    MCP tool covers your specific need.

    Returns stdout, stderr, exit_code, host, and command. The remote command is
    wrapped in `timeout --kill-after=5 <timeout>` so runaway processes are
    guaranteed to be killed server-side when the deadline is reached. If the
    command is killed by timeout, exit_code will be 124 (SIGTERM) or 137 (SIGKILL)
    and an "error" key will be present in the result.

    If ssh_command_allowlist is set in config.yaml, only listed base commands
    are permitted.

    Args:
        command: Shell command to run.
        host: Named host from config (defaults to default_host).
        max_lines: Truncate stdout to this many lines (default 200). Use 0 for unlimited.
        cwd: If provided, cd into this directory before running the command.
        timeout: Remote execution timeout in seconds (default 60). The remote
            process tree is killed after this many seconds. Use a larger value
            for known slow operations (e.g. package installs). Max recommended: 90.
    """
    try:
        _, host_cfg = _resolve_host(host)
        _check_allowlist(command, host_cfg)
        if cwd:
            command = f"cd {cwd} && {command}"
        result = _run(host, command, timeout=timeout)
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


@_tool
def docker_ps(host: Optional[str] = None, filter: Optional[str] = None) -> dict:
    """
    List running Docker containers on a host.

    Returns a list of containers with name, image, status, and ports.

    Args:
        host: Named host from config (defaults to default_host).
        filter: Optional filter string passed to docker ps --filter (e.g. "name=loki").
    """
    fmt = '{"Name":"{{.Names}}","Image":"{{.Image}}","Status":"{{.Status}}","Ports":"{{.Ports}}"}'
    try:
        filter_flag = f" --filter {filter}" if filter else ""
        result = _run(host, f"docker ps{filter_flag} --format '{fmt}'")
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


@_tool
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


@_tool
def docker_restart(container: str, host: Optional[str] = None) -> dict:
    """Restart a Docker container by name or ID."""
    try:
        result = _run(host, f"docker restart {container}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@_tool
def docker_stop(container: str, host: Optional[str] = None) -> dict:
    """Stop a running Docker container."""
    try:
        result = _run(host, f"docker stop {container}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@_tool
def docker_start(container: str, host: Optional[str] = None) -> dict:
    """Start a stopped Docker container."""
    try:
        result = _run(host, f"docker start {container}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@_tool
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


@_tool
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
                item = data[0] if isinstance(data, list) and len(data) == 1 else data
                # Redact env vars from the MCP server's own container — they contain secrets.
                if isinstance(item, dict) and item.get("Name", "").lstrip("/") == "homelab-mcp":
                    item.get("Config", {}).pop("Env", None)
                    item["_note"] = "Env redacted for homelab-mcp (contains secrets)"
                return {"ok": True, "container": container, "host": result["host"], "data": item}
            except json.JSONDecodeError:
                pass
        return {"ok": True, "container": container, "host": result["host"], "output": output}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@_tool
def docker_exec(container: str, command: str, host: Optional[str] = None) -> dict:
    """
    Run a command inside a running Docker container.

    Equivalent to 'docker exec <container> <command>'. Use for in-container
    diagnostics such as checking /proc/1/status for capabilities, inspecting
    running processes, or verifying installed packages.

    Args:
        container: Container name or ID.
        command: Command to run inside the container (passed to sh -c).
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"docker exec {container} sh -c {repr(command)}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "container": container, "host": result["host"],
                "output": result["stdout"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@_tool
def docker_capabilities(container: str, host: Optional[str] = None) -> dict:
    """
    Return decoded Linux capabilities for a running container.

    Reads /proc/1/status inside the container to get raw hex capability masks,
    then decodes them via capsh on the host. Requires capsh (libcap2-bin) on the
    target host. Falls back to raw hex values if capsh is unavailable.

    Args:
        container: Container name or ID.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"docker exec {container} sh -c 'grep -E \"^Cap\" /proc/1/status'")
        if result["exit_code"] != 0:
            return {"ok": False, "container": container, "host": result["host"],
                    "error": result["stderr"] or "Failed to read /proc/1/status"}

        masks: dict[str, str] = {}
        for line in result["stdout"].splitlines():
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                masks[key.strip()] = val.strip()

        key_map = {
            "CapInh": "inheritable",
            "CapPrm": "permitted",
            "CapEff": "effective",
            "CapBnd": "bounding",
            "CapAmb": "ambient",
        }
        capabilities: dict[str, list[str]] = {}
        for cap_key, cap_name in key_map.items():
            hex_val = masks.get(cap_key, "0000000000000000")
            decode_result = _run(host, f"capsh --decode={hex_val}")
            if decode_result["exit_code"] != 0:
                capabilities[cap_name] = [f"raw:{hex_val}"]
                continue
            output = decode_result["stdout"].strip()
            if "=" in output:
                _, _, caps_str = output.partition("=")
                cap_list = [c.strip() for c in caps_str.split(",") if c.strip()]
            else:
                cap_list = []
            capabilities[cap_name] = cap_list

        return {"ok": True, "container": container, "host": result["host"],
                "capabilities": capabilities}
    except ValueError as exc:
        return {"ok": False, "container": container, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "container": container, "error": str(exc)}


@_tool
def docker_stats(container: str, host: Optional[str] = None) -> dict:
    """
    Get a one-shot resource usage snapshot for a Docker container.

    Returns CPU%, memory usage/limit, memory%, network I/O, and PID count.

    Args:
        container: Container name or ID.
        host: Named host from config (defaults to default_host).
    """
    fmt = '{"Name":"{{.Name}}","CPU":"{{.CPUPerc}}","MemUsage":"{{.MemUsage}}","MemPerc":"{{.MemPerc}}","NetIO":"{{.NetIO}}","BlockIO":"{{.BlockIO}}","PIDs":"{{.PIDs}}"}'
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


@_tool
def docker_compose_up(path: str, host: Optional[str] = None) -> dict:
    """
    Run 'docker compose up -d' in the given directory on a host.

    Args:
        path: Absolute path to the directory containing docker-compose.yml.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"cd {path} && docker compose up -d 2>&1")
        ok = result["exit_code"] == 0
        lines = result["stdout"].splitlines()
        return {"ok": ok, "host": result["host"], "path": path,
                "stdout": "\n".join(lines[-50:]) if lines else "",
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@_tool
def docker_compose_down(path: str, host: Optional[str] = None) -> dict:
    """
    Run 'docker compose down' in the given directory on a host.

    Args:
        path: Absolute path to the directory containing docker-compose.yml.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"cd {path} && docker compose down 2>&1")
        ok = result["exit_code"] == 0
        lines = result["stdout"].splitlines()
        return {"ok": ok, "host": result["host"], "path": path,
                "stdout": "\n".join(lines[-50:]) if lines else "",
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@_tool
def docker_compose_logs(path: str, tail: int = 100, host: Optional[str] = None) -> dict:
    """
    Fetch recent logs from all services in a Docker Compose stack.

    Args:
        path: Absolute path to the directory containing compose.yml or docker-compose.yml.
        tail: Number of recent log lines to return per service. Default 100.
        host: Named host from config (defaults to default_host).
    """
    try:
        result = _run(host, f"cd {path} && docker compose logs --tail={tail} 2>&1")
        ok = result["exit_code"] == 0
        lines = result["stdout"].splitlines()
        return {"ok": ok, "host": result["host"], "path": path,
                "stdout": "\n".join(lines[-500:]) if lines else "",
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@_tool
def docker_network_list(host: Optional[str] = None) -> dict:
    """
    List Docker networks on a host.

    Returns a list of networks with ID, name, driver, and scope.
    """
    fmt = '{"ID":"{{.ID}}","Name":"{{.Name}}","Driver":"{{.Driver}}","Scope":"{{.Scope}}"}'
    try:
        result = _run(host, f"docker network ls --format '{fmt}'")
        if result["exit_code"] != 0:
            return {"ok": False, "error": result["stderr"]}
        networks = []
        for line in result["stdout"].splitlines():
            line = line.strip()
            if line:
                try:
                    networks.append(json.loads(line))
                except json.JSONDecodeError:
                    networks.append({"raw": line})
        return {"ok": True, "host": result["host"], "networks": networks, "count": len(networks)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def http_get(url: str, expected_status: Optional[int] = None, host: Optional[str] = None,
             headers: Optional[dict] = None) -> dict:
    """
    Make an HTTP GET request and return the status code and response body.

    By default the request is made from the MCP server itself — useful for
    reaching local-network services (e.g. Loki, Grafana, Prometheus).
    If host is specified, the request is made from that host via SSH using curl.

    Args:
        url: URL to fetch.
        expected_status: If provided, ok=False when the response status doesn't match.
        host: Named host from config. If specified, curl runs on that host via SSH.
        headers: Optional dict of HTTP headers (e.g. {"Authorization": "Bearer tk_..."}).
    """
    try:
        if host:
            header_flags = "".join(f" -H {shlex.quote(k + ': ' + v)}" for k, v in (headers or {}).items())
            result = _run(host, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10{header_flags} {url!r}")
            if result["exit_code"] != 0:
                return {"ok": False, "url": url, "host": result["host"],
                        "error": result["stderr"] or result["stdout"]}
            status_code = int(result["stdout"].strip())
            matched = expected_status is None or status_code == expected_status
            return {"ok": matched, "url": url, "host": result["host"],
                    "status_code": status_code,
                    **({"expected_status": expected_status,
                        "error": f"Expected {expected_status}, got {status_code}"} if not matched else {})}
        else:
            resp = _requests.get(url, timeout=10, verify=False, headers=headers or {})
            status_code = resp.status_code
            matched = expected_status is None or status_code == expected_status
            return {"ok": matched, "url": url, "status_code": status_code,
                    "body": resp.text[:2000],
                    **({"expected_status": expected_status,
                        "error": f"Expected {expected_status}, got {status_code}"} if not matched else {})}
    except ValueError as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "url": url, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — systemctl
# ---------------------------------------------------------------------------


@_tool
def systemctl_status(service: str, host: Optional[str] = None) -> dict:
    """Return the systemctl status of a service on the named host."""
    try:
        return _run(host, f"systemctl status {service} --no-pager")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@_tool
def systemctl_restart(service: str, host: Optional[str] = None) -> dict:
    """Restart a systemd service on the named host."""
    try:
        result = _run(host, f"systemctl restart {service}")
        ok = result["exit_code"] == 0
        return {"ok": ok, "service": service, "host": result["host"],
                **({"error": result["stderr"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "service": service, "error": str(exc)}


@_tool
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


@_tool
def read_file(path: str, host: Optional[str] = None, max_bytes: int = 51200,
              use_sudo: bool = False, offset_bytes: int = 0) -> dict:
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
        offset_bytes: Byte offset to seek to before reading (default 0 = start of file).
                      Use stat_file to get file size, then compute offset from grep line
                      numbers to reach content near the end of large files.
    """
    try:
        _check_secret_path(path)
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
            if offset_bytes:
                f.seek(offset_bytes)
            raw = f.read(max_bytes if max_bytes else -1)
        sftp.close()
        content = raw.decode("utf-8", errors="replace")
        result: dict[str, Any] = {"content": content, "host": host_name, "path": path}
        if offset_bytes:
            result["offset_bytes"] = offset_bytes
        if max_bytes and len(raw) == max_bytes:
            result["truncated"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        return {"content": None, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@_tool
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
        _check_secret_path(path)
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
            stdin, stdout, stderr = client.exec_command(f"sudo tee {path} > /dev/null", timeout=30)
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


@_tool
def patch_file(
    path: str,
    old_string: str,
    new_string: str,
    host: Optional[str] = None,
    replace_all: bool = False,
    use_sudo: bool = False,
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
        use_sudo: If True, read via 'sudo cat' and write via 'sudo tee'. Use for
                  root-owned files the SSH user cannot access directly. Requires
                  passwordless sudo on the target host.
    """
    try:
        _check_secret_path(path)
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "host": host, "path": path}

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

    if use_sudo:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(**connect_kwargs)
            _, stdout_r, stderr_r = client.exec_command(f"sudo cat {path}", timeout=30)
            content = stdout_r.read().decode("utf-8", errors="replace")
            if stdout_r.channel.recv_exit_status() != 0:
                err = stderr_r.read().decode("utf-8", errors="replace").strip()
                return {"ok": False, "error": err, "host": host_name, "path": path}
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
            stdin_w, stdout_w, stderr_w = client.exec_command(f"sudo tee {path} > /dev/null", timeout=30)
            stdin_w.write(new_content.encode("utf-8"))
            stdin_w.channel.shutdown_write()
            exit_code = stdout_w.channel.recv_exit_status()
            err = stderr_w.read().decode("utf-8", errors="replace").strip()
            if exit_code != 0:
                return {"ok": False, "error": err, "host": host_name, "path": path}
            return {"ok": True, "host": host_name, "path": path, "replacements": count if replace_all else 1}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "host": host_name, "path": path}
        finally:
            client.close()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
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


@_tool
def regex_patch_file(
    path: str,
    pattern: str,
    replacement: str,
    host: Optional[str] = None,
    flags: str = "",
    use_sudo: bool = False,
) -> dict:
    """
    Make a targeted regex replacement in a remote file over SSH (SFTP).

    Reads the file, applies re.sub(pattern, replacement, content, flags=...) and
    writes it back. Use this over patch_file when the match spans many lines or
    requires pattern syntax (e.g. removing an entire function block with DOTALL).

    If the pattern matches zero times the tool refuses and returns an error so
    you know the pattern needs adjustment before anything is written.

    Args:
        path: Absolute path to the file on the remote host.
        pattern: Python regex pattern string.
        replacement: Replacement string (supports backreferences like \\1).
        host: Named host from config (defaults to default_host).
        flags: Pipe-separated regex flag names, e.g. "DOTALL" or "DOTALL|MULTILINE".
               Supported: DOTALL, MULTILINE, IGNORECASE, VERBOSE.
        use_sudo: If True, read via 'sudo cat' and write via 'sudo tee'.
    """
    flag_map = {
        "DOTALL": _re.DOTALL,
        "MULTILINE": _re.MULTILINE,
        "IGNORECASE": _re.IGNORECASE,
        "VERBOSE": _re.VERBOSE,
    }
    re_flags = 0
    for name in (f.strip() for f in flags.split("|") if f.strip()):
        if name not in flag_map:
            return {"ok": False, "error": f"Unknown flag: {name!r}. Supported: {list(flag_map)}", "path": path}
        re_flags |= flag_map[name]

    try:
        compiled = _re.compile(pattern, re_flags)
    except _re.error as exc:
        return {"ok": False, "error": f"Invalid regex: {exc}", "path": path}

    try:
        _check_secret_path(path)
        host_name, host_cfg = _resolve_host(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "host": host, "path": path}

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

    def _apply(content: str) -> tuple[str, int]:
        matches = len(compiled.findall(content))
        if matches == 0:
            return content, 0
        return compiled.sub(replacement, content), matches

    if use_sudo:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(**connect_kwargs)
            _, stdout_r, stderr_r = client.exec_command(f"sudo cat {path}", timeout=30)
            content = stdout_r.read().decode("utf-8", errors="replace")
            if stdout_r.channel.recv_exit_status() != 0:
                err = stderr_r.read().decode("utf-8", errors="replace").strip()
                return {"ok": False, "error": err, "host": host_name, "path": path}
            new_content, count = _apply(content)
            if count == 0:
                return {"ok": False, "error": "Pattern matched zero times — nothing written.", "host": host_name, "path": path}
            stdin_w, stdout_w, stderr_w = client.exec_command(f"sudo tee {path} > /dev/null", timeout=30)
            stdin_w.write(new_content.encode("utf-8"))
            stdin_w.channel.shutdown_write()
            exit_code = stdout_w.channel.recv_exit_status()
            err = stderr_w.read().decode("utf-8", errors="replace").strip()
            if exit_code != 0:
                return {"ok": False, "error": err, "host": host_name, "path": path}
            return {"ok": True, "host": host_name, "path": path, "replacements": count}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "host": host_name, "path": path}
        finally:
            client.close()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(**connect_kwargs)
        sftp = client.open_sftp()

        with sftp.file(path, "r") as f:
            content = f.read().decode("utf-8", errors="replace")

        new_content, count = _apply(content)
        if count == 0:
            sftp.close()
            return {"ok": False, "error": "Pattern matched zero times — nothing written.", "host": host_name, "path": path}

        with sftp.file(path, "w") as f:
            f.write(new_content.encode("utf-8"))
        sftp.close()

        return {"ok": True, "host": host_name, "path": path, "replacements": count}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "host": host_name, "path": path}
    finally:
        client.close()


@_tool
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
        _check_secret_path(path)
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


@_tool
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
        _check_secret_path(path)
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


@_tool
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


@_tool
def list_directory(path: str, host: Optional[str] = None, all: bool = True,
                   use_sudo: bool = False) -> dict:
    """
    List the contents of a directory on a remote host with ownership and permission details.

    Returns a structured list of entries with name, type, permissions, owner, group,
    size, and modified time. Use this to check file ownership, find config files, or
    verify directory contents before reading or writing.

    Args:
        path: Absolute path to the directory on the remote host.
        host: Named host from config (defaults to default_host).
        all: Include hidden files (dot-files). Default True.
        use_sudo: If True, run ls via sudo. Use for directories only accessible
                  as root (e.g. /var/lib/docker/volumes/). Requires passwordless
                  sudo on the target host.
    """
    try:
        all_flag = "-la" if all else "-l"
        sudo_prefix = "sudo " if use_sudo else ""
        result = _run(host, f"{sudo_prefix}ls {all_flag} --time-style=long-iso {path} 2>&1")
        ok = result["exit_code"] == 0
        if not ok:
            return {"ok": False, "path": path, "host": result["host"], "error": result["stdout"]}

        entries = []
        for line in result["stdout"].splitlines():
            # Skip "total N" header line
            if line.startswith("total "):
                continue
            parts = line.split(None, 8)
            if len(parts) < 8:
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

        if not entries and use_sudo:
            return {"ok": False, "path": path, "host": result["host"],
                    "error": "sudo ls succeeded but returned no entries — sudo may lack a TTY or the path is empty."}
        return {"ok": True, "path": path, "host": result["host"],
                "entries": entries, "count": len(entries)}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@_tool
def rclone_ls(remote_path: str, host: Optional[str] = None,
              max_depth: int = 1, recursive: bool = False) -> dict:
    """
    List files on an rclone remote, returning structured size+path entries.

    Runs `rclone ls` on the target host via SSH. Output is parsed into a list
    of {"size_bytes": int, "path": str} entries.

    Args:
        remote_path: rclone remote path, e.g. "b2:my-backups/subdir/".
        host: Named host from config (defaults to default_host).
        max_depth: Passed as --max-depth (default 1). Ignored when recursive=True.
        recursive: If True, omit --max-depth and list all files recursively.
    """
    try:
        depth_flag = "" if recursive else f"--max-depth {max_depth}"
        cmd = f"rclone ls {depth_flag} {shlex.quote(remote_path)}".strip()
        result = _run(host, cmd, timeout=60)
        host_name = result["host"]

        if result["exit_code"] != 0:
            stderr = result["stderr"] or result["stdout"]
            if "not found" in stderr.lower() or "command not found" in stderr.lower():
                error = "rclone not found on host"
            else:
                error = stderr or f"rclone exited with code {result['exit_code']}"
            log.warning("rclone_ls failed", extra={
                "event": "rclone_ls", "host": host_name,
                "remote_path": remote_path, "error": error,
            })
            return {"ok": False, "remote_path": remote_path, "host": host_name, "error": error}

        entries = []
        for line in result["stdout"].splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            size_str, path = parts
            if size_str.isdigit():
                entries.append({"size_bytes": int(size_str), "path": path})

        log.info("rclone_ls complete", extra={
            "event": "rclone_ls", "host": host_name,
            "remote_path": remote_path, "count": len(entries),
        })
        return {"ok": True, "remote_path": remote_path, "host": host_name,
                "entries": entries, "count": len(entries)}
    except ValueError as exc:
        return {"ok": False, "remote_path": remote_path, "error": str(exc)}


@_tool
def make_directory(path: str, host: Optional[str] = None, use_sudo: bool = False) -> dict:
    """
    Create a directory (and any missing parents) on a remote host.

    Args:
        path: Absolute path of the directory to create.
        host: Named host from config (defaults to default_host).
        use_sudo: If True, run mkdir via sudo. Required when the parent directory
                  is root-owned (e.g. /srv/, /etc/). Requires passwordless sudo.
    """
    try:
        sudo_prefix = "sudo " if use_sudo else ""
        result = _run(host, f"{sudo_prefix}mkdir -p {path} 2>&1")
        ok = result["exit_code"] == 0
        return {"ok": ok, "path": path, "host": result["host"],
                **({"error": result["stdout"]} if not ok else {})}
    except ValueError as exc:
        return {"ok": False, "path": path, "error": str(exc)}


@_tool
def backup_file(path: str, host: Optional[str] = None, use_sudo: bool = False) -> dict:
    """
    Create a timestamped backup of a file on a remote host before editing it.

    Copies the file to <path>.backup.YYYYMMDD-HHMM in the same directory.
    Run this before patch_file or write_file when editing important config files.

    Args:
        path: Absolute path to the file to back up.
        host: Named host from config (defaults to default_host).
        use_sudo: If True, run cp via sudo. Use for root-owned files the SSH
                  user cannot copy directly (e.g. files in /srv/ or /etc/).
                  Requires passwordless sudo on the target host.
    """
    try:
        sudo_prefix = "sudo " if use_sudo else ""
        result = _run(host, f"{sudo_prefix}cp {path} {path}.backup.$(date +%Y%m%d-%H%M)")
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


@_tool
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


@_tool
def disk_usage(host: Optional[str] = None) -> dict:
    """Return disk usage summary (df -h) for the named host."""
    try:
        return _run(host, "df -h")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


@_tool
def memory_usage(host: Optional[str] = None) -> dict:
    """Return memory usage summary (free -h) for the named host."""
    try:
        return _run(host, "free -h")
    except ValueError as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}


# ---------------------------------------------------------------------------
# Tools — Proxmox (uses REST API)
# ---------------------------------------------------------------------------


@_tool
def proxmox_vm_list(host: str) -> dict:
    """
    List all VMs and containers on a Proxmox node.

    Queries both QEMU VMs and LXC containers and returns a combined list
    sorted by VMID.

    Returns vmid, name, status, uptime, and type ('qemu' or 'lxc') for each.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1') or bare IP address.
    """
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        vms_resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/qemu")
        lxc_resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/lxc")
        vms = [
            {
                "vmid": vm.get("vmid"),
                "name": vm.get("name"),
                "status": vm.get("status"),
                "uptime": vm.get("uptime"),
                "type": "qemu",
            }
            for vm in vms_resp.get("data", [])
        ]
        containers = [
            {
                "vmid": ct.get("vmid"),
                "name": ct.get("name"),
                "status": ct.get("status"),
                "uptime": ct.get("uptime"),
                "type": "lxc",
            }
            for ct in lxc_resp.get("data", [])
        ]
        all_vms = sorted(vms + containers, key=lambda x: x.get("vmid") or 0)
        return {
            "ok": True,
            "node": node,
            "host": node_cfg["host"],
            "vms": all_vms,
            "count": len(all_vms),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_snapshot_list(host: str, vmid: int) -> dict:
    """
    List all snapshots for a Proxmox VM or container.

    Returns snapshot name, description, and creation time (Unix timestamp).
    The 'current' pseudo-snapshot is excluded from results.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1') or bare IP address.
        vmid: VM or container ID.
    """
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        # Try qemu first, fall back to lxc
        vm_type = "qemu"
        try:
            resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/qemu/{vmid}/snapshot")
        except _requests.RequestException:
            vm_type = "lxc"
            resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/lxc/{vmid}/snapshot")
        snapshots = [
            {
                "name": snap.get("name"),
                "description": snap.get("description", ""),
                "snaptime": snap.get("snaptime"),
            }
            for snap in resp.get("data", [])
            if snap.get("name") != "current"
        ]
        return {
            "ok": True,
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "snapshots": snapshots,
            "count": len(snapshots),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_snapshot_create(host: str, vmid: int, snapname: str, description: str = "") -> dict:
    """
    Create a disk-only snapshot for a Proxmox VM or container.

    Waits for the task to complete before returning — polls until status is
    'stopped', then reports success or failure based on exitstatus. This is
    more reliable than pvesh, which is fire-and-forget.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1') or bare IP address.
        vmid: VM or container ID.
        snapname: Name for the new snapshot (no spaces).
        description: Optional description for the snapshot.
    """
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        body: dict[str, Any] = {"snapname": snapname, "vmstate": 0}
        if description:
            body["description"] = description
        # Try qemu first, fall back to lxc
        vm_type = "qemu"
        try:
            resp = _proxmox_api(
                node_cfg, "POST", f"/nodes/{node}/qemu/{vmid}/snapshot", json=body
            )
        except _requests.RequestException:
            vm_type = "lxc"
            lxc_body = {"snapname": snapname}
            if description:
                lxc_body["description"] = description
            resp = _proxmox_api(
                node_cfg, "POST", f"/nodes/{node}/lxc/{vmid}/snapshot", json=lxc_body
            )
        upid = resp.get("data", "")
        task_result = _proxmox_wait_task(node_cfg, upid)
        return {
            "ok": task_result["ok"],
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "snapname": snapname,
            "upid": upid,
            "exitstatus": task_result.get("exitstatus"),
            **({"error": task_result["error"]} if not task_result["ok"] and "error" in task_result else {}),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_snapshot_delete(host: str, vmid: int, snapname: str) -> dict:
    """
    Delete a snapshot from a Proxmox VM or container.

    Waits for the task to complete before returning, so callers know whether
    the delete actually succeeded.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1') or bare IP address.
        vmid: VM or container ID.
        snapname: Name of the snapshot to delete.
    """
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        # Try qemu first, fall back to lxc
        vm_type = "qemu"
        try:
            resp = _proxmox_api(
                node_cfg, "DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{snapname}"
            )
        except _requests.RequestException:
            vm_type = "lxc"
            resp = _proxmox_api(
                node_cfg, "DELETE", f"/nodes/{node}/lxc/{vmid}/snapshot/{snapname}"
            )
        upid = resp.get("data", "")
        task_result = _proxmox_wait_task(node_cfg, upid)
        return {
            "ok": task_result["ok"],
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "snapname": snapname,
            "upid": upid,
            "exitstatus": task_result.get("exitstatus"),
            **({"error": task_result["error"]} if not task_result["ok"] and "error" in task_result else {}),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_task_status(host: str, upid: str) -> dict:
    """
    Poll the current status of a Proxmox task by its UPID.

    Returns the task status, exitstatus (if finished), and whether it is
    still running. Useful for checking tasks independently of snapshot tools.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1') or bare IP address.
        upid: Task UPID string (returned by snapshot_create, snapshot_delete, etc.).
    """
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        encoded_upid = _url_quote(upid, safe="")
        resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/tasks/{encoded_upid}/status")
        data = resp.get("data", {})
        return {
            "ok": True,
            "node": node,
            "upid": upid,
            "status": data.get("status"),
            "exitstatus": data.get("exitstatus"),
            "running": data.get("status") != "stopped",
            "type": data.get("type"),
            "user": data.get("user"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_storage_info(host: str) -> dict:
    """
    Return storage status for all active storage on a Proxmox node.

    Includes name, type, total/used/free space in bytes and GB, and percentage
    used. Particularly useful for monitoring LVM thin pool fill on pve2.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox2') or bare IP address.
    """
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        resp = _proxmox_api(node_cfg, "GET", f"/nodes/{node}/storage")
        storages = []
        for s in resp.get("data", []):
            if not s.get("active"):
                continue
            total = s.get("total", 0)
            used = s.get("used", 0)
            avail = s.get("avail", 0)
            used_pct = round(used / total * 100, 1) if total else 0
            storages.append({
                "name": s.get("storage"),
                "type": s.get("type"),
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": avail,
                "total_gb": round(total / 1_073_741_824, 2),
                "used_gb": round(used / 1_073_741_824, 2),
                "free_gb": round(avail / 1_073_741_824, 2),
                "used_pct": used_pct,
            })
        return {
            "ok": True,
            "node": node,
            "host": node_cfg["host"],
            "storages": storages,
            "count": len(storages),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_vm_start(host: str, vmid: int, confirmed: bool = False) -> dict:
    """
    Start a stopped Proxmox VM or container.

    Requires confirmed=True — only set this after the user has explicitly
    approved starting this VM in the current conversation.

    Waits up to 2 minutes for the start task to complete.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1').
        vmid: VM or container ID.
        confirmed: Must be True to execute. Default False blocks the action.
    """
    if not confirmed:
        return {
            "ok": False,
            "error": "Set confirmed=True after the user explicitly approves starting this VM.",
            "host": host,
            "vmid": vmid,
        }
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        vm_type = "qemu"
        try:
            resp = _proxmox_api(node_cfg, "POST", f"/nodes/{node}/qemu/{vmid}/status/start")
        except _requests.RequestException:
            vm_type = "lxc"
            resp = _proxmox_api(node_cfg, "POST", f"/nodes/{node}/lxc/{vmid}/status/start")
        upid = resp.get("data", "")
        task_result = _proxmox_wait_task(node_cfg, upid, timeout=120)
        return {
            "ok": task_result["ok"],
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "action": "start",
            "upid": upid,
            "exitstatus": task_result.get("exitstatus"),
            **({"error": task_result["error"]} if not task_result["ok"] and "error" in task_result else {}),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def proxmox_vm_stop(host: str, vmid: int, confirmed: bool = False) -> dict:
    """
    Gracefully shut down a running Proxmox VM or container (ACPI shutdown).

    Sends the ACPI shutdown signal so the guest OS shuts down cleanly.
    For a hard power-off, use the Proxmox web UI directly.

    Requires confirmed=True — only set this after the user has explicitly
    approved stopping this VM in the current conversation.

    Waits up to 3 minutes for shutdown to complete.

    Args:
        host: Proxmox node name from config (e.g. 'proxmox1').
        vmid: VM or container ID.
        confirmed: Must be True to execute. Default False blocks the action.
    """
    if not confirmed:
        return {
            "ok": False,
            "error": "Set confirmed=True after the user explicitly approves stopping this VM.",
            "host": host,
            "vmid": vmid,
        }
    try:
        node_cfg = _resolve_proxmox_node(host)
        node = node_cfg["node"]
        vm_type = "qemu"
        try:
            resp = _proxmox_api(node_cfg, "POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown")
        except _requests.RequestException:
            vm_type = "lxc"
            resp = _proxmox_api(node_cfg, "POST", f"/nodes/{node}/lxc/{vmid}/status/shutdown")
        upid = resp.get("data", "")
        task_result = _proxmox_wait_task(node_cfg, upid, timeout=180)
        return {
            "ok": task_result["ok"],
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "action": "shutdown",
            "upid": upid,
            "exitstatus": task_result.get("exitstatus"),
            **({"error": task_result["error"]} if not task_result["ok"] and "error" in task_result else {}),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — BookStack
# Removed 2026-05-29: BookStack decommissioned. Full implementation preserved
# in git at tag: bookstack-final
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Tools — Loki
# ---------------------------------------------------------------------------


def _loki_cfg() -> str:
    """Return Loki base URL from config."""
    url = _load_config().get("loki", {}).get("url", "").rstrip("/")
    if not url:
        raise ValueError("loki config missing — set loki.url in config.yaml")
    return url


@_tool
def loki_query(
    since: str = "1h",
    limit: int = 50,
    container: Optional[str] = None,
    query: Optional[str] = None,
) -> dict:
    """
    Query Loki for logs using LogQL.

    Provide either container (simple label filter) or a raw LogQL query string.
    Results are returned newest-first.

    Args:
        since: How far back to query. Examples: "15m", "1h", "6h", "24h". Default "1h".
        limit: Maximum log lines to return. Default 50.
        container: Container name shorthand — expands to {container="<value>"}.
        query: Raw LogQL query. Overrides container if both provided.
               Example: '{compose_project=~".+"} |= "error"'
    """
    try:
        base_url = _loki_cfg()

        if query:
            logql = query
        elif container:
            logql = f'{{container="{container}"}}'
        else:
            return {"ok": False, "error": "Provide either container or query."}

        unit = since[-1]
        try:
            value = int(since[:-1])
        except ValueError:
            return {"ok": False, "error": f"Invalid since value '{since}'. Examples: 15m, 1h, 6h."}
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if unit not in multipliers:
            return {"ok": False, "error": f"Invalid since unit '{unit}'. Use s, m, h, or d."}
        duration_ns = value * multipliers[unit] * 1_000_000_000

        now_ns = int(time.time() * 1e9)
        resp = _requests.get(
            f"{base_url}/loki/api/v1/query_range",
            params={"query": logql, "start": str(now_ns - duration_ns),
                    "end": str(now_ns), "limit": limit, "direction": "backward"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        lines = []
        for stream in data.get("data", {}).get("result", []):
            for ts, line in stream.get("values", []):
                lines.append({"ts": ts, "line": line})
        lines.sort(key=lambda x: x["ts"], reverse=True)

        return {"ok": True, "query": logql, "since": since,
                "count": len(lines), "lines": lines[:limit]}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — OPNsense (Caddy + DHCP)
# ---------------------------------------------------------------------------

_CF_API_BASE = "https://api.cloudflare.com/client/v4"


def _opnsense_api(method: str, path: str, **kwargs) -> dict:
    """Make an authenticated OPNsense API request. Returns parsed JSON."""
    cfg = _load_config().get("opnsense", {})
    base_url = cfg.get("url", "").rstrip("/")
    api_key = os.environ.get("OPNSENSE_API_KEY", "")
    api_secret = os.environ.get("OPNSENSE_API_SECRET", "")
    if not (base_url and api_key and api_secret):
        raise ValueError(
            "opnsense config missing — set opnsense.url in config.yaml and "
            "OPNSENSE_API_KEY / OPNSENSE_API_SECRET in .env"
        )
    resp = _requests.request(
        method,
        f"{base_url}/api{path}",
        auth=(api_key, api_secret),
        verify=False,
        timeout=30,
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


def _caddy_parse_destination(to_destination: str) -> tuple[str, str, str]:
    """Parse 'http://host:port' into (http_tls, to_domain, to_port)."""
    from urllib.parse import urlparse
    parsed = urlparse(to_destination if "://" in to_destination else f"http://{to_destination}")
    http_tls = "1" if parsed.scheme == "https" else "0"
    to_domain = parsed.hostname or ""
    to_port = str(parsed.port) if parsed.port else ("443" if http_tls == "1" else "80")
    return http_tls, to_domain, to_port


@_tool
def caddy_list_routes() -> dict:
    """
    List all Caddy reverse proxy routes configured on OPNsense.

    Joins the reverse-proxy domain entries with their handle (backend) entries
    to return each route's UUID, from_domain, backend address, enabled state,
    and description.
    """
    try:
        rev_data = _opnsense_api(
            "POST",
            "/caddy/ReverseProxy/searchReverseProxy",
            json={"current": 1, "rowCount": 100, "searchPhrase": ""},
        )
        han_data = _opnsense_api(
            "POST",
            "/caddy/ReverseProxy/searchHandle",
            json={"current": 1, "rowCount": 500, "searchPhrase": ""},
        )
        # Index handles by the reverse UUID they reference
        handles: dict[str, list] = {}
        for h in han_data.get("rows", []):
            rev_uuid = h.get("reverse", "")
            handles.setdefault(rev_uuid, []).append(h)

        routes = []
        for r in rev_data.get("rows", []):
            uuid = r.get("uuid", "")
            hs = handles.get(uuid, [])
            backends = []
            for h in hs:
                proto = "https://" if h.get("HttpTls") == "1" else "http://"
                domain = h.get("ToDomain", "")
                port = h.get("ToPort", "")
                backends.append({
                    "handle_uuid": h.get("uuid"),
                    "backend": f"{proto}{domain}:{port}" if port else f"{proto}{domain}",
                })
            routes.append({
                "uuid": uuid,
                "from_domain": r.get("FromDomain", ""),
                "enabled": r.get("enabled", "1") in (True, "1", 1),
                "description": r.get("description", ""),
                "backends": backends,
            })
        return {"ok": True, "routes": routes, "count": len(routes)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def caddy_add_route(
    from_domain: str,
    to_destination: str,
    description: Optional[str] = None,
) -> dict:
    """
    Add a Caddy reverse proxy route on OPNsense and apply the configuration.

    Creates both the reverse-proxy domain entry and its handle (backend) entry,
    then reconfigures Caddy. Loopback upstreams are rejected.

    Args:
        from_domain: Incoming hostname to match (e.g. "app.pelorus.org").
        to_destination: Backend URL (e.g. "http://192.168.0.10:3000").
        description: Optional human-readable label for this route.
    """
    if _re.search(r"127\.\d+\.\d+\.\d+|localhost|::1", to_destination):
        return {"ok": False, "error": "Loopback upstream addresses are not allowed."}
    try:
        http_tls, to_domain, to_port = _caddy_parse_destination(to_destination)

        # OPNsense os-caddy model key is "reverse" (not "reverseproxy")
        reverse_payload: dict = {
            "reverse": {
                "enabled": "1",
                "FromDomain": from_domain,
                "FromPort": "",
                "accesslist": "",
                "description": description or "",
                "DnsChallenge": "1",
                "DnsChallengeOverrideDomain": "",
                "CustomCertificate": "",
                "AccessLog": "0",
                "DynDns": "0",
                "AcmePassthrough": "",
                "DisableTls": "0",
                "ClientAuthMode": "",
                "ClientAuthTrustPool": "",
            }
        }
        rev = _opnsense_api("POST", "/caddy/ReverseProxy/addReverseProxy", json=reverse_payload)
        if rev.get("result") not in ("saved", "ok"):
            return {
                "ok": False,
                "error": (
                    "OPNsense rejected reverse proxy creation — "
                    f"endpoint=POST /caddy/ReverseProxy/addReverseProxy, "
                    f"fields={list(reverse_payload['reverse'].keys())}, "
                    f"response={rev}"
                ),
            }
        reverse_uuid = rev.get("uuid", "")

        handle_payload: dict = {
            "handle": {
                "reverse": reverse_uuid,
                "enabled": "1",
                "HandleType": "handle",
                "HandleDirective": "reverse_proxy",
                "ToDomain": to_domain,
                "ToPort": to_port,
                "HttpTls": http_tls,
                "description": description or "",
            }
        }
        han = _opnsense_api("POST", "/caddy/ReverseProxy/addHandle", json=handle_payload)
        if han.get("result") not in ("saved", "ok"):
            # Roll back the reverse entry we just created
            _opnsense_api("POST", f"/caddy/ReverseProxy/delReverseProxy/{reverse_uuid}")
            return {
                "ok": False,
                "error": (
                    "OPNsense rejected handle creation (reverse entry rolled back) — "
                    f"endpoint=POST /caddy/ReverseProxy/addHandle, "
                    f"fields={list(handle_payload['handle'].keys())}, "
                    f"response={han}"
                ),
            }

        _opnsense_api("POST", "/caddy/service/reconfigure")
        return {
            "ok": True,
            "from_domain": from_domain,
            "to_destination": to_destination,
            "uuid": reverse_uuid,
            "handle_uuid": han.get("uuid"),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def caddy_remove_route(uuid: str) -> dict:
    """
    Remove a Caddy reverse proxy route by UUID and apply the configuration.

    Deletes both the reverse-proxy domain entry and any associated handle
    (backend) entries, then reconfigures Caddy. Use caddy_list_routes to
    find the UUID.

    Args:
        uuid: UUID of the reverse-proxy entry to remove (from caddy_list_routes).
    """
    try:
        # Find and delete all handles referencing this reverse entry
        han_data = _opnsense_api(
            "POST",
            "/caddy/ReverseProxy/searchHandle",
            json={"current": 1, "rowCount": 500, "searchPhrase": ""},
        )
        for h in han_data.get("rows", []):
            if h.get("reverse") == uuid:
                _opnsense_api("POST", f"/caddy/ReverseProxy/delHandle/{h['uuid']}")

        data = _opnsense_api("POST", f"/caddy/ReverseProxy/delReverseProxy/{uuid}")
        if data.get("result") not in ("deleted", "ok"):
            return {"ok": False, "error": f"Unexpected API response: {data}"}
        _opnsense_api("POST", "/caddy/service/reconfigure")
        return {"ok": True, "uuid": uuid, "deleted": True}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def opnsense_list_dhcp_leases(search: Optional[str] = None) -> dict:
    """
    List active DHCP leases from OPNsense.

    Returns hostname, IP address, MAC address, and lease state for each client.

    Args:
        search: Optional filter string matched against hostname, IP, or MAC.
    """
    try:
        data = _opnsense_api(
            "POST",
            "/dhcpv4/leases/searchLease",
            json={"current": 1, "rowCount": 500, "searchPhrase": search or ""},
        )
        rows = data.get("rows", [])
        leases = [
            {
                "hostname": r.get("hostname", ""),
                "ip": r.get("address", r.get("ip", "")),
                "mac": r.get("mac", ""),
                "state": r.get("state", ""),
                "interface": r.get("if", r.get("interface", "")),
            }
            for r in rows
        ]
        return {"ok": True, "leases": leases, "count": len(leases)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — Cloudflare Tunnel
# ---------------------------------------------------------------------------


def _cf_tunnel_cfg() -> tuple[str, str, dict]:
    """Return (account_id, tunnel_id, headers) for Cloudflare Tunnel API calls."""
    cfg = _load_config().get("cloudflare", {})
    account_id = cfg.get("account_id", "")
    tunnel_id = cfg.get("tunnel_id", "")
    token = os.environ.get("CLOUDFLARE_TUNNEL_API_TOKEN", "")
    if not (account_id and tunnel_id and token):
        raise ValueError(
            "cloudflare tunnel config missing — set cloudflare.account_id and tunnel_id "
            "in config.yaml and CLOUDFLARE_TUNNEL_API_TOKEN in .env"
        )
    return account_id, tunnel_id, {"Authorization": f"Bearer {token}"}


def _cf_access_cfg() -> tuple[str, dict]:
    """Return (account_id, headers) for Cloudflare Access API calls."""
    cfg = _load_config().get("cloudflare", {})
    account_id = cfg.get("account_id", "")
    token = os.environ.get("CLOUDFLARE_ACCESS_API_TOKEN", "")
    if not (account_id and token):
        raise ValueError(
            "cloudflare access config missing — set cloudflare.account_id in config.yaml "
            "and CLOUDFLARE_ACCESS_API_TOKEN in .env"
        )
    return account_id, {"Authorization": f"Bearer {token}"}


def _cf_get_tunnel_config(account_id: str, tunnel_id: str, headers: dict) -> dict:
    """Fetch the full tunnel ingress configuration from Cloudflare."""
    resp = _requests.get(
        f"{_CF_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError(f"Cloudflare API error: {data.get('errors')}")
    return data.get("result", {})


def _cf_put_tunnel_config(account_id: str, tunnel_id: str, headers: dict, config: dict) -> dict:
    """Replace the tunnel ingress configuration on Cloudflare."""
    resp = _requests.put(
        f"{_CF_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        headers={**headers, "Content-Type": "application/json"},
        json={"config": config},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError(f"Cloudflare API error: {data.get('errors')}")
    return data.get("result", {})


@_tool
def cloudflare_list_tunnel_routes() -> dict:
    """
    List all ingress routes configured in the Cloudflare Tunnel.

    Returns each route's public hostname and backend service URL. The
    catch-all rule (no hostname) is excluded from the results.
    """
    try:
        account_id, tunnel_id, headers = _cf_tunnel_cfg()
        result = _cf_get_tunnel_config(account_id, tunnel_id, headers)
        ingress = result.get("config", {}).get("ingress", [])
        routes = [
            {
                "hostname": rule.get("hostname", ""),
                "service": rule.get("service", ""),
                "origin_request": rule.get("originRequest", {}),
            }
            for rule in ingress
            if rule.get("hostname")
        ]
        return {"ok": True, "routes": routes, "count": len(routes)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def cloudflare_add_tunnel_route(
    hostname: str,
    service: str,
    no_tls_verify: bool = False,
    disable_chunked_encoding: bool = False,
) -> dict:
    """
    Add an ingress route to the Cloudflare Tunnel.

    Fetches the current tunnel config, inserts the new route before the catch-all
    rule, and pushes the updated config. Loopback backends are rejected.

    Args:
        hostname: Public hostname to route (e.g. "app.pelorus.org").
        service: Internal backend URL (e.g. "http://192.168.0.10:3000").
        no_tls_verify: Skip TLS verification for the backend. Default False.
        disable_chunked_encoding: Set disableChunkedEncoding in originRequest.
                                  Required for some MCP/SSE backends. Default False.
    """
    if _re.search(r"127\.\d+\.\d+\.\d+|//localhost", service):
        return {"ok": False, "error": "Loopback backend addresses are not allowed."}
    try:
        account_id, tunnel_id, headers = _cf_tunnel_cfg()
        result = _cf_get_tunnel_config(account_id, tunnel_id, headers)
        config = result.get("config", {})
        ingress: list = config.get("ingress", [{"service": "http_status:404"}])

        if any(r.get("hostname") == hostname for r in ingress):
            return {"ok": False, "error": f"Route for '{hostname}' already exists."}

        new_rule: dict[str, Any] = {"hostname": hostname, "service": service}
        origin_req: dict[str, Any] = {}
        if no_tls_verify:
            origin_req["noTLSVerify"] = True
        if disable_chunked_encoding:
            origin_req["disableChunkedEncoding"] = True
        if origin_req:
            new_rule["originRequest"] = origin_req

        # Insert before the catch-all (last entry, which has no hostname)
        if ingress and not ingress[-1].get("hostname"):
            ingress = ingress[:-1] + [new_rule, ingress[-1]]
        else:
            ingress = ingress + [new_rule]

        config["ingress"] = ingress
        _cf_put_tunnel_config(account_id, tunnel_id, headers, config)
        return {"ok": True, "hostname": hostname, "service": service, "tunnel_id": tunnel_id}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def cloudflare_remove_tunnel_route(hostname: str) -> dict:
    """
    Remove an ingress route from the Cloudflare Tunnel by hostname.

    Fetches the current tunnel config, removes the matching route, and pushes
    the updated config. The catch-all rule is never removed.

    Args:
        hostname: Public hostname of the route to remove (e.g. "app.pelorus.org").
    """
    try:
        account_id, tunnel_id, headers = _cf_tunnel_cfg()
        result = _cf_get_tunnel_config(account_id, tunnel_id, headers)
        config = result.get("config", {})
        ingress: list = config.get("ingress", [])

        named = [r for r in ingress if r.get("hostname")]
        filtered = [r for r in ingress if r.get("hostname") != hostname]

        if len(named) == len([r for r in filtered if r.get("hostname")]):
            return {"ok": False, "error": f"No route found for hostname '{hostname}'."}

        config["ingress"] = filtered
        _cf_put_tunnel_config(account_id, tunnel_id, headers, config)
        return {"ok": True, "hostname": hostname, "removed": True, "tunnel_id": tunnel_id}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — Cloudflare Access
# ---------------------------------------------------------------------------


@_tool
def cloudflare_list_access_policies() -> dict:
    """
    List all Cloudflare Access applications and their associated policies.

    Returns each application's name, domain, session duration, and a summary
    of its allow/block policies including which identities are permitted.
    """
    try:
        account_id, headers = _cf_access_cfg()

        resp = _requests.get(
            f"{_CF_API_BASE}/accounts/{account_id}/access/apps",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            return {"ok": False, "error": str(data.get("errors"))}

        apps = []
        for app in data.get("result", []):
            app_id = app.get("id")
            pol_resp = _requests.get(
                f"{_CF_API_BASE}/accounts/{account_id}/access/apps/{app_id}/policies",
                headers=headers,
                timeout=30,
            )
            pol_resp.raise_for_status()
            pol_data = pol_resp.json()
            policies = [
                {
                    "name": p.get("name"),
                    "decision": p.get("decision"),
                    "include": p.get("include", []),
                }
                for p in pol_data.get("result", [])
            ]
            apps.append({
                "id": app_id,
                "name": app.get("name"),
                "domain": app.get("domain"),
                "session_duration": app.get("session_duration"),
                "policies": policies,
            })

        return {"ok": True, "apps": apps, "count": len(apps)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def cloudflare_add_access_policy(
    hostname: str,
    name: str,
    allowed_emails: str,
    session_duration: str = "24h",
) -> dict:
    """
    Create a Cloudflare Access application and allow policy for a hostname.

    Creates the Access Application protecting the hostname, then adds an Allow
    policy restricting access to the specified email addresses. Use the
    Cloudflare dashboard to remove or modify policies — no delete tool is
    provided here by design.

    Args:
        hostname: Hostname to protect (e.g. "app.pelorus.org").
        name: Human-readable name for the application.
        allowed_emails: Comma-separated email addresses to permit (e.g. "you@example.com").
        session_duration: Session length (e.g. "24h", "7d"). Default "24h".
    """
    emails = [e.strip() for e in allowed_emails.split(",") if e.strip()]
    if not emails:
        return {"ok": False, "error": "At least one email address is required."}
    try:
        account_id, headers = _cf_access_cfg()
        json_headers = {**headers, "Content-Type": "application/json"}

        app_resp = _requests.post(
            f"{_CF_API_BASE}/accounts/{account_id}/access/apps",
            headers=json_headers,
            json={
                "name": name,
                "domain": hostname,
                "type": "self_hosted",
                "session_duration": session_duration,
            },
            timeout=30,
        )
        app_resp.raise_for_status()
        app_data = app_resp.json()
        if not app_data.get("success"):
            return {"ok": False, "error": str(app_data.get("errors"))}

        app_id = app_data["result"]["id"]

        pol_resp = _requests.post(
            f"{_CF_API_BASE}/accounts/{account_id}/access/apps/{app_id}/policies",
            headers=json_headers,
            json={
                "name": f"Allow {name}",
                "decision": "allow",
                "include": [{"email": {"email": e}} for e in emails],
            },
            timeout=30,
        )
        pol_resp.raise_for_status()
        pol_data = pol_resp.json()
        if not pol_data.get("success"):
            return {"ok": False, "error": str(pol_data.get("errors"))}

        return {
            "ok": True,
            "app_id": app_id,
            "policy_id": pol_data["result"]["id"],
            "hostname": hostname,
            "name": name,
            "allowed_emails": emails,
            "session_duration": session_duration,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tools — Home Assistant (uses REST API)
# ---------------------------------------------------------------------------


def _ha_request(method: str, path: str, **kwargs) -> _requests.Response:
    """Make an authenticated Home Assistant REST API request.

    Base URL is read from the 'homeassistant' api_services entry in config.yaml;
    the bearer token is read from HA_TOKEN in the environment (never config).
    Follows the same direct-HTTP pattern as the Proxmox tools rather than the
    homelab_api_* proxy. Returns the raw requests.Response (caller parses).
    """
    svc = _load_config().get("api_services", {}).get("homeassistant", {})
    base_url = svc.get("base_url", "").rstrip("/")
    if not base_url:
        raise ValueError(
            "homeassistant.base_url not configured under api_services in config.yaml"
        )
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        raise ValueError(
            "HA_TOKEN is not set in the environment. "
            "Add it to .env on docker-server and restart the container."
        )
    headers = {"Authorization": f"Bearer {token}"}
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
    resp = _requests.request(method, f"{base_url}{path}", headers=headers, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp


@_tool
def ha_get_states(domain: Optional[str] = None) -> dict:
    """
    Fetch entity states from Home Assistant, optionally filtered to one domain.

    Returns a list of {entity_id, state, attributes, last_changed} for every
    entity (or only those whose entity_id starts with "<domain>." when domain
    is given).

    Args:
        domain: Optional domain prefix to filter by, e.g. "light", "switch",
                "timer", "sensor", "climate". Omit to return all entities.
    """
    try:
        resp = _ha_request("GET", "/states")
        states = resp.json()
        if domain:
            prefix = f"{domain}."
            states = [s for s in states if str(s.get("entity_id", "")).startswith(prefix)]
        entities = [
            {
                "entity_id": s.get("entity_id"),
                "state": s.get("state"),
                "attributes": s.get("attributes", {}),
                "last_changed": s.get("last_changed"),
            }
            for s in states
        ]
        return {"ok": True, "count": len(entities), "entities": entities}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def ha_get_state(entity_id: str) -> dict:
    """
    Fetch the full state of a single Home Assistant entity, including attributes.

    Args:
        entity_id: Full entity id, e.g. "light.kitchen" or "timer.laundry".
    """
    try:
        resp = _ha_request("GET", f"/states/{_url_quote(entity_id)}")
        return {"ok": True, "state": resp.json()}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "entity_id": entity_id}


@_tool
def ha_call_service(
    domain: str,
    service: str,
    data: Optional[dict] = None,
    confirmed: bool = False,
) -> dict:
    """
    Call any Home Assistant service — the universal control tool.

    Use this to turn lights/switches on or off, start/cancel timers, set climate,
    run scripts, etc. The (domain, service) pair maps to POST /services/<domain>/<service>
    and `data` is the service-call payload (e.g. {"entity_id": "light.kitchen"}).

    Requires confirmed=True — only set this after the user has explicitly approved
    this specific action in the current conversation. If confirmed=False (default)
    no request is made.

    Args:
        domain: Service domain, e.g. "light", "switch", "timer", "climate", "script".
        service: Service name, e.g. "turn_on", "turn_off", "start", "set_temperature".
        data: Service data dict, typically including "entity_id". Optional.
        confirmed: Must be True to execute. Default False blocks the action.
    """
    if not confirmed:
        return {
            "ok": False,
            "error": (
                "Set confirmed=True only after the user explicitly approves this "
                "Home Assistant service call in the current conversation."
            ),
            "domain": domain,
            "service": service,
        }
    try:
        resp = _ha_request("POST", f"/services/{domain}/{service}", json=data or {})
        try:
            result = resp.json()
        except ValueError:
            result = resp.text
        return {"ok": True, "domain": domain, "service": service, "result": result}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "domain": domain, "service": service}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "domain": domain, "service": service}


@_tool
def ha_render_template(template: str) -> dict:
    """
    Render a Home Assistant Jinja2 template and return the resulting string.

    Useful for computed values and formatting, e.g.
    "{{ states('sensor.outside_temp') }}" or
    "{{ state_attr('climate.living_room', 'temperature') }}".

    Args:
        template: A Home Assistant Jinja2 template string.
    """
    try:
        resp = _ha_request("POST", "/template", json={"template": template})
        return {"ok": True, "result": resp.text}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def ha_get_history(entity_id: str, hours: int = 24) -> dict:
    """
    Return state-change history for one entity over the last N hours.

    Attributes are trimmed from the results (minimal_response + no_attributes)
    to keep responses small — each entry is just {state, last_changed}.

    Args:
        entity_id: Full entity id, e.g. "sensor.outside_temp".
        hours: How many hours of history to fetch (default 24).
    """
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=max(int(hours), 1))
        params = {
            "filter_entity_id": entity_id,
            "minimal_response": "true",
            "no_attributes": "true",
        }
        resp = _ha_request(
            "GET", f"/history/period/{_url_quote(start.isoformat())}", params=params
        )
        data = resp.json()
        # Response is a list of per-entity series; flatten to a single change list.
        changes = []
        for series in data:
            for entry in series:
                changes.append({
                    "state": entry.get("state"),
                    "last_changed": entry.get("last_changed") or entry.get("last_updated"),
                })
        return {
            "ok": True,
            "entity_id": entity_id,
            "hours": hours,
            "count": len(changes),
            "history": changes,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "entity_id": entity_id}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "entity_id": entity_id}


@_tool
def ha_get_logbook(hours: int = 4) -> dict:
    """
    Return recent Home Assistant logbook entries — what happened and when.

    Results are capped at 200 entries to keep responses manageable.

    Args:
        hours: How many hours back to fetch (default 4).
    """
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=max(int(hours), 1))
        resp = _ha_request("GET", f"/logbook/{_url_quote(start.isoformat())}")
        data = resp.json()
        entries = [
            {
                "when": e.get("when"),
                "name": e.get("name"),
                "message": e.get("message"),
                "entity_id": e.get("entity_id"),
                "domain": e.get("domain"),
            }
            for e in data
        ]
        capped = entries[-200:]
        return {
            "ok": True,
            "hours": hours,
            "count": len(capped),
            "total": len(entries),
            "entries": capped,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def ha_list_automations() -> dict:
    """
    List all Home Assistant automations with their on/off state and friendly name.

    Returns entity_id, friendly_name, state ("on"/"off"), and last_triggered for
    every automation.* entity.
    """
    try:
        resp = _ha_request("GET", "/states")
        states = resp.json()
        automations = [
            {
                "entity_id": s.get("entity_id"),
                "friendly_name": s.get("attributes", {}).get("friendly_name"),
                "state": s.get("state"),
                "last_triggered": s.get("attributes", {}).get("last_triggered"),
            }
            for s in states
            if str(s.get("entity_id", "")).startswith("automation.")
        ]
        return {"ok": True, "count": len(automations), "automations": automations}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def ha_trigger_automation(automation_id: str, confirmed: bool = False) -> dict:
    """
    Trigger a Home Assistant automation by entity id (runs its actions now).

    Calls POST /services/automation/trigger with the given entity_id. Requires
    confirmed=True — only set this after the user has explicitly approved
    triggering this automation in the current conversation.

    Args:
        automation_id: Automation entity id, e.g. "automation.morning_lights".
        confirmed: Must be True to execute. Default False blocks the action.
    """
    if not confirmed:
        return {
            "ok": False,
            "error": (
                "Set confirmed=True only after the user explicitly approves "
                "triggering this automation."
            ),
            "automation_id": automation_id,
        }
    try:
        resp = _ha_request(
            "POST", "/services/automation/trigger", json={"entity_id": automation_id}
        )
        try:
            result = resp.json()
        except ValueError:
            result = resp.text
        return {"ok": True, "automation_id": automation_id, "result": result}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "automation_id": automation_id}
    except _requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "automation_id": automation_id}


# ---------------------------------------------------------------------------
# Tools — Homelab API proxy
# ---------------------------------------------------------------------------


def _api_svc_cfg(service: str) -> dict:
    """Resolve config for a named api_service. Raises ValueError on misconfiguration."""
    services = _load_config().get("api_services", {})
    if service not in services:
        available = sorted(services.keys())
        raise ValueError(f"Unknown service '{service}'. Available: {available}")
    cfg = dict(services[service])
    style = cfg.get("auth_style", "header")
    if style == "basic":
        user_env = cfg.get("auth_env_user", "")
        pass_env = cfg.get("auth_env_pass", "")
        user = os.environ.get(user_env, "") if user_env else ""
        password = os.environ.get(pass_env, "") if pass_env else ""
        missing = [e for e, v in ((user_env, user), (pass_env, password)) if not v]
        if missing:
            raise ValueError(
                f"Credential env var(s) {missing} not set for service '{service}'. "
                f"Add them to .env on docker-server and restart the container."
            )
        cfg["_token"] = f"{user}:{password}"
    else:
        auth_env = cfg.get("auth_env", "")
        token = os.environ.get(auth_env, "") if auth_env else ""
        if not token:
            raise ValueError(
                f"Credential env var '{auth_env}' is not set for service '{service}'. "
                f"Add it to .env on docker-server and restart the container."
            )
        cfg["_token"] = token
    return cfg


def _api_build_request(cfg: dict, path: str, params: dict | None) -> tuple[str, dict, dict]:
    """Return (url, headers, params_dict) with auth injected."""
    base_url = cfg["base_url"].rstrip("/")
    url = base_url + path
    headers: dict = {}
    params = dict(params or {})
    style = cfg.get("auth_style", "header")
    token = cfg["_token"]

    if style == "header":
        headers[cfg["auth_header"]] = token
    elif style == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif style == "token":
        headers["Authorization"] = f"Token {token}"
    elif style == "query_param":
        params[cfg["auth_param"]] = token
    elif style == "basic":
        # token is "user:password" — split on first colon so passwords with colons work
        encoded = base64.b64encode(token.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    elif style == "googlelogin":
        headers["Authorization"] = f"GoogleLogin auth={token}"

    return url, headers, params


@_tool
def homelab_api_get(service: str, path: str, params: Optional[dict] = None) -> dict:
    """
    Proxy a GET request to a homelab service without exposing credentials.

    ALWAYS use this tool instead of curl or http_get when reading data from any
    configured homelab service (Radarr, Sonarr, Sonarr4K, Radarr4K, Jellyfin,
    SABnzbd, Tautulli, Seerr, Grafana, InfluxDB, Shlink, Ntfy, N8N, Karakeep,
    Changedetection). Credentials are resolved server-side and never appear in
    Claude's context.

    If this tool returns an error, report it — do NOT fall back to curl or http_get.

    Args:
        service: Service name from api_services config (e.g. "radarr", "jellyfin").
        path: API path starting with / (e.g. "/movie", "/api/v2").
        params: Optional query parameters as a dict.
    """
    try:
        cfg = _api_svc_cfg(service)
        url, headers, resolved_params = _api_build_request(cfg, path, params)
        resp = _requests.get(url, headers=headers, params=resolved_params, timeout=15, verify=False)
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return {"ok": resp.ok, "status": resp.status_code, "data": data}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Request failed: {exc}"}


@_tool
def homelab_api_post(service: str, path: str, body: Optional[dict] = None) -> dict:
    """
    Proxy a POST request to a homelab service without exposing credentials.

    ALWAYS use this tool instead of curl when POSTing to configured homelab services.
    Only paths listed in the service's post_allowlist are permitted — the tool will
    return a clear error for any unlisted path.

    If this tool returns an error, report it — do NOT fall back to curl.

    Args:
        service: Service name from api_services config.
        path: API path starting with /. Must match a post_allowlist prefix.
        body: Optional JSON request body as a dict.
    """
    try:
        cfg = _api_svc_cfg(service)
        allowlist: list[str] = cfg.get("post_allowlist", [])
        if not allowlist:
            return {
                "ok": False,
                "error": f"POST is not configured for service '{service}'. No post_allowlist defined.",
            }
        if not any(
            path == entry or path.startswith(entry.rstrip("/") + "/") for entry in allowlist
        ):
            return {
                "ok": False,
                "error": (
                    f"POST to '{path}' is not permitted for '{service}'. "
                    f"Allowed prefixes: {allowlist}"
                ),
            }
        url, headers, resolved_params = _api_build_request(cfg, path, {})
        headers["Content-Type"] = "application/json"
        resp = _requests.post(
            url, headers=headers, params=resolved_params, json=body or {}, timeout=15, verify=False
        )
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return {"ok": resp.ok, "status": resp.status_code, "data": data}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Request failed: {exc}"}


@_tool
def homelab_api_mutate(
    service: str,
    method: str,
    path: str,
    body: Optional[dict] = None,
    confirmed: bool = False,
) -> dict:
    """
    Proxy a PUT, PATCH, or DELETE request to a homelab service.

    Only call this tool when the user has explicitly confirmed the specific operation
    in their current message. Do NOT set confirmed=True based on general task context
    or inferred intent — if there is any ambiguity, ask first.

    If confirmed=False (the default), this tool returns an error without making any
    HTTP request. Ask the user to confirm, then call again with confirmed=True.

    Args:
        service: Service name from api_services config.
        method: HTTP method — "PUT", "PATCH", or "DELETE".
        path: API path starting with /.
        body: Optional JSON request body (ignored for DELETE).
        confirmed: Set True only after the user has explicitly confirmed this specific
                   destructive operation in the current conversation.
    """
    if not confirmed:
        return {
            "ok": False,
            "error": (
                "confirmed=False. Describe the operation to the user and ask them to "
                "explicitly confirm before calling this tool with confirmed=True."
            ),
        }
    try:
        method = method.upper()
        if method not in ("PUT", "PATCH", "DELETE"):
            return {"ok": False, "error": f"Method '{method}' not permitted. Use PUT, PATCH, or DELETE."}
        cfg = _api_svc_cfg(service)
        url, headers, resolved_params = _api_build_request(cfg, path, {})
        headers["Content-Type"] = "application/json"
        if method == "DELETE":
            resp = _requests.delete(url, headers=headers, params=resolved_params, timeout=15, verify=False)
        elif method == "PUT":
            resp = _requests.put(
                url, headers=headers, params=resolved_params, json=body or {}, timeout=15, verify=False
            )
        else:
            resp = _requests.patch(
                url, headers=headers, params=resolved_params, json=body or {}, timeout=15, verify=False
            )
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return {"ok": resp.ok, "status": resp.status_code, "data": data}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Request failed: {exc}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if path.startswith("/.well-known/oauth-"):
                return JSONResponse({"error": "Not Found"}, status_code=404)
            # Tell OAuth clients that dynamic registration is not supported.
            if path == "/register":
                return JSONResponse(
                    {"error": "invalid_client_metadata",
                     "error_description": "Dynamic client registration is not supported. Use a pre-configured bearer token."},
                    status_code=400,
                )

            token = os.environ.get("MCP_AUTH_TOKEN")
            if token:
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {token}":
                    return JSONResponse(
                        {"error": "Unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="MCP"'},
                    )
            return await call_next(request)

    class StripAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            log.debug("http request", extra={"method": request.method, "path": request.url.path})
            scope = request.scope
            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", [])
                if k.lower() != b"authorization"
            ]
            return await call_next(request)

    # Do not advertise OAuth. Claude Code should use the configured bearer header.
    from contextlib import asynccontextmanager

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            yield

    app = Starlette(
        routes=[Mount("/", app=mcp_app)],
        lifespan=lifespan
    )
    app.add_middleware(StripAuthMiddleware)
    app.add_middleware(BearerAuthMiddleware)

    log.info("server starting", extra={
        "host": HOST, "port": PORT, "transport": "streamable_http",
        "tool_filter": sorted(_ENABLED_TOOLS) if _ENABLED_TOOLS is not None else "all",
    })
    uvicorn.run(app, host=HOST, port=PORT)