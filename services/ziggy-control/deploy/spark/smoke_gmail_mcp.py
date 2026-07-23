#!/usr/bin/env python3
"""Exercise the deployed Gmail MCP path with the runtime's real config."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, resolve_config_env_vars

SERVER_NAME = "ziggy_gmail"
STATUS_TOOL = "mcp_ziggy_gmail_gmail_connection_status"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    return parser.parse_args()


async def smoke(config_path: Path) -> None:
    config = resolve_config_env_vars(load_config(config_path.expanduser().resolve()))
    server = config.tools.mcp_servers.get(SERVER_NAME)
    if server is None:
        raise RuntimeError(f"{SERVER_NAME} is not configured")

    registry = ToolRegistry()
    stacks = await connect_mcp_servers({SERVER_NAME: server}, registry)
    try:
        if not registry.has(STATUS_TOOL):
            raise RuntimeError("Gmail MCP status tool was not registered")
        result = await registry.execute(STATUS_TOOL, {})
        if result.startswith("Error"):
            raise RuntimeError("Gmail MCP status tool returned an error")
    finally:
        await asyncio.gather(
            *(stack.aclose() for stack in stacks.values()),
            return_exceptions=True,
        )


def main() -> None:
    args = parse_args()
    asyncio.run(smoke(args.config))
    print("Gmail MCP smoke passed")


if __name__ == "__main__":
    main()
