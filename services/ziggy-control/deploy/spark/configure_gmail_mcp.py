#!/usr/bin/env python3
"""Harden a runtime and add its tenant-scoped Ziggy Gmail MCP server."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from provision_tenant import DEFAULT_CONNECTOR_MCP_URL, gmail_mcp_server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connector-mcp-url", default=DEFAULT_CONNECTOR_MCP_URL)
    return parser.parse_args()


def update_config(document: dict, url: str = DEFAULT_CONNECTOR_MCP_URL) -> bool:
    websocket = (document.get("channels") or {}).get("websocket") or {}
    capability = str(
        websocket.get("tokenIssueSecret") or websocket.get("token_issue_secret") or ""
    )
    expected = gmail_mcp_server(capability, url)
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
    changed = update_config(document, args.connector_mcp_url)
    if changed:
        atomic_json(filename, document)
    print(f"{filename}: {'updated' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
