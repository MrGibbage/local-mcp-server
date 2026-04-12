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


# ---------------------------------------------------------------------------
# Tools — File I/O
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(path: str, host: Optional[str] = None, max_bytes: int = 51200) -> dict:
    """
    Read the contents of a file on a remote host over SSH (SFTP).

    Returns the file content as a string, or an error message.

    Args:
        path: Absolute path to the file on the remote host.
        host: Named host from config (defaults to default_host).
        max_bytes: Maximum bytes to read (default 50 KB). Use 0 for unlimited.
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
        return {"success": True, "host": host_name, "path": path}
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
def bookstack_update_page(page_id: int, markdown: str) -> dict:
    """
    Update an existing BookStack page with new Markdown content.

    Sends the content as the markdown field, which sets the page to Markdown editor mode.
    This will also convert an HTML page to Markdown if one is updated this way.

    Returns ok, id, name, and url on success.

    Args:
        page_id: Numeric BookStack page ID.
        markdown: Full Markdown content to replace the page body with.
    """
    try:
        base_url, headers = _bs_cfg()
        resp = _requests.put(
            f"{base_url}/api/pages/{page_id}",
            headers=headers,
            json={"markdown": markdown},
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

    async def oauth_protected_resource(request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": BASE_URL,
            "authorization_servers": [BASE_URL],
        })

    async def oauth_authorization_server(request: Request) -> JSONResponse:
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