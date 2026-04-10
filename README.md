# Homelab MCP Server

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Claude manage your homelab over SSH. Runs as a Docker container on any LAN server, exposes tools via SSE transport.

## Tools

| Tool | Description |
|---|---|
| `list_hosts` | List configured hosts |
| `ssh_exec` | Run an arbitrary shell command on a host |
| `docker_ps` | List running containers |
| `docker_logs` | Fetch container logs |
| `docker_restart` | Restart a container |
| `docker_stop` / `docker_start` | Stop or start a container |
| `docker_pull` | Pull an image |
| `docker_compose_up` / `docker_compose_down` | Manage compose stacks |
| `systemctl_status` / `systemctl_restart` | Manage systemd services |
| `read_file` | Read a remote file over SFTP |
| `write_file` | Write/overwrite a remote file over SFTP |
| `disk_usage` | `df -h` summary |
| `memory_usage` | `free -h` summary |

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
