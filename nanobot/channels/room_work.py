"""Private, durable room proposals. No proposal result is part of room history.

The first connected-work capability is deliberately bounded to one exact Gmail
read. Connector mutations continue to use the connector's existing Work approval
protocol; they cannot be dispatched by this read-only capability.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPERATIONS = {"gmail_search", "gmail_get_message"}


def canonical_action(operation: str, account_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if (
        operation not in OPERATIONS
        or not isinstance(account_id, str)
        or not 1 <= len(account_id) <= 512
    ):
        raise ValueError("Choose a connected account and a supported read action.")
    if not isinstance(arguments, dict):
        raise ValueError("Invalid action scope.")
    if operation == "gmail_search":
        if (
            set(arguments) - {"query", "max_results"}
            or not isinstance(arguments.get("query", ""), str)
            or len(arguments.get("query", "")) > 1024
        ):
            raise ValueError("Invalid search scope.")
        maximum = arguments.get("max_results", 10)
        if type(maximum) is not int or not 1 <= maximum <= 20:
            raise ValueError("Choose between 1 and 20 results.")
        arguments = {"query": arguments.get("query", ""), "max_results": maximum}
    else:
        if (
            set(arguments) != {"message_id"}
            or not isinstance(arguments["message_id"], str)
            or not 1 <= len(arguments["message_id"]) <= 256
        ):
            raise ValueError("Choose one exact message.")
    return {"operation": operation, "account_id": account_id, "arguments": arguments}


def action_hash(action: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(action, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RoomWorkStore:
    def __init__(self, workspace: Path):
        self.root = workspace / ".room-work"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def transaction(self, room_id: str):
        if not re.fullmatch(r"room_[a-f0-9]{32}", room_id):
            raise ValueError("Invalid room.")
        path = self.root / f"{room_id}.json"
        with (self.root / f"{room_id}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = (
                json.loads(path.read_text()) if path.exists() else {"version": 1, "proposals": []}
            )
            yield data
            raw = json.dumps(data, ensure_ascii=False)
            if len(raw.encode()) > 4 * 1024 * 1024:
                raise ValueError("Room work storage is full.")
            temp = path.with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def propose(
        self, room_id: str, request_id: str, participant_id: str, action: dict[str, Any]
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise ValueError("A stable request identity is required.")
        action = canonical_action(**action)
        identity = hashlib.sha256(f"{participant_id}:{request_id}".encode()).hexdigest()[:32]
        with self.transaction(room_id) as data:
            for proposal in data["proposals"]:
                if proposal["id"] == identity:
                    if proposal["action"] != action:
                        raise ValueError("This request identity was used for a different action.")
                    return deepcopy(proposal)
            if len(data["proposals"]) >= 100:
                raise ValueError("This room has reached its proposal limit.")
            proposal = {
                "id": identity,
                "participant_id": participant_id,
                "action": action,
                "argument_hash": action_hash(action),
                "state": "proposed",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            data["proposals"].append(proposal)
            return deepcopy(proposal)

    def list(self, room_id: str, *, owner: bool = False) -> list[dict[str, Any]]:
        with self.transaction(room_id) as data:
            return [deepcopy(p) if owner else self.public(p) for p in data["proposals"]]

    @staticmethod
    def public(proposal: dict[str, Any]) -> dict[str, Any]:
        # Do not include account identity, provider IDs, private result, or errors
        # that may contain provider content in participant-visible state.
        return {
            key: deepcopy(proposal[key]) for key in ("id", "participant_id", "state", "created_at")
        }

    def consume(
        self, room_id: str, proposal_id: str, expected_hash: str
    ) -> tuple[dict[str, Any], bool]:
        with self.transaction(room_id) as data:
            for proposal in data["proposals"]:
                if proposal["id"] != proposal_id:
                    continue
                if proposal["argument_hash"] != expected_hash:
                    raise ValueError("Action, account, or scope changed. Review again.")
                if proposal["state"] != "proposed":
                    return deepcopy(proposal), False
                # Consume before calling the provider. An interrupted/unknown
                # execution never runs again merely because delivery was repeated.
                proposal["state"] = "executing"
                proposal["approved_at"] = datetime.now(timezone.utc).isoformat()
                return deepcopy(proposal), True
            raise ValueError("Proposal not found.")

    def finish(
        self, room_id: str, proposal_id: str, result: str | None, *, failed: bool = False
    ) -> None:
        with self.transaction(room_id) as data:
            for proposal in data["proposals"]:
                if proposal["id"] == proposal_id and proposal["state"] == "executing":
                    proposal["state"] = "failed" if failed else "needs_review"
                    # Fail closed on overlarge outputs; never present a truncated
                    # provider response as a complete result.
                    if result is not None and len(result.encode()) <= 256 * 1024:
                        proposal["private_result"] = result
                    else:
                        proposal["state"] = "failed"
                    return

    def publish(self, room_id: str, proposal_id: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > 64 * 1024:
            raise ValueError("Select up to 64 KB of reviewed content.")
        with self.transaction(room_id) as data:
            for proposal in data["proposals"]:
                if proposal["id"] != proposal_id:
                    continue
                if proposal["state"] == "published":
                    if proposal.get("publication") != content:
                        raise ValueError("Already published a different selection.")
                    return deepcopy(proposal)
                if proposal["state"] != "needs_review":
                    raise ValueError("A completed private review is required.")
                proposal["publication"] = content
                proposal["state"] = "published"
                return deepcopy(proposal)
            raise ValueError("Proposal not found.")


def connected_read_executor(agent: Any, server_name: str):
    """Bind to one configured connector, never to model-selected tools or servers."""

    async def execute(action: dict[str, Any]) -> str:
        import asyncio

        from nanobot.agent.tools.mcp import MCPToolWrapper, _sanitize_name

        action = canonical_action(**action)
        async with asyncio.timeout(40):
            await agent._connect_mcp()
            while agent._mcp_connecting:
                await asyncio.sleep(0.05)
            tool = agent.tools.get(_sanitize_name(f"mcp_{server_name}_{action['operation']}"))
            if not isinstance(tool, MCPToolWrapper) or tool._original_name != action["operation"]:
                raise ValueError("This exact connected read is unavailable.")
            arguments = {**action["arguments"], "account_id": action["account_id"]}
            # One RPC, without a model round trip or automatic provider retry.
            result = await tool._session.call_tool(tool._original_name, arguments=arguments)
            if result.isError:
                raise ValueError("The connected read failed.")
            payload = result.structuredContent
            if not isinstance(payload, dict):
                text = "\n".join(part.text for part in result.content if hasattr(part, "text"))
                try:
                    payload = json.loads(text)
                except (ValueError, TypeError):
                    return text
            if not isinstance(payload, dict):
                raise ValueError("Unreadable connected result")
            messages = payload.get("messages")
            if not isinstance(messages, list):
                messages = [payload.get("message", payload)]
            rendered = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                lines = [
                    str(message.get("subject") or "Email"),
                    str(message.get("from") or ""),
                    str(message.get("date") or ""),
                    str(
                        message.get("text_body")
                        or message.get("body")
                        or message.get("snippet")
                        or ""
                    ),
                ]
                rendered.append("\n".join(line for line in lines if line))
            return "\n\n".join(rendered) or "No messages matched the approved search."

    return execute
