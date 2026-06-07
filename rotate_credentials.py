#!/usr/bin/env python3
"""
rotate_credentials.py — Rotate exposed homelab MCP server credentials.

Run this whenever credentials have been exposed in a Claude Code conversation.
The script handles the full rotation lifecycle: create new, update .env,
verify, then delete old.

Automation level per service:
  Proxmox    — fully automated (create-before-delete, no access gap)
  Cloudflare — manual prompt (current tokens lack API Tokens: Edit permission)
  OPNsense   — manual prompt (API key has limited scope, no user management)

Secrets live in /srv/local-mcp-server/.env — config.yaml contains no secrets.

Usage:
  python3 /srv/local-mcp-server/rotate_credentials.py
"""

import re
import sys
import time
import subprocess
import requests
import urllib3
import yaml
from datetime import datetime
from pathlib import Path

urllib3.disable_warnings()

ENV_PATH = Path("/srv/local-mcp-server/.env")
CONFIG_PATH = Path("/srv/local-mcp-server/config.yaml")
COMPOSE_FILE = "/srv/local-mcp-server/compose.yml"
TODAY = datetime.now().strftime("%Y%m%d")


def load_env() -> dict:
    """Parse .env file into a dict, ignoring comments and blank lines."""
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def patch_env(key: str, new_value: str):
    """Replace a key's value in .env in-place."""
    content = ENV_PATH.read_text()
    new_content = re.sub(
        rf"^{re.escape(key)}=.*$",
        f"{key}={new_value}",
        content,
        flags=re.MULTILINE,
    )
    if new_content == content:
        raise ValueError(f"Key {key!r} not found in .env")
    ENV_PATH.write_text(new_content)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def section(title: str):
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def prompt_value(label: str, instructions: str) -> str:
    print(f"\n{instructions}")
    while True:
        val = input(f"  {label}: ").strip()
        if val:
            return val
        print("  (cannot be empty, try again)")


# ─── Proxmox ─────────────────────────────────────────────────────────────────

def rotate_proxmox(cfg: dict, env: dict) -> list[tuple]:
    """
    Creates new API tokens on each Proxmox node using the existing token for auth,
    updates .env, and returns cleanup info for post-verification deletion.

    Returns list of (node_cfg, old_token_name, env_key, new_token_str) tuples.
    """
    section("Proxmox: Rotating API tokens (automated)")
    new_token_name = f"mcp-{TODAY}"
    results = []

    for node in cfg["proxmox_nodes"]:
        name = node["name"]
        host = node["host"]
        env_key = f"{name.upper()}_API_TOKEN"  # e.g. PROXMOX1_API_TOKEN
        old_token_str = env.get(env_key, "")

        if not old_token_str:
            print(f"[{name}] ERROR: {env_key} not found in .env")
            sys.exit(1)

        # Parse "root@pam!mcp-token=<uuid>" → old_token_name = "mcp-token"
        prefix, _ = old_token_str.rsplit("=", 1)
        _, old_token_name = prefix.split("!")

        print(f"\n[{name}] Current: root@pam!{old_token_name}")
        print(f"[{name}] Creating: root@pam!{new_token_name}")

        r = requests.post(
            f"https://{host}:8006/api2/json/access/users/root@pam/token/{new_token_name}",
            headers={"Authorization": f"PVEAPIToken={old_token_str}"},
            verify=False,
            timeout=15,
            json={"comment": f"MCP server — rotated {TODAY}", "privsep": 0},
        )

        if r.status_code not in (200, 201):
            print(f"[{name}] ERROR: HTTP {r.status_code}")
            print(r.text)
            sys.exit(1)

        new_secret = r.json()["data"]["value"]
        new_token_str = f"root@pam!{new_token_name}={new_secret}"
        print(f"[{name}] Created root@pam!{new_token_name} — updating .env")

        patch_env(env_key, new_token_str)
        results.append((node, old_token_name, env_key, new_token_str))

    return results


def delete_old_proxmox_tokens(results: list):
    """Delete old tokens after new ones are verified working."""
    section("Proxmox: Deleting old tokens")
    for node_cfg, old_token_name, env_key, new_token_str in results:
        name = node_cfg["name"]
        host = node_cfg["host"]
        print(f"\n[{name}] Deleting root@pam!{old_token_name}")
        r = requests.delete(
            f"https://{host}:8006/api2/json/access/users/root@pam/token/{old_token_name}",
            headers={"Authorization": f"PVEAPIToken={new_token_str}"},
            verify=False,
            timeout=15,
        )
        if r.status_code == 200:
            print(f"[{name}] Deleted")
        else:
            print(f"[{name}] WARNING: delete returned {r.status_code} — may need manual cleanup")


# ─── Cloudflare ──────────────────────────────────────────────────────────────

def rotate_cloudflare(env: dict):
    section("Cloudflare: Manual token rotation")
    tunnel_prefix = env.get("CLOUDFLARE_TUNNEL_API_TOKEN", "")[:12]
    access_prefix = env.get("CLOUDFLARE_ACCESS_API_TOKEN", "")[:12]

    print(f"""
Go to: https://dash.cloudflare.com/profile/api-tokens

Create TWO new tokens, then revoke the two old ones shown below.

1. Tunnel token  (old token starts with: {tunnel_prefix}...)
   Permissions: Cloudflare Tunnel → Edit
   Account Resources: your account

2. Access token  (old token starts with: {access_prefix}...)
   Permissions: Access: Apps and Policies → Edit
   Account Resources: your account

Revoke the old tokens after creating the new ones.
""")

    new_tunnel = prompt_value("New CLOUDFLARE_TUNNEL_API_TOKEN", "Paste the new Cloudflare Tunnel token:")
    patch_env("CLOUDFLARE_TUNNEL_API_TOKEN", new_tunnel)
    print("CLOUDFLARE_TUNNEL_API_TOKEN updated in .env")

    new_access = prompt_value("New CLOUDFLARE_ACCESS_API_TOKEN", "Paste the new Cloudflare Access token:")
    patch_env("CLOUDFLARE_ACCESS_API_TOKEN", new_access)
    print("CLOUDFLARE_ACCESS_API_TOKEN updated in .env")


# ─── OPNsense ─────────────────────────────────────────────────────────────────

def rotate_opnsense(env: dict, cfg: dict):
    section("OPNsense: Manual API key rotation")
    opn_url = cfg.get("opnsense", {}).get("url", "https://192.168.0.1:8443")
    key_prefix = env.get("OPNSENSE_API_KEY", "")[:12]

    print(f"""
Go to: {opn_url} → System → Access → Users

Find the user whose API key starts with: {key_prefix}...
(Likely the user 'skip' or 'mcp-api'.)

In the user's edit page, scroll to the "API Keys" section:
  1. Delete the existing key
  2. Click "+" to generate a new key/secret pair
  3. Copy both values — the secret is only shown once!
""")

    new_key = prompt_value("New OPNSENSE_API_KEY", "Paste the new OPNsense API key:")
    patch_env("OPNSENSE_API_KEY", new_key)

    new_secret = prompt_value("New OPNSENSE_API_SECRET", "Paste the new OPNsense API secret:")
    patch_env("OPNSENSE_API_SECRET", new_secret)

    print("OPNsense credentials updated in .env")


# ─── Restart & Verify ─────────────────────────────────────────────────────────

def restart_container():
    section("Restarting homelab-mcp container")
    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "restart"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        sys.exit(1)
    print("Container restarted — waiting 5s for startup...")
    time.sleep(5)


def verify_services(cfg: dict, env: dict) -> bool:
    section("Verifying services with new credentials")
    all_ok = True

    # Proxmox nodes
    for node in cfg.get("proxmox_nodes", []):
        env_key = f"{node['name'].upper()}_API_TOKEN"
        token = env.get(env_key, "")
        try:
            r = requests.get(
                f"https://{node['host']}:8006/api2/json/nodes",
                headers={"Authorization": f"PVEAPIToken={token}"},
                verify=False, timeout=10,
            )
            if r.status_code == 200:
                print(f"  [OK] {node['name']}")
            else:
                print(f"  [FAIL] {node['name']} — HTTP {r.status_code}")
                all_ok = False
        except Exception as e:
            print(f"  [FAIL] {node['name']} — {e}")
            all_ok = False

    # OPNsense
    try:
        opn = cfg.get("opnsense", {})
        r = requests.get(
            f"{opn['url']}/api/caddy/ReverseProxy/getReverseProxy",
            auth=(env["OPNSENSE_API_KEY"], env["OPNSENSE_API_SECRET"]),
            verify=False, timeout=10,
        )
        if r.status_code == 200:
            print(f"  [OK] OPNsense")
        else:
            print(f"  [FAIL] OPNsense — HTTP {r.status_code}")
            all_ok = False
    except Exception as e:
        print(f"  [FAIL] OPNsense — {e}")
        all_ok = False

    # Cloudflare
    try:
        cf = cfg.get("cloudflare", {})
        r = requests.get(
            f"https://api.cloudflare.com/client/v4/accounts/{cf['account_id']}/cfd_tunnel/{cf['tunnel_id']}",
            headers={"Authorization": f"Bearer {env['CLOUDFLARE_TUNNEL_API_TOKEN']}"},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("success"):
            print(f"  [OK] Cloudflare Tunnel")
        else:
            print(f"  [FAIL] Cloudflare Tunnel — HTTP {r.status_code}")
            all_ok = False
    except Exception as e:
        print(f"  [FAIL] Cloudflare Tunnel — {e}")
        all_ok = False

    return all_ok


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════════════════════╗
║        homelab MCP Credential Rotation Script            ║
╠══════════════════════════════════════════════════════════╣
║  Rotates all credentials in .env.                        ║
║  Run after any session where credentials were exposed.   ║
╚══════════════════════════════════════════════════════════╝
""")

    for path in (ENV_PATH, CONFIG_PATH):
        if not path.exists():
            print(f"ERROR: {path} not found")
            sys.exit(1)

    cfg = load_config()
    env = load_env()

    # Phase 1: Proxmox — automated, creates new tokens before touching .env
    proxmox_results = rotate_proxmox(cfg, env)

    # Phase 2-4: Manual services — prompts user, updates .env as each is entered
    rotate_cloudflare(env)
    rotate_opnsense(env, cfg)
    # BookStack rotation removed — service decommissioned 2026-05-27 (migrated to Holocron)

    # Phase 5: Restart container with new credentials
    restart_container()

    # Phase 6: Verify all services (reload .env after all updates)
    env = load_env()
    ok = verify_services(cfg, env)

    if ok:
        # Phase 7: Safe to delete old Proxmox tokens now that everything is verified
        delete_old_proxmox_tokens(proxmox_results)
        section("Rotation complete")
        print(f"\nAll credentials rotated and verified.")
        print(f"New Proxmox token name: root@pam!mcp-{TODAY}")
    else:
        section("WARNING: Verification failures")
        print("\nFix the failing services before deleting old Proxmox tokens.")
        print("Old tokens have been preserved. To delete them manually:")
        for node_cfg, old_token_name, _, _ in proxmox_results:
            print(f"  [{node_cfg['name']}] Proxmox UI → Datacenter → Permissions → API Tokens → root@pam!{old_token_name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
