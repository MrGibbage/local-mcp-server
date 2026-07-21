# Homelab MCP Server — Claude Instructions

## API calls to homelab services

The MCP server exposes three proxy tools that make API calls to homelab services
server-side, keeping credentials out of Claude's context entirely:

- `homelab_api_get(service, path, params)` — GET requests
- `homelab_api_post(service, path, body)` — POST requests (allowlisted paths only)
- `homelab_api_mutate(service, method, path, body, confirmed)` — PUT/PATCH/DELETE

**Always use these tools.** Never construct a curl command, use `http_get`, or use
`ssh_exec` to call homelab service APIs. This applies to all configured services:
radarr, radarr4k, sonarr, sonarr4k, sabnzbd, jellyfin, tautulli, seerr, grafana,
influxdb, shlink, ntfy, n8n, karakeep, changedetection.

**If a proxy tool returns an error, report the error.** Do not attempt to work
around it by falling back to curl or http_get — the fallback exposes credentials.

### homelab_api_mutate rules

- Default is `confirmed=False`, which blocks the request.
- Only set `confirmed=True` after the user has explicitly said to proceed with
  that specific operation in the current conversation.
- If there is any ambiguity about whether the user confirmed, ask — do not infer.

### Verifying a service is reachable

```
homelab_api_get("radarr", "/system/status")
```

This always returns version info and confirms auth is working.

## Secrets — what not to do

Never run commands or read files that would expose credentials in context:

- Do not run `docker inspect homelab-mcp` (exposes all container env vars)
- Do not run `docker exec homelab-mcp env` or `docker exec homelab-mcp printenv`
- Do not run bare `env`, `printenv`, or `set` via ssh_exec
- Do not run `docker compose config` (resolves and prints all env var values)
- Do not read `.env` files

`config.yaml` is safe to read (credentials are stored in env vars, not in the file).

## Editing this server

- Edit `server.py` locally in `/srv/local-mcp-server/`
- Use `patch_file` (not `write_file`) to modify `config.yaml` on docker-server —
  the live file contains real credentials and must not be overwritten
- `config.yaml` hot-reloads automatically — it's mtime-checked and re-read on
  every tool call, so edits (e.g. a temporary `ssh_command_allowlist` addition
  to test something) take effect on the next call with **no container
  restart**. This does not apply to env vars (`.env` / `/etc/homelab/*.env`),
  which are only read at container start.
- Only `server.py` changes require a rebuild — that's real Python code, it
  can't hot-reload. After editing it: `docker compose up -d --build`
- After a rebuild, check `docker logs homelab-mcp | tail -20` for
  "Application startup complete" and no tracebacks. Startup does **not** log
  a tool count — to confirm a newly added tool actually registered, call it
  (a fresh session/reconnect is needed to pick up the new tool in the client's
  own tool list; the running server has it immediately).
