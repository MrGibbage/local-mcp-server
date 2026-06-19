# Security Model

This server is built on one premise: **let a language model operate a homelab
without ever handing it a secret.** Everything below follows from that.

## Credentials never enter the model's context

The model does not see API keys, SSH private keys, or tokens — not in tool
arguments, not in responses, not in logs it can read.

- **Service APIs** are reached through proxy tools
  (`homelab_api_get/post/mutate`). The server looks up the service in
  `config.yaml`, reads the key from an environment variable, attaches it
  server-side, and returns only the response body.
- **SSH** keys are mounted read-only and referenced by *path*. The model can ask
  to run a command on `nas`; it never sees the key that authenticates it.
- **Integrations** (Home Assistant, Cloudflare, OPNsense) read their tokens from
  the environment at call time. The token name may appear in config; the value
  never does.

`config.yaml` holds only non-secret routing information (hostnames, usernames,
URLs, key *paths*), which is why it is safe to read in a session.

## Secrets live in the environment, not in files Claude reads

API keys and tokens are stored in env files mounted into the container, not in
`config.yaml`. The repo's own guidance forbids the model from running anything
that would surface them — no `docker inspect`, no `env`/`printenv`, no reading
`.env`. A credential-rotation script is kept on hand for the case where a secret
does end up in a transcript.

## Container hardening

- Runs as a **non-root** user (`mcp`) inside the image.
- `config.yaml` and `keys/` are mounted **read-only** (`:ro`).
- Secrets are injected via `env_file`, never baked into the image.
- The image carries only what it needs (Python slim base + OpenSSH client).

## Write operations are opt-in and gated

The toolset is read-biased by default:

- **API proxy:** `GET` is always allowed; `POST` is denied unless the exact path
  is in that service's `post_allowlist`; `PUT`/`PATCH`/`DELETE` require the
  caller to pass `confirmed=true` through the separate `homelab_api_mutate`
  tool. A freshly added service is read-only until you opt into specific writes.
- **SSH:** an optional `ssh_command_allowlist` (global and/or per-host)
  restricts `ssh_exec` to a set of base commands. `ssh_exec` also blocks access
  to known secret paths.
- **Home Assistant:** the LAN-facing instance exposes only scoped
  `ha_light_control`, not arbitrary service calls.

## Tiered exposure

The same image is deployed at three trust levels so that capability tracks
reachability (see [configuration.md](configuration.md#deployment-profiles)):

| Instance | Reachable from | Auth | Capability |
|---|---|---|---|
| `:8090` | Trusted LAN only | LAN trust (no token) | full toolset |
| `:8091` | Public via Cloudflare Tunnel | **bearer token required** | read-only |
| `:8092` | LAN, untrusted clients | none | read-only + scoped lights |

The only internet-reachable instance is read-only **and** token-gated. The
full-power instance never leaves the LAN. Authentication is enforced by a
Starlette bearer-token middleware; the server deliberately does **not** advertise
OAuth or dynamic client registration — clients must present a pre-shared token.

## Recommendations for your own deployment

- Keep the full-power instance bound to the LAN. Do not port-forward 8090.
- If you expose anything publicly, expose only a read-only, token-gated
  instance, and front it with Cloudflare Access or equivalent.
- Use the SSH allowlist if any device on your LAN is untrusted.
- Scope every API token to the minimum the tool needs (read-only where possible).
- Rotate any credential that appears in a transcript.
</content>
