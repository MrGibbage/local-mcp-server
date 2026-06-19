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

## Minimum configuration

Almost everything is optional. To do anything useful (run commands or read files
over SSH) you need just a host and a default:

```yaml
default_host: docker-server
hosts:
  docker-server:
    hostname: 192.168.1.100
    user: myuser
    key_path: /keys/id_rsa
```

That's the whole minimum. The `server:` block is optional (defaults to
`0.0.0.0:8080`), and every integration — `proxmox_nodes`, `loki`, `opnsense`,
`cloudflare`, `api_services`, `ha_light_allowlist` — is purely opt-in. Add a
block only when you want those tools to actually work.

The one hard requirement is that the file **exists**: if `config.yaml` is
missing entirely, the server refuses to start. An empty-but-present file boots.

### What happens when a block is missing

Integration blocks are read **lazily**, at the moment a tool is called — not at
startup. So leaving out a block you don't use costs nothing:

- The server starts normally regardless of which blocks are present.
- A tool whose block is missing **still appears** in the tool list, but returns
  a clear error *only if it is actually called* — e.g. calling `loki_query`
  without a `loki:` block returns `loki config missing — set loki.url in
  config.yaml`. Nothing crashes; every other tool keeps working.

If you'd rather not advertise tools you can't use (so the model never sees them),
restrict each instance with the `MCP_ENABLED_TOOLS` allowlist — see
[Deployment profiles](#deployment-profiles).

## Hosts

Each SSH host you want to manage is one block under `hosts:`. **The block's key
is its name** — the string you (or the model) pass as the `host` argument to a
tool. Names are arbitrary; pick whatever is memorable. With four Docker hosts,
give each a distinct, descriptive key:

```yaml
default_host: docker1         # used when a tool is called without a host argument

hosts:
  docker1:
    hostname: 192.168.1.100   # IP or resolvable name
    user: myuser
    key_path: /keys/id_rsa    # path INSIDE the container (./keys is mounted to /keys)
    # port: 22                # optional
  docker2:
    hostname: 192.168.1.101
    user: myuser
    key_path: /keys/id_rsa
  docker3:
    hostname: 192.168.1.102
    user: myuser
    key_path: /keys/id_rsa
  docker4:
    hostname: 192.168.1.103
    user: myuser
    key_path: /keys/id_rsa
```

A tool then targets one by name: `docker_ps(host="docker3")`. Omitting the
argument falls back to `default_host`. Hosts can share one key (`key_path`) or
each use a different one — the keys just need to live in `./keys/`.

SSH private keys live in `./keys/` on the host, mounted **read-only** into the
container at `/keys/`. The matching public key must be in
`~/.ssh/authorized_keys` on each target host.

## Proxmox nodes — naming with multiple servers

Proxmox is configured separately under `proxmox_nodes:` (a **list**, not a map),
because each node also needs an API token. With six Proxmox servers you get six
list entries. Each entry has three name-related fields, and getting them right
matters:

```yaml
proxmox_nodes:
  - name: pve1          # logical name + ENV VAR PREFIX -> PVE1_API_TOKEN
    host: 192.168.1.10  # API address (https://<host>:8006)
    node: pve1          # the PVE node name as it appears in Proxmox itself
  - name: pve2
    host: 192.168.1.11
    node: pve2
  - name: pve3
    host: 192.168.1.12
    node: pve3
  # ... pve4, pve5, pve6
```

| Field | What it is | Rules |
|---|---|---|
| `name` | The name you pass to a tool (`proxmox_vm_list(host="pve3")`), **and** the prefix of the token env var | Must be unique. Use only letters, digits, and underscores |
| `host` | IP/hostname the API is reached at | Must be unique per physical server |
| `node` | The node name *inside* Proxmox (shown in the web UI / used in API paths) | Usually equals the PVE hostname; may differ from `name` |

**The token env var is derived from `name`, upper-cased, plus `_API_TOKEN`.** So
`name: pve3` looks for `PVE3_API_TOKEN` in the environment. Each of the six nodes
needs its own:

```bash
PVE1_API_TOKEN=user@pam!tokenid=uuid-secret
PVE2_API_TOKEN=user@pam!tokenid=uuid-secret
# ... through PVE6_API_TOKEN
```

> **Gotcha:** because `name` becomes a shell env var name, avoid hyphens or dots
> in it — `name: pve-01` would map to the invalid env var `PVE-01_API_TOKEN`.
> Stick to `pve1`, `pve_dc1`, etc. The `node:` field has no such restriction, so
> put the real Proxmox node name there.

A tool resolves a node by matching the `host` argument against **either** `name`
**or** `host`, so `proxmox_vm_list(host="pve3")` and
`proxmox_vm_list(host="192.168.1.12")` both work.

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
