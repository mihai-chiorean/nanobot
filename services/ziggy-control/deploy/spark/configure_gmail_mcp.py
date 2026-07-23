#!/usr/bin/env python3
"""Harden a runtime and add its tenant-scoped Ziggy Gmail MCP server."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from provision_tenant import (
    DEFAULT_CONNECTOR_MCP_URL,
    DEFAULT_CONNECTOR_TOKEN_URL,
    DEFAULT_MCP_CLIENT_SECRET_FILE,
    gmail_mcp_server,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--connector-mcp-url", default=DEFAULT_CONNECTOR_MCP_URL)
    parser.add_argument("--connector-token-url", default=DEFAULT_CONNECTOR_TOKEN_URL)
    parser.add_argument(
        "--client-secret-file",
        default=DEFAULT_MCP_CLIENT_SECRET_FILE,
    )
    return parser.parse_args()


def update_config(
    document: dict,
    client_id: str,
    url: str = DEFAULT_CONNECTOR_MCP_URL,
    token_url: str = DEFAULT_CONNECTOR_TOKEN_URL,
    client_secret_file: str = DEFAULT_MCP_CLIENT_SECRET_FILE,
) -> bool:
    expected = gmail_mcp_server(
        client_id,
        url,
        token_url,
        client_secret_file,
    )
    tools = document.setdefault("tools", {})
    changed = tools.get("restrictToWorkspace") is not True
    tools["restrictToWorkspace"] = True
    servers = tools.setdefault("mcpServers", {})
    if servers.get("ziggy_gmail") != expected:
        servers["ziggy_gmail"] = expected
        changed = True
    return changed


def atomic_json(filename: Path, document: dict) -> None:
    metadata = filename.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gmail-mcp-", dir=filename.parent)
    try:
        os.fchmod(descriptor, metadata.st_mode & 0o777)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, indent=2, ensure_ascii=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary_name, metadata.st_uid, metadata.st_gid)
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
    filename = args.config.expanduser().resolve()
    document = json.loads(filename.read_text(encoding="utf-8"))
    changed = update_config(
        document,
        args.client_id,
        args.connector_mcp_url,
        args.connector_token_url,
        args.client_secret_file,
    )
    if changed:
        atomic_json(filename, document)
    print(f"{filename}: {'updated' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
