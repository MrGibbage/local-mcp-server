#!/usr/bin/env python3
"""
PreToolUse hook — block direct curl/http_get calls to homelab service IPs.

Once homelab_api_get/post/mutate are available, there is no legitimate reason
for Claude to curl these IPs directly. Any such call would expose API keys.
"""
import json
import re
import sys

# IPs that host homelab services behind the proxy tools
HOMELAB_IPS = ("192.168.0.181", "192.168.0.231")

# Ports used by homelab services on those IPs (not router/firewall ports)
# Block only when the IP appears with a service port — leave OPNsense (192.168.0.1) alone
SERVICE_PORT_RE = re.compile(
    r"(?:https?://)?(" + "|".join(re.escape(ip) for ip in HOMELAB_IPS) + r")(?::\d+)?/"
)


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def allow() -> None:
    print(json.dumps({"decision": "allow"}))
    sys.exit(0)


try:
    hook_input = json.load(sys.stdin)
except Exception:
    allow()

tool_name = hook_input.get("tool_name", "")
tool_input = hook_input.get("tool_input", {})

# --- Block: mcp__homelab-mcp__http_get to homelab service IPs ---
if "http_get" in tool_name:
    url = tool_input.get("url", "")
    if SERVICE_PORT_RE.search(url):
        block(
            f"Direct http_get to homelab service IP is blocked. "
            f"Use homelab_api_get(service, path) instead — it keeps credentials out of context."
        )

# --- Block: Bash or ssh_exec curl to homelab service IPs ---
if tool_name in ("Bash", "mcp__homelab-mcp__ssh_exec"):
    command = tool_input.get("command", "")
    if "curl" in command and SERVICE_PORT_RE.search(command):
        block(
            f"curl to a homelab service IP is blocked. "
            f"Use homelab_api_get(service, path) or homelab_api_post(service, path, body) instead — "
            f"they resolve credentials server-side so no API key appears in context."
        )

allow()
