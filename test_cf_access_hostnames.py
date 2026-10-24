"""Access apps: list secondary destinations, not just the primary `domain`.
The tool returned `domain` only, so "+1 other domain" aliases (prox1, prox2,
sab, dash) looked absent on 2026-09-05 when they were present."""
from __future__ import annotations

import os

os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.example.yaml"))

import server  # noqa: E402

hostnames = server._cf_access_app_hostnames


def test_primary_only():
    assert hostnames({"domain": "a.pelorus.org"}) == ["a.pelorus.org"]


def test_destinations_add_secondaries_in_order():
    app = {
        "domain": "proxmox1.pelorus.org",
        "destinations": [
            {"type": "public", "uri": "proxmox1.pelorus.org"},
            {"type": "public", "uri": "prox1.pelorus.org"},
        ],
    }
    assert hostnames(app) == ["proxmox1.pelorus.org", "prox1.pelorus.org"]


def test_legacy_self_hosted_domains():
    app = {"domain": "a.pelorus.org", "self_hosted_domains": ["a.pelorus.org", "b.pelorus.org"]}
    assert hostnames(app) == ["a.pelorus.org", "b.pelorus.org"]


def test_private_destinations_skipped():
    app = {
        "domain": "a.pelorus.org",
        "destinations": [{"type": "private", "cidr": "10.0.0.0/8"}, {"uri": "c.pelorus.org"}],
    }
    assert hostnames(app) == ["a.pelorus.org", "c.pelorus.org"]


def test_missing_fields():
    assert hostnames({}) == []
    assert hostnames({"domain": None, "destinations": None, "self_hosted_domains": None}) == []
