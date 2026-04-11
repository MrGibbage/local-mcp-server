# Deployment Guide

This guide walks through deploying the homelab MCP server so it is reachable from Claude Cowork (and other MCP clients) over the internet via Cloudflare Tunnel.

> **Note:** Claude Cowork connectors are brokered through Anthropic's cloud infrastructure — the MCP connection originates from Anthropic's servers, not your local machine. This means the server **must** be reachable over the public internet. Private network URLs, Tailscale, and VPN-only hosts will not work for Cowork connectors.

---

## Prerequisites

- A Linux host with Docker and Docker Compose installed
- A domain managed by Cloudflare (free plan is fine)
- A Cloudflare account with Zero Trust enabled (free tier works)
- An existing `cloudflared` tunnel connected to your domain
- SSH key access from the server to each host you want to manage

---

## 1. Clone and Configure

```bash
git clone https://github.com/mrgibbage/local-mcp-server
cd local-mcp-server
cp config.example.yaml config.yaml
```

Edit `config.yaml` to define your hosts and SSH key paths. See `config.example.yaml` for the full schema.

Set your public hostname in `server.py`. Find this line near the bottom of the file and change it to match your domain:

```python
BASE_URL = "https://your-mcp-hostname.yourdomain.com"
```

---

## 2. Deploy with Docker Compose

```bash
docker compose up -d
```

Verify the server is running:

```bash
curl http://localhost:8090/mcp
# Expected: 405 Method Not Allowed (correct — POST is required)
```

The container listens on port **8090** on the host and **8080** inside the container. Change the left side of the port mapping in `docker-compose.yml` if 8090 conflicts with something else.

---

## 3. Cloudflare Tunnel — Public Hostname

In the Cloudflare Zero Trust dashboard:

**Networks → Tunnels → [your tunnel] → Public Hostnames → Add a public hostname**

| Field | Value |
|---|---|
| Subdomain | `your-mcp-hostname` (e.g. `homelab-mcp`) |
| Domain | `yourdomain.com` |
| Service | `http://[your-host-ip]:8090` |

Save the hostname, then open it again and go to **Edit → Additional application settings → HTTP Settings** and enable:

- **Disable Chunked Encoding** → ON

This is required for the MCP Streamable HTTP transport to work correctly through Cloudflare's proxy.

---

## 4. Cloudflare Transform Rule — Strip Authorization Header

Cloudflare's edge will cancel MCP requests that carry an `Authorization: Bearer` header. You need a Transform Rule to strip it before the request enters the tunnel.

In the main Cloudflare dashboard (not Zero Trust):

**[yourdomain.com] → Rules → Transform Rules → Create rule**

- **Rule name:** `Strip Authorization for MCP`
- **If incoming requests match:** Custom filter expression
- Click **Edit expression** and enter:

```
(http.host eq "your-mcp-hostname.yourdomain.com")
```

- **Then → Modify request header:** Remove → `authorization`

Click **Deploy**.

---

## 5. DNS — Verify the Record

The Cloudflare Tunnel automatically creates a DNS CNAME record when you add a public hostname. Confirm it exists:

**[yourdomain.com] → DNS → Records**

You should see a CNAME for your MCP subdomain pointing to your tunnel UUID at `cfargotunnel.com`. If it is missing, add it manually or re-save the public hostname in Zero Trust.

---

## 6. LAN Reverse Proxy (Optional — for Local Browser Auth Flow)

During the OAuth browser flow, your browser is redirected to `https://your-mcp-hostname.yourdomain.com/authorize`. If your LAN DNS resolves that hostname to a local IP (hairpin routing), you need a local reverse proxy route for it — otherwise the browser will hit your router with no handler.

### OPNsense Caddy

**Services → Caddy → Reverse Proxy → Domains → Add**

| Field | Value |
|---|---|
| Protocol | `https://` |
| Domain | `your-mcp-hostname.yourdomain.com` |
| Port | *(leave blank)* |
| Certificate | Auto HTTPS |
| DNS-01 Challenge | ✓ checked |

Add a reverse proxy handle pointing to `http://[your-host-ip]:8090`.

Then add your domain to the DNS rebind exceptions:

**System → Settings → Administration → Alternate Hostnames**

Add `yourdomain.com` and save.

### Other Reverse Proxies

Any reverse proxy (nginx, Traefik, Caddy standalone) will work. Proxy `https://your-mcp-hostname.yourdomain.com` → `http://[your-host-ip]:8090` with a valid TLS certificate. The certificate must be trusted by the browser — self-signed will break the OAuth redirect.

---

## 7. Connect in Claude Cowork

1. Open Cowork and go to **Connectors → Add custom connector**
2. Set the URL to:

```
https://your-mcp-hostname.yourdomain.com/mcp
```

3. Leave OAuth Client ID and Secret blank
4. Click **Add**, then **Connect**

A browser window will open briefly for the OAuth authorization step — this is expected. It will close automatically and Cowork will confirm the connection.

---

## 8. Connect in Claude.ai

The same connector URL works in claude.ai:

**Settings → Connectors → Add custom connector**

Enter `https://your-mcp-hostname.yourdomain.com/mcp` and follow the same OAuth flow.

---

## Verifying the Connection

Once connected, Cowork will enumerate the available tools. You can test immediately by asking Claude something like:

> "List all configured hosts on my MCP server."

or

> "Run `df -h` on docker-server."

---

## Architecture Overview

```
Claude Cowork / claude.ai (Anthropic cloud)
    │
    ▼ HTTPS — Transform Rule strips Authorization header
Cloudflare Edge
    │
    ▼ QUIC tunnel
cloudflared daemon (your host)
    │
    ▼ HTTP :8090
homelab-mcp container
    ├── OAuth discovery  (/.well-known/*, /register, /authorize, /token)
    └── MCP Streamable HTTP  (/mcp)

Your browser (OAuth redirect only)
    │
    ▼ HTTPS
LAN reverse proxy → homelab-mcp container
```

The OAuth browser flow and the MCP tool calls use different network paths. The browser hits your LAN reverse proxy for the `/authorize` redirect. All actual MCP traffic — tool calls, responses — goes through the Cloudflare Tunnel.

---

## Troubleshooting

**"Couldn't reach the MCP server"**
- Confirm the Cloudflare Tunnel public hostname is saved and active (green in Zero Trust → Tunnels)
- Confirm `disableChunkedEncoding` is enabled on the hostname
- Confirm the Transform Rule is deployed and the filter expression matches your hostname exactly

**Browser opens, shows certificate error**
- Your LAN reverse proxy doesn't have a valid cert for the hostname. Enable DNS-01 challenge (or equivalent) so it gets a real Let's Encrypt certificate.

**Browser opens, shows DNS rebind warning (OPNsense)**
- Add your domain to System → Settings → Administration → Alternate Hostnames in OPNsense.

**"Authorization with the MCP server failed"**
- The OAuth flow completed but the MCP connection failed. Check container logs: `docker logs homelab-mcp --tail=50`
- Confirm `POST /mcp` is returning 200 (not 500). A 500 usually means the lifespan context is not initialized — verify the entry point in `server.py` uses the lifespan wrapper shown in the code.

**Tools connect but SSH commands fail**
- Verify the SSH key at the path configured in `config.yaml` is present in the container (check the volume mount)
- Verify the key has been added to `~/.ssh/authorized_keys` on each target host
- Test manually: `docker exec homelab-mcp ssh -i /keys/mcp-server user@host 'echo ok'`

---

## Security Notes

This server implements a **stub OAuth 2.1 flow** — it satisfies the MCP client's discovery requirements but does not enforce real token validation. The Cloudflare Transform Rule strips the Authorization header before requests reach the server, so the bearer token is never verified server-side.

**This means anyone who can reach `https://your-mcp-hostname.yourdomain.com/mcp` can call your tools without authentication.**

To restrict access:

- Add a **Cloudflare Access policy** to `your-mcp-hostname.yourdomain.com` requiring a service token. Pass the service token credentials in the Cowork connector's OAuth Client ID / Client Secret fields.
- Or keep the hostname unlisted and rely on obscurity — the URL is not guessable, and the OAuth browser flow provides a soft gate.
- Or implement real token validation in `server.py` by replacing the stub `/token` endpoint with a proper PKCE-validating token issuer.

The `ssh_command_allowlist` in `config.yaml` limits which commands can be run via `ssh_exec` and is recommended for production use.