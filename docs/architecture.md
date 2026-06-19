# Architecture & Design

This document covers the tech stack, the design decisions behind the server, and
the kind of homelab it was built to operate.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | Fast to iterate on, first-class MCP SDK support |
| MCP framework | **[FastMCP](https://github.com/modelcontextprotocol/python-sdk)** (`mcp[cli]`) | Decorator-based tool registration, generates the JSON-RPC schema from type hints and docstrings |
| Transport | **Streamable HTTP** (stateless) | Works over a single port, survives reconnects, and is reachable by both local and cloud-brokered MCP clients |
| HTTP server | **Uvicorn + Starlette** | ASGI stack; custom middleware adds bearer auth and header stripping |
| Remote execution | **[Paramiko](https://www.paramiko.org/)** | Pure-Python SSH/SFTP — no shelling out to a system `ssh` binary |
| Service APIs | **Requests** | The homelab API proxy and Home Assistant / Cloudflare / OPNsense integrations |
| Config | **PyYAML** | Single hot-reloadable `config.yaml` |
| Packaging | **Docker + Compose** | Reproducible, runs as a non-root user, mounts secrets read-only |

The entire server is a single `server.py`. Tools are plain Python functions
decorated with `@_tool`; FastMCP turns each function's signature and docstring
into the schema the model sees. Adding a tool is adding a function.

## Why a server-side proxy instead of giving Claude the credentials

The central design idea: **the model should be able to operate the homelab
without ever seeing a secret.**

A naive setup hands the model an API key and lets it build `curl` commands.
Every key then lives in the model's context, gets logged, and can leak into a
transcript. This server inverts that:

- The model calls a tool like `homelab_api_get("radarr", "/system/status")`.
- The server looks up the service in `config.yaml`, reads the API key from an
  **environment variable**, attaches it server-side, and makes the request.
- The model receives only the response body. The key never enters its context.

The same pattern covers SSH (keys are mounted read-only and referenced by path,
never content), Home Assistant, Cloudflare, OPNsense, and every other
integration. See [security.md](security.md) for the full model.

## Stateless transport

The server runs FastMCP with `stateless_http=True`, which drops the
`Mcp-Session-Id` handshake. This was a deliberate interop fix: some MCP clients
(notably `mark3labs/mcp-go`, used by third-party chat integrations) treat the
session handshake as a hard failure. Stateless mode keeps the server compatible
with the broadest set of clients while costing nothing for the well-behaved ones.

## Three deployment profiles from one image

The same image is run as up to three containers with different trust levels,
selected entirely through environment variables — no code branches. See
[configuration.md](configuration.md#deployment-profiles) for the details.

| Port | Trust | Auth | Tools |
|---|---|---|---|
| 8090 | Trusted LAN | none (LAN trust) | all 64 |
| 8091 | Public via Cloudflare | **bearer token required** | read-only subset |
| 8092 | Untrusted LAN clients | none | read-only subset + whitelisted light control |

The read-only subset is just an `MCP_ENABLED_TOOLS` allowlist passed to the
container. The full-power instance never leaves the LAN.

## The homelab it drives

This server was built to run a real, multi-host homelab:

- **Proxmox** virtualization cluster — VMs and LXC containers, snapshots managed
  through the API.
- **OPNsense** router/firewall — DHCP, plus a Caddy reverse-proxy plugin driven
  through its API.
- **Docker host** running ~40 containers (media automation, monitoring,
  dashboards, utilities).
- **NAS** for bulk storage, reached over SSH/SFTP.
- **Home Assistant** for home automation — states, history, templates,
  automations, and scoped light control.
- **Cloudflare Tunnel + Zero Trust Access** for the handful of services that are
  safely reachable from outside.
- **Grafana / Loki / InfluxDB** for metrics and logs — Loki is queryable
  directly through a tool.

Each host is one block in `config.yaml`. Nothing about the toolset is hard-wired
to these specific machines — point the config at your own hosts and the same
tools operate them.
