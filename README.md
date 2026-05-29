# Homelab MCP Server

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Claude manage your homelab over SSH. Runs as a Docker container on any LAN server, exposes tools via SSE transport.

## Tools

### Host & Shell

| Tool | Description |
|---|---|
| `list_hosts` | List all configured hosts |
| `ssh_exec` | Run an arbitrary shell command on a host |
| `disk_usage` | `df -h` summary |
| `memory_usage` | `free -h` summary |
| `http_get` | Make an HTTP GET request and return status + body |

### Docker

| Tool | Description |
|---|---|
| `docker_ps` | List running containers |
| `docker_logs` | Fetch recent container logs |
| `docker_restart` | Restart a container |
| `docker_stop` / `docker_start` | Stop or start a container |
| `docker_pull` | Pull an image |
| `docker_inspect` | Inspect a container's configuration and runtime state |
| `docker_exec` | Run a command inside a running container |
| `docker_capabilities` | Return decoded Linux capabilities for a container |
| `docker_stats` | One-shot resource usage snapshot for a container |
| `docker_compose_up` / `docker_compose_down` | Manage Compose stacks |
| `docker_compose_logs` | Fetch logs from all services in a Compose stack |
| `docker_network_list` | List Docker networks |

### Systemd

| Tool | Description |
|---|---|
| `systemctl_status` | Return the status of a systemd service |
| `systemctl_restart` | Restart a systemd service |
| `systemctl_list` | List systemd units with optional state filter |

### Files

| Tool | Description |
|---|---|
| `read_file` | Read a remote file over SFTP |
| `write_file` | Write/overwrite a remote file over SFTP |
| `patch_file` | Targeted string replacement in a remote file |
| `regex_patch_file` | Targeted regex replacement in a remote file |
| `tail_file` | Return the last N lines of a remote file |
| `grep_file` | Search for a pattern in a remote file |
| `stat_file` | Return metadata for a file or directory |
| `list_directory` | List directory contents with ownership and permissions |
| `make_directory` | Create a directory (and missing parents) on a remote host |
| `backup_file` | Create a timestamped backup of a file before editing |
| `validate_config` | Validate a YAML or JSON config file without restarting |
| `rclone_ls` | List files on an rclone remote |

### Proxmox

| Tool | Description |
|---|---|
| `proxmox_vm_list` | List all VMs and containers on a Proxmox node |
| `proxmox_snapshot_list` | List snapshots for a VM or container |
| `proxmox_snapshot_create` | Create a disk-only snapshot |
| `proxmox_snapshot_delete` | Delete a snapshot |
| `proxmox_task_status` | Poll the status of a Proxmox task by UPID |
| `proxmox_storage_info` | Return storage status for all active storage on a node |

### Loki

| Tool | Description |
|---|---|
| `loki_query` | Query Loki for logs using LogQL |

### Caddy / OPNsense

| Tool | Description |
|---|---|
| `caddy_list_routes` | List all Caddy reverse proxy routes on OPNsense |
| `caddy_add_route` | Add a Caddy reverse proxy route and apply config |
| `caddy_remove_route` | Remove a Caddy reverse proxy route by UUID |
| `opnsense_list_dhcp_leases` | List active DHCP leases from OPNsense |

### Cloudflare

| Tool | Description |
|---|---|
| `cloudflare_list_tunnel_routes` | List all Cloudflare Tunnel ingress routes |
| `cloudflare_add_tunnel_route` | Add an ingress route to the Cloudflare Tunnel |
| `cloudflare_remove_tunnel_route` | Remove a tunnel route by hostname |
| `cloudflare_list_access_policies` | List Access applications and their policies |
| `cloudflare_add_access_policy` | Create an Access application and allow policy |

## Prerequisites

- Docker + Docker Compose on the server where you'll run this
- SSH key-based access to each homelab host you want to manage

## Quick Start

### 1. Clone the repo

```bash
git clone <your-repo-url> homelab-mcp
cd homelab-mcp
```

### 2. Copy and edit the config

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your hosts, IPs, usernames
```

### 3. Set up SSH keys

Create a `keys/` directory and copy in the private keys referenced in `config.yaml`:

```bash
mkdir -p keys
cp ~/.ssh/id_rsa keys/id_rsa
chmod 600 keys/id_rsa
```

The `keys/` directory is mounted read-only into the container at `/keys/`. Key paths in `config.yaml` should use the `/keys/` prefix — for example `/keys/id_rsa`.

Make sure the corresponding public key is in `~/.ssh/authorized_keys` on each target host.

### 4. Build and start

```bash
docker compose up -d
```

The server listens on `http://<server-ip>:8080` by default.

### 5. Verify it's running

```bash
curl http://localhost:8080/sse
# Should return an SSE stream header
```

Or check logs:

```bash
docker compose logs -f
```

## Configuration

`config.yaml` is the only file you need to edit. It is gitignored — secrets never leave your machine.

```yaml
server:
  host: "0.0.0.0"
  port: 8080

default_host: docker-server   # used when no host is specified in a tool call

hosts:
  docker-server:
    hostname: 192.168.0.231
    user: skip
    key_path: /keys/id_rsa    # path inside the container
    # port: 22                # optional

  nas:
    hostname: 192.168.0.2
    user: skip
    key_path: /keys/id_rsa

# Optional allowlist — restricts ssh_exec to these base commands only.
# Omit or set to null to allow everything.
# ssh_command_allowlist:
#   - docker
#   - systemctl
#   - df
#   - free
```

### Changing the port

Edit `docker-compose.yml`:
```yaml
ports:
  - "9090:8080"   # host port 9090 → container port 8080
```

Or change the container port too by editing `config.yaml` `server.port` and updating both sides of the mapping.

## Connecting to Claude

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "homelab": {
      "url": "http://192.168.0.231:8080/sse"
    }
  }
}
```

Replace `192.168.0.231` with the IP of the server running this container.

### Claude Code (CLI)

```bash
claude mcp add homelab --transport sse http://192.168.0.231:8080/sse
```

Or add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "homelab": {
      "type": "sse",
      "url": "http://192.168.0.231:8080/sse"
    }
  }
}
```

## Security Notes

**This server is designed for LAN use only.**

- Do not expose port 8080 to the internet. Bind your firewall rule to the LAN interface only.
- The `ssh_exec` tool can run arbitrary shell commands on your hosts. Use `ssh_command_allowlist` in `config.yaml` to restrict what commands are permitted if you want belt-and-suspenders protection.
- SSH keys are mounted read-only. The container runs as a non-root user (`mcp`).
- `write_file` overwrites files in-place with no backup. Read first if you want to preserve the original.
- Consider putting this behind a reverse proxy with basic auth if your LAN has untrusted devices on it.

## Development

Run locally without Docker:

```bash
python -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml with real hosts, or test with localhost
python server.py
```

The server will start and print `Starting Homelab MCP server on 0.0.0.0:8080 (SSE transport)`.
