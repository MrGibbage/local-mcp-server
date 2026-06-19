<h1 align="center">Homelab MCP Server</h1>

<p align="center">
  <em>Let Claude operate your entire homelab — without ever seeing a credential.</em>
</p>

<p align="center">
  <a href="https://modelcontextprotocol.io/"><img alt="MCP" src="https://img.shields.io/badge/Model_Context_Protocol-server-7C3AED"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white">
  <img alt="Transport" src="https://img.shields.io/badge/transport-Streamable_HTTP-0EA5E9">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
  <img alt="Last commit" src="https://img.shields.io/github/last-commit/MrGibbage/local-mcp-server">
</p>

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io/) server
that gives an AI assistant a safe, structured way to run a real multi-host
homelab — Docker, Proxmox, systemd, files over SSH, Home Assistant, Cloudflare,
OPNsense, and any HTTP API service you configure.

It ships **64 tools** today, but the headline isn't the count. It's the
**security model**: the assistant operates everything through server-side proxy
tools, so API keys, SSH private keys, and tokens **never enter its context**.

---

## Why this server

Most "let an LLM manage my infra" setups hand the model a credential and let it
build `curl` commands. Every key then lives in the model's context, gets logged,
and can leak into a transcript. This server is built the opposite way:

- **Credentials stay server-side.** The model calls
  `homelab_api_get("radarr", "/system/status")`; the server attaches the key
  from an environment variable and returns only the response. The key is never
  seen. → [Security model](docs/security.md)
- **Config-driven, not hard-wired.** Hosts, services, allowlists, and tool sets
  are all defined in `config.yaml` and environment variables. Point it at your
  own homelab and the same tools operate it — no code changes.
  → [Configuration](docs/configuration.md)
- **Capability tracks reachability.** The same image runs at three trust levels:
  a full-power LAN-only instance, a read-only token-gated instance for public
  access, and a read-only LAN instance for untrusted clients — all from
  environment variables. → [Deployment profiles](docs/configuration.md#deployment-profiles)
- **Writes are opt-in.** Everything is read-biased by default; mutations are
  gated by per-path allowlists and an explicit confirmation flag.

## What it can do

A few representative tools — see the [full catalog](docs/tools.md) for all 64:

```text
homelab_api_get("sonarr", "/series")        # call any configured API, key-safe
docker_ps(host="docker-server")             # manage ~40 containers
proxmox_snapshot_create(vmid=110)           # snapshot a VM before a change
ha_get_state("sensor.living_room_temp")     # read Home Assistant
loki_query('{container="caddy"} |= "404"')  # query logs with LogQL
read_file("/etc/caddy/Caddyfile", host="nas")
```

Tool families: **Host/Shell · Docker · Systemd · Files (SFTP) · Proxmox ·
Home Assistant · Caddy/OPNsense · Cloudflare · Loki · HTTP API proxy.**

## Tech stack

**Python 3.12** · **[FastMCP](https://github.com/modelcontextprotocol/python-sdk)**
for tool registration · **Streamable HTTP** (stateless) transport on
**Uvicorn + Starlette** with custom auth middleware · **Paramiko** for SSH/SFTP ·
**Requests** for service APIs · packaged with **Docker Compose**, running
non-root with read-only secret mounts.

The whole server is one `server.py`; a tool is just a decorated Python function.
→ [Architecture & design](docs/architecture.md)

## The homelab it drives

Built to run a real environment: a **Proxmox** cluster, an **OPNsense**
router with a Caddy reverse-proxy plugin, a **Docker** host of ~40 containers, a
**NAS**, **Home Assistant**, **Grafana/Loki/InfluxDB** observability, and
**Cloudflare Tunnel + Zero Trust Access** for the few services that face the
internet. Each host is one block in `config.yaml`.
→ [More on the homelab](docs/architecture.md#the-homelab-it-drives)

## Quick start

**Requirements:** Docker + Docker Compose on the host that will run the server,
and SSH key access to each machine you want to manage.

```bash
git clone https://github.com/MrGibbage/local-mcp-server
cd local-mcp-server

cp config.example.yaml config.yaml      # define your hosts + services
mkdir -p keys && cp ~/.ssh/id_rsa keys/ # SSH keys, mounted read-only

docker compose up -d
```

Connect a client (Claude Code shown; the server speaks Streamable HTTP at `/mcp`):

```bash
claude mcp add homelab --transport http http://<server-ip>:8090/mcp
```

Full walkthrough — keys, env vars, public access via Cloudflare Tunnel — is in
[`docs/configuration.md`](docs/configuration.md) and
[`deployment.md`](deployment.md).

## Documentation

| Page | What's inside |
|---|---|
| [Architecture & design](docs/architecture.md) | Tech stack, design rationale, the homelab it drives |
| [Configuration](docs/configuration.md) | `config.yaml`, the API proxy, allowlists, deployment profiles |
| [Security model](docs/security.md) | How credentials stay out of the model's context |
| [Tool catalog](docs/tools.md) | All 64 tools, grouped |
| [Deployment](deployment.md) | Public access over Cloudflare Tunnel |

## License

[MIT](LICENSE) — © Skip Morrow
</content>
