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
MCP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")
DEFAULT_CONNECTOR_MCP_URL = "https://127.0.0.1:8790/mcp"
DEFAULT_CONNECTOR_TOKEN_URL = "https://127.0.0.1:8790/oauth/token"
DEFAULT_MCP_CLIENT_SECRET_FILE = "${ZIGGY_MCP_CLIENT_SECRET_FILE}"
GMAIL_MCP_SCOPES = ["gmail.status", "gmail.search", "gmail.read"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--tenant-root", type=Path, required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--websocket-host", required=True)
    parser.add_argument("--websocket-port", type=int, required=True)
    parser.add_argument("--bootstrap-secret-file", type=Path, required=True)
    parser.add_argument("--connector-mcp-url", default=DEFAULT_CONNECTOR_MCP_URL)
    parser.add_argument("--connector-token-url", default=DEFAULT_CONNECTOR_TOKEN_URL)
    parser.add_argument("--gmail-mcp-client-id", default="")
    parser.add_argument(
        "--gmail-mcp-client-secret-file",
        default=DEFAULT_MCP_CLIENT_SECRET_FILE,
    )
    parser.add_argument("--enable-gmail-mcp", action="store_true")
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


def read_bootstrap_secret(filename: Path) -> str:
    try:
        secret = filename.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read bootstrap secret file: {filename}") from exc
    if len(secret) < 32:
        raise ValueError("bootstrap secret must contain at least 32 characters")
    return secret


def valid_websocket_bind_host(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        return False
    if address.is_loopback:
        return True
    if address.version == 4:
        return any(
            address in network
            for network in (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
                ipaddress.ip_network("100.64.0.0/10"),
            )
        )
    return address in ipaddress.ip_network("fc00::/7")


def valid_loopback_service_url(value: str, expected_path: str) -> bool:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.netloc
        or (port is not None and not (1 <= port <= 65535))
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        return False
    try:
        return ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        return (parsed.hostname or "").lower() == "localhost"


def valid_client_secret_file(value: str) -> bool:
    if value == DEFAULT_MCP_CLIENT_SECRET_FILE:
        return True
    path = Path(value)
    return path.is_absolute() and value.startswith("/run/credentials/")


def gmail_mcp_server(
    client_id: str,
    url: str,
    token_url: str = DEFAULT_CONNECTOR_TOKEN_URL,
    client_secret_file: str = DEFAULT_MCP_CLIENT_SECRET_FILE,
) -> dict:
    client_id = client_id.strip()
    if not MCP_CLIENT_ID_RE.fullmatch(client_id):
        raise ValueError("runtime connector client ID is invalid")
    if not valid_loopback_service_url(url, "/mcp"):
        raise ValueError("connector MCP URL must be loopback HTTPS at /mcp")
    if not valid_loopback_service_url(token_url, "/oauth/token"):
        raise ValueError("connector token URL must be loopback HTTPS at /oauth/token")
    if not valid_client_secret_file(client_secret_file):
        raise ValueError("connector client secret file must be a systemd credential path")
    return {
        "type": "streamableHttp",
        "url": url,
        "oauthClientCredentials": {
            "tokenUrl": token_url,
            "clientId": client_id,
            "clientSecretFile": client_secret_file,
            "scopes": GMAIL_MCP_SCOPES,
        },
        "enabledTools": [
            "gmail_connection_status",
            "gmail_search",
            "gmail_get_message",
        ],
        "toolTimeout": 30,
    }


def tenant_config(
    source: dict,
    tenant_root: Path,
    email: str,
    gateway_port: int,
    websocket_host: str,
    websocket_port: int,
    bootstrap_secret: str = "",
    connector_mcp_url: str = DEFAULT_CONNECTOR_MCP_URL,
    enable_gmail_mcp: bool = False,
    gmail_mcp_client_id: str = "",
    connector_token_url: str = DEFAULT_CONNECTOR_TOKEN_URL,
    gmail_mcp_client_secret_file: str = DEFAULT_MCP_CLIENT_SECRET_FILE,
) -> dict:
    normalized_email = email.strip().lower()
    if not EMAIL_RE.fullmatch(normalized_email):
        raise ValueError("invalid tenant email")
    if not (1024 <= gateway_port <= 65535 and 1024 <= websocket_port <= 65535):
        raise ValueError("ports must be between 1024 and 65535")
    if gateway_port == websocket_port:
        raise ValueError("gateway and WebSocket ports must differ")
    if not valid_websocket_bind_host(websocket_host):
        raise ValueError("WebSocket host must be loopback, RFC1918, IPv6 ULA, or Tailscale CGNAT")
    bootstrap_secret = bootstrap_secret.strip()
    if len(bootstrap_secret) < 32:
        raise ValueError("bootstrap secret must contain at least 32 characters")
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
    auth_issuer = websocket.get("authIssuer") or websocket.get("auth_issuer")
    auth_jwks_url = websocket.get("authJwksUrl") or websocket.get("auth_jwks_url")
    authorized_parties = websocket.get("authAuthorizedParties") or websocket.get(
        "auth_authorized_parties"
    )
    if not isinstance(auth_issuer, str) or not auth_issuer.strip():
        raise ValueError("source WebSocket config is missing authIssuer")
    if not isinstance(auth_jwks_url, str) or not auth_jwks_url.strip():
        raise ValueError("source WebSocket config is missing authJwksUrl")
    if not isinstance(authorized_parties, list) or not all(
        isinstance(party, str) and party.strip() for party in authorized_parties
    ):
        raise ValueError("source WebSocket config is missing authAuthorizedParties")
    for name in list(channels):
        if name not in {
            "sendProgress",
            "sendToolHints",
            "sendMaxRetries",
            "transcriptionProvider",
            "transcriptionLanguage",
        }:
            channels.pop(name, None)
    websocket.update(
        {
            "enabled": True,
            "host": websocket_host,
            "port": websocket_port,
            "path": "/",
            "token": "",
            "tokenIssuePath": "/auth/token",
            "tokenIssueSecret": bootstrap_secret,
            "allowFrom": ["*"],
            "authAllowedEmails": [normalized_email],
            "pingIntervalS": None,
        }
    )
    channels["websocket"] = websocket

    tools = config.setdefault("tools", {})
    tools["restrictToWorkspace"] = True
    tools["rag"] = {"enable": False}
    tools["mcpServers"] = (
        {
            "ziggy_gmail": gmail_mcp_server(
                gmail_mcp_client_id,
                connector_mcp_url,
                connector_token_url,
                gmail_mcp_client_secret_file,
            )
        }
        if enable_gmail_mcp
        else {}
    )
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
        read_bootstrap_secret(args.bootstrap_secret_file.expanduser().resolve()),
        args.connector_mcp_url,
        args.enable_gmail_mcp,
        args.gmail_mcp_client_id,
        args.connector_token_url,
        args.gmail_mcp_client_secret_file,
    )
    for directory in (tenant_root, tenant_root / "runtime", tenant_root / "workspace"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    atomic_json(destination, generated)
    print(destination)


if __name__ == "__main__":
    main()
