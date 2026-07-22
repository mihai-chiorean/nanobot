#!/usr/bin/env python3
"""Generate a least-privilege Nanobot config and empty tree for one tenant."""

from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,190}\.[^@\s]{2,63}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--tenant-root", type=Path, required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--websocket-host", required=True)
    parser.add_argument("--websocket-port", type=int, required=True)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def local_provider(config: dict) -> bool:
    raw = str(config.get("apiBase") or config.get("api_base") or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tenant_config(source: dict, tenant_root: Path, email: str, gateway_port: int,
                  websocket_host: str, websocket_port: int) -> dict:
    normalized_email = email.strip().lower()
    if not EMAIL_RE.fullmatch(normalized_email):
        raise ValueError("invalid tenant email")
    if not (1024 <= gateway_port <= 65535 and 1024 <= websocket_port <= 65535):
        raise ValueError("ports must be between 1024 and 65535")
    if gateway_port == websocket_port:
        raise ValueError("gateway and WebSocket ports must differ")
    ipaddress.ip_address(websocket_host)

    config = copy.deepcopy(source)
    workspace = tenant_root / "workspace"
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    defaults["workspace"] = str(workspace)

    gateway = config.setdefault("gateway", {})
    gateway["host"] = "127.0.0.1"
    gateway["port"] = gateway_port
    gateway.setdefault("heartbeat", {})["enabled"] = False

    channels = config.setdefault("channels", {})
    websocket = copy.deepcopy(channels.get("websocket") or {})
    for name in list(channels):
        if name not in {"sendProgress", "sendToolHints", "sendMaxRetries", "transcriptionProvider", "transcriptionLanguage"}:
            channels.pop(name, None)
    websocket.update({
        "enabled": True,
        "host": websocket_host,
        "port": websocket_port,
        "path": "/",
        "token": "",
        "allowFrom": ["*"],
        "authAllowedEmails": [normalized_email],
        "pingIntervalS": None,
    })
    channels["websocket"] = websocket

    tools = config.setdefault("tools", {})
    tools["restrictToWorkspace"] = True
    tools["mcpServers"] = {}
    execution = tools.setdefault("exec", {})
    execution["enable"] = False
    execution["allowedEnvKeys"] = []

    providers = config.setdefault("providers", {})
    for name, provider in list(providers.items()):
        if not isinstance(provider, dict) or not local_provider(provider):
            providers[name] = {}

    return config


def atomic_json(filename: Path, value: dict) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=filename.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, ensure_ascii=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, filename)
        directory = os.open(filename.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    args = parse_args()
    source_path = args.source_config.expanduser().resolve()
    tenant_root = args.tenant_root.expanduser().resolve()
    destination = tenant_root / "runtime" / "config.json"
    if destination.exists() and not args.force:
        raise SystemExit(f"refusing to replace existing {destination}; pass --force")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    generated = tenant_config(
        source,
        tenant_root,
        args.email,
        args.gateway_port,
        args.websocket_host,
        args.websocket_port,
    )
    for directory in (tenant_root, tenant_root / "runtime", tenant_root / "workspace"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    atomic_json(destination, generated)
    print(destination)


if __name__ == "__main__":
    main()
