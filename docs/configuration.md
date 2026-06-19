# Configuration

Everything the server does is driven by **`config.yaml`** plus a set of
**environment variables** for secrets. Nothing is hard-coded to a particular
homelab — to adopt this server, you edit config, not code.

`config.yaml` is gitignored. A documented template lives in
[`config.example.yaml`](../config.example.yaml); copy it and fill in your values.

```bash
cp config.example.yaml config.yaml
```

## The split: config vs. secrets

| Goes in `config.yaml` | Goes in environment variables |
|---|---|
| Hostnames, IPs, usernames | API keys, tokens, passwords |
| SSH key **paths** (e.g. `/keys/id_rsa`) | SSH key **contents** (mounted as files) |
| Service base URLs, auth *style* | The auth *value* (referenced by env var name) |
| Allowlists, ports, default host | — |

This is what lets `config.yaml` be safe to read in a session while secrets stay
out of context entirely. See [security.md](security.md).

## Hosts

Each host you want to manage is one block:

```yaml
default_host: docker-server   # used when a tool is called without a host argument

hosts:
  docker-server:
    hostname: 192.168.1.100   # IP or resolvable name
    user: myuser
    key_path: /keys/id_rsa    # path INSIDE the container (./keys is mounted to /keys)
    # port: 22                # optional

  nas:
    hostname: 192.168.1.101
    user: myuser
    key_path: /keys/id_rsa
```

SSH private keys live in `./keys/` on the host, mounted **read-only** into the
container at `/keys/`. The matching public key must be in
`~/.ssh/authorized_keys` on each target host.

## The homelab API proxy — extensibility in one block

This is the part that makes the server genuinely reusable. To let the model
operate any HTTP API service without seeing its credentials, you add a block
under `api_services` and put the secret in an environment variable:

```yaml
api_services:
  radarr:
    base_url: http://192.168.1.100:7878/api/v3
    auth_style: header          # header | bearer | token | query_param
    auth_header: X-Api-Key      # for auth_style: header
    auth_env: RADARR_API_KEY    # the env var holding the key
    post_allowlist:             # POST is blocked unless the path is listed here
      - /command
      - /movie/editor
```

Then add `RADARR_API_KEY=...` to your env file and restart. The model can now
call:

```
homelab_api_get("radarr", "/system/status")
homelab_api_post("radarr", "/command", {"name": "RefreshMovie"})
```

Supported `auth_style` values:

| Style | Where the secret goes |
|---|---|
| `header` | A custom header (`auth_header`), e.g. `X-Api-Key` |
| `bearer` | `Authorization: Bearer <token>` |
| `token` | A query/token convention specific to the service |
| `query_param` | A URL query parameter (`auth_param`), e.g. `?apikey=` |

**Write safety:** `GET` is always allowed. `POST` is denied unless the exact
path is in `post_allowlist`. `PUT`/`PATCH`/`DELETE` go through a separate
`homelab_api_mutate` tool that requires an explicit `confirmed=true`. This means
adding a service is read-only by default — you opt into writes path by path.

## SSH command allowlist

`ssh_exec` runs shell commands on a host. You can restrict it to a set of base
commands, globally or per-host:

```yaml
# global — applies to every host
ssh_command_allowlist:
  - docker
  - systemctl
  - df
  - free
  - journalctl

hosts:
  nas:
    hostname: 192.168.1.101
    user: myuser
    key_path: /keys/id_rsa
    ssh_command_allowlist:   # merged with the global list, for this host only
      - zpool
      - zfs
```

Omit the global block (or set it to `null`) to allow everything. The check
matches the **first token** of the command against the list.

## Integration blocks

Optional blocks enable whole tool families. Each follows the same pattern:
non-secret settings in `config.yaml`, secrets in env vars.

- **`opnsense`** — Caddy reverse-proxy and DHCP tools (`caddy_*`, `opnsense_*`).
  API key/secret come from `OPNSENSE_API_KEY` / `OPNSENSE_API_SECRET`.
- **`cloudflare`** — Tunnel ingress and Zero Trust Access tools. Two scoped
  tokens via `CLOUDFLARE_TUNNEL_API_TOKEN` / `CLOUDFLARE_ACCESS_API_TOKEN`.
- **Home Assistant** — `ha_*` tools. Base URL in config, long-lived token in
  `HA_TOKEN`.

See [`config.example.yaml`](../config.example.yaml) for the exact keys.

## Hot reload

`config.yaml` is re-read whenever its modification time changes — host edits,
new `api_services`, and allowlist changes take effect on the **next tool call**
with no restart. Environment variables (secrets) are read at startup and require
a container restart to pick up changes.

## Deployment profiles

The same image runs at different trust levels purely through environment
variables. Two knobs do all the work:

- **`MCP_ENABLED_TOOLS`** — a comma-separated allowlist. When set, only these
  tools are registered; everything else is invisible to clients. Unset = all
  tools.
- **`MCP_AUTH_TOKEN`** — when set, every request must carry
  `Authorization: Bearer <token>`. Unset = no auth (rely on network trust).

The committed [`compose.yml`](../compose.yml) defines three:

```yaml
homelab-mcp        # :8090  full toolset, LAN trust, no token
homelab-mcp-ro     # :8091  read-only subset, bearer token REQUIRED (public via Cloudflare)
homelab-mcp-lan    # :8092  read-only subset + scoped light control, LAN-bound, no token
```

The `-ro` instance is the only one safe to expose to the internet, and it can
only read. The full-power instance is never routed off the LAN.

## Connecting a client

The server speaks **Streamable HTTP** at `/mcp`.

**Claude Code:**

```bash
claude mcp add homelab --transport http http://192.168.1.100:8090/mcp
```

**`.mcp.json` / `claude_desktop_config.json`:**

```json
{
  "mcpServers": {
    "homelab": {
      "type": "http",
      "url": "http://192.168.1.100:8090/mcp"
    }
  }
}
```

For a token-protected instance, add the header your client supports
(e.g. `Authorization: Bearer <token>`).
</content>
