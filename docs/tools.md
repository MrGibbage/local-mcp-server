# Tool Catalog

The server exposes **71 tools**. They're grouped below. Any subset can be
enabled per deployment via the `MCP_ENABLED_TOOLS` allowlist — see
[configuration.md](configuration.md#deployment-profiles).

> Tools are plain Python functions in `server.py` decorated with `@_tool`.
> FastMCP generates each tool's schema from its signature and docstring, so the
> source is the authoritative reference.

## Homelab API proxy

The credential-safe way to reach any configured HTTP service. The model never
sees the key.

| Tool | Description |
|---|---|
| `homelab_api_get` | GET against a configured service |
| `homelab_api_post` | POST (allowlisted paths only) |
| `homelab_api_mutate` | PUT/PATCH/DELETE — requires `confirmed=true` |

## Host & shell

| Tool | Description |
|---|---|
| `list_hosts` | List all configured hosts |
| `ssh_exec` | Run a shell command on a host (allowlist-gated, secret-path blocked) |
| `disk_usage` | `df -h` summary |
| `memory_usage` | `free -h` summary |
| `http_get` | Make an HTTP GET and return status + body |
| `kill_pid` | Send a signal to a single numeric PID (no shell metacharacters) |
| `list_crontab` | List cron jobs for the SSH user (read-only) |

## Docker

| Tool | Description |
|---|---|
| `docker_ps` | List running containers |
| `docker_logs` | Recent container logs |
| `docker_restart` / `docker_start` / `docker_stop` | Lifecycle control |
| `docker_pull` | Pull an image |
| `docker_inspect` | Container configuration and runtime state |
| `docker_exec` | Run a command inside a running container |
| `docker_capabilities` | Decoded Linux capabilities for a container |
| `docker_stats` | One-shot resource usage snapshot |
| `docker_compose_up` / `docker_compose_down` | Manage Compose stacks |
| `docker_compose_logs` | Logs across a Compose stack |
| `docker_network_list` | List Docker networks |

## Systemd

| Tool | Description |
|---|---|
| `systemctl_status` | Status of a service |
| `systemctl_restart` | Restart a service |
| `systemctl_list` | List units with optional state filter |

## Files (over SFTP)

| Tool | Description |
|---|---|
| `read_file` / `write_file` | Read or overwrite a remote file |
| `patch_file` / `regex_patch_file` | Targeted string / regex replacement |
| `tail_file` / `grep_file` | Tail N lines / search a file |
| `stat_file` / `list_directory` | Metadata / directory listing |
| `make_directory` | Create a directory (and parents) |
| `backup_file` | Timestamped backup before editing |
| `validate_config` | Validate YAML/JSON without restarting |
| `rclone_ls` | List files on an rclone remote |

## Screenshots (multi-host, over SFTP)

Any host in `config.yaml` with a `screenshot_dir` set (e.g. a Windows machine)
is queried automatically — the caller doesn't need to know which physical
machine took the screenshot. Built so a session running anywhere (a headless
Linux box, a different machine than the one physically screenshotted) can
still see it. Unreachable hosts are skipped and cached for a cooldown window
rather than retried on every call — see [configuration.md](configuration.md).

| Tool | Description |
|---|---|
| `list_screenshots` | List the most recent screenshots across all screenshot hosts, newest first |
| `get_screenshot` | Fetch a screenshot as a viewable image; defaults to the single most recent across all hosts |

## Proxmox

| Tool | Description |
|---|---|
| `proxmox_vm_list` | List VMs and containers on a node |
| `proxmox_vm_start` / `proxmox_vm_stop` | Power control |
| `proxmox_snapshot_list` / `_create` / `_delete` | Snapshot management |
| `proxmox_task_status` | Poll a task by UPID |
| `proxmox_storage_info` | Storage status for a node |

## Home Assistant

| Tool | Description |
|---|---|
| `ha_get_states` / `ha_get_state` | Entity states |
| `ha_get_history` / `ha_get_logbook` | History and logbook |
| `ha_render_template` | Render a Jinja template |
| `ha_list_automations` / `ha_trigger_automation` | List / fire automations |
| `ha_call_service` | Call any HA service |
| `ha_light_control` | Scoped, whitelisted light control |

## Networking — Caddy / OPNsense / Cloudflare

| Tool | Description |
|---|---|
| `caddy_list_routes` / `caddy_add_route` / `caddy_remove_route` | Caddy reverse-proxy routes on OPNsense |
| `opnsense_list_dhcp_leases` | Active DHCP leases |
| `cloudflare_list_tunnel_routes` / `_add_tunnel_route` / `_remove_tunnel_route` | Cloudflare Tunnel ingress |
| `cloudflare_list_access_policies` / `_add_access_policy` | Zero Trust Access apps & policies |

## Observability

| Tool | Description |
|---|---|
| `loki_query` | Query Loki logs with LogQL |

## Media

| Tool | Description |
|---|---|
| `ffmpeg_probe` | Probe a media file or stream URL (codec/resolution/container) — fixed `-t 5 -f null` read, no arbitrary ffmpeg flags |

## Holocron docs repo

| Tool | Description |
|---|---|
| `holocron_git_status` | Git status + last-commit age for the holocron clone on one host (read-only) |
| `holocron_sync_push` | Run the existing add/commit/pull --rebase/push sync script on one host — requires `confirmed=true` |
