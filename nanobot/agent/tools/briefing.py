"""Conversation-bound client for Ziggy's versioned briefing workflows."""

from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "What to do with this conversation's briefing.",
            enum=[
                "inspect",
                "create",
                "update",
                "pause",
                "resume",
                "regenerate",
                "feedback",
            ],
        ),
        workflow_id=StringSchema(
            "An existing workflow ID returned by inspect. Required except for inspect/create."
        ),
        title=StringSchema("Briefing title; required when creating.", max_length=160),
        instructions=StringSchema(
            "Complete future-edition instructions. Required when creating; omitted fields stay unchanged when updating.",
            max_length=16000,
        ),
        sources=ArraySchema(
            StringSchema("A source, website or connected-source scope."), max_items=32
        ),
        frequency=StringSchema("Required when creating.", enum=["daily", "weekdays"]),
        hour=IntegerSchema(
            description="Local scheduled hour; required when creating.", minimum=0, maximum=23
        ),
        minute=IntegerSchema(
            description="Local scheduled minute; required when creating.", minimum=0, maximum=59
        ),
        time_zone=StringSchema(
            "IANA timezone, such as America/Los_Angeles; required when creating."
        ),
        edition_id=StringSchema(
            "Exact existing edition to regenerate or leave feedback on. Required for those actions."
        ),
        feedback=StringSchema(
            "Feedback for this edition only, without changing future instructions.", max_length=4000
        ),
        required=["action"],
    )
)
class BriefingTool(Tool):
    def __init__(self, config, sessions, *, transport=None):
        self._config = config
        self._sessions = sessions
        self._transport = transport
        self._turn = contextvars.ContextVar("briefing_turn", default=None)
        target = urlsplit(config.control_url)
        try:
            loopback = (
                target.hostname == "localhost"
                or ipaddress.ip_address(target.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            (target.scheme != "https" and not (target.scheme == "http" and loopback))
            or not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path not in {"", "/"}
        ):
            raise ValueError("Briefing Control URL must be an HTTPS origin or loopback HTTP origin")
        if not config.user_id or not config.workspace_id or not config.credential_file:
            raise ValueError("Briefing runtime allocation and credential file are required")
        credential_path = Path(config.credential_file).expanduser().resolve()
        if credential_path.is_relative_to(sessions.workspace.resolve()):
            raise ValueError("Briefing credential must be outside the model workspace")
        self._credential_path = credential_path
        self._base_url = config.control_url.rstrip("/") + "/runtime/briefings"

    @property
    def name(self):
        return "briefing"

    @property
    def description(self):
        return (
            "Create and manage the current private conversation's daily/weekday briefing in "
            "Ziggy's Result and Updates views. Prefer this tool to cron or schedule_work for "
            "daily briefings: it uses the same versioned instructions, editions and schedule "
            "as the app editor. Inspect first when changing existing work. Create schedules "
            "or change instructions only when requested by the user; ask for missing source "
            "scope or schedule details. Create saves the workflow and queues its first edition. "
            "Update affects future editions; regenerate uses the chosen edition's original "
            "instructions; feedback does not change instructions. Never claim a queued edition "
            "has finished. Keep workflow and edition IDs internal unless asked; describe the "
            "result and schedule plainly. Shared-room participants must use proposals instead."
        )

    def set_context(self, channel, chat_id, *, metadata=None, session_key=None, message_id=None):
        metadata = metadata or {}
        key = session_key or f"{channel}:{chat_id}"
        session = self._sessions.get_or_create(key)
        private = (
            channel == "websocket"
            and key == f"websocket:{chat_id}"
            and not metadata.get("shared_room")
            and not session.metadata.get("shared_room")
            and not metadata.get("work_mode")
        )
        origin = metadata.get("client_message_id") or message_id
        self._turn.set((chat_id, str(origin or "")) if private else None)

    async def execute(self, action: str, workflow_id: str | None = None, **changes: Any) -> str:
        turn = self._turn.get()
        if turn is None:
            return "Error: Briefings can only be managed in a private owner conversation. Room participants must propose connected work."
        chat_id, origin = turn
        if action != "inspect" and not origin:
            return "Error: A durable message identity is required. Use the briefing editor."
        try:
            secret = self._credential_path.read_text().strip()
            if not 32 <= len(secret) <= 4096:
                raise ValueError("Briefing credential is unavailable")
            headers = {
                "Authorization": "Bearer " + secret,
                "X-Ziggy-Runtime-User": self._config.user_id,
                "X-Ziggy-Runtime-Workspace": self._config.workspace_id,
            }
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=False, transport=self._transport
            ) as client:

                async def request(method, path, payload=None, key=None, params=None):
                    response = await client.request(
                        method,
                        self._base_url + path,
                        headers={
                            **headers,
                            **({"Idempotency-Key": key} if key else {}),
                        },
                        json=payload,
                        params=params,
                    )
                    if response.status_code == 409:
                        raise ValueError(
                            "The workflow changed. Inspect it again before retrying; no newer edits were overwritten."
                        )
                    response.raise_for_status()
                    return response.json()

                capabilities = await request("GET", "/capabilities")
                if capabilities.get("editorial") is not True or capabilities.get("version") != 1:
                    return "Error: Versioned briefings are not enabled on this server."
                context = await request("GET", "/context", params={"session_key": chat_id})
                workflows = context.get("workflows", [])
                if action == "inspect":
                    return json.dumps(
                        {
                            "workflows": workflows,
                            "results": [
                                {
                                    k: t.get(k)
                                    for k in ("task_id", "status", "result_summary", "error")
                                }
                                for t in context.get("tasks", [])
                            ],
                        },
                        ensure_ascii=False,
                    )
                if action not in {"create", "update", "pause", "resume", "regenerate", "feedback"}:
                    raise ValueError("Unsupported briefing action")
                identity = hashlib.sha256(f"{chat_id}:{origin}".encode()).hexdigest()
                if action == "create":
                    workflow_id = "wf_" + identity[:32]
                workflow = next((w for w in workflows if w.get("id") == workflow_id), None)
                if action != "create" and workflow is None:
                    raise ValueError(
                        "Inspect this conversation and choose one of its workflow IDs."
                    )
                if workflow and workflow.get("session_key") != chat_id:
                    raise ValueError("Workflow belongs to a different conversation")
                path = "/workflows/" + str(workflow_id)
                if action in {"create", "update", "pause", "resume"}:
                    if action == "create":
                        needed = (
                            "title",
                            "instructions",
                            "sources",
                            "frequency",
                            "hour",
                            "minute",
                            "time_zone",
                        )
                        if any(changes.get(k) is None for k in needed):
                            raise ValueError(
                                "Ask the user for missing briefing instructions, sources, schedule or timezone before creating it."
                            )
                        payload = {
                            "session_key": chat_id,
                            "title": changes["title"],
                            "instructions": changes["instructions"],
                            "sources": changes["sources"],
                            "expected_version": 0,
                            "schedule": {
                                k: changes[k] for k in ("frequency", "hour", "minute", "time_zone")
                            },
                        }
                        payload["schedule"]["enabled"] = True
                    else:
                        revision = workflow["revisions"][-1]
                        payload = {
                            "session_key": chat_id,
                            "title": workflow["title"],
                            "instructions": revision["instructions"],
                            "sources": revision["sources"],
                            "expected_version": workflow["version"],
                            "schedule": dict(workflow["schedule"]),
                        }
                        if action == "update":
                            for name in ("title", "instructions", "sources"):
                                if changes.get(name) is not None:
                                    payload[name] = changes[name]
                            for name in ("frequency", "hour", "minute", "time_zone"):
                                if changes.get(name) is not None:
                                    payload["schedule"][name] = changes[name]
                        elif action in {"pause", "resume"}:
                            payload["schedule"]["enabled"] = action == "resume"
                    payload["schedule"].pop("next_run", None)
                    if action == "create" and workflow:
                        # A repeated delivery may see a saved workflow whose first
                        # run was unconfirmed. Reuse it only if every input matches.
                        revision = workflow["revisions"][-1]
                        same = (
                            workflow["title"] == payload["title"]
                            and revision["instructions"] == payload["instructions"]
                            and revision["sources"] == payload["sources"]
                            and all(
                                workflow["schedule"].get(k) == v
                                for k, v in payload["schedule"].items()
                            )
                        )
                        if not same:
                            raise ValueError(
                                "This message already created a different briefing. Inspect it and explicitly update it."
                            )
                    else:
                        workflow = await request("PUT", path, payload)
                    if action == "create":
                        workflow = await request(
                            "POST", path + "/run", {}, "briefing-first:" + identity
                        )
                    return json.dumps(
                        {
                            "status": "first_edition_requested" if action == "create" else "saved",
                            "workflow": workflow,
                        },
                        ensure_ascii=False,
                    )
                edition_id = changes.get("edition_id")
                if not edition_id or not any(
                    e["id"] == edition_id for e in workflow.get("editions", [])
                ):
                    raise ValueError("Choose an exact existing edition from inspect.")
                key = (
                    "briefing:"
                    + hashlib.sha256(
                        f"{identity}:{action}:{workflow_id}:{edition_id}".encode()
                    ).hexdigest()
                )
                if action == "regenerate":
                    workflow = await request("POST", path + "/run", {"edition_id": edition_id}, key)
                else:
                    comment = changes.get("feedback")
                    if not isinstance(comment, str) or not comment.strip():
                        raise ValueError("Edition feedback must contain a comment")
                    workflow = await request(
                        "POST", path + "/feedback", {"edition_id": edition_id, "text": comment}, key
                    )
                return json.dumps(
                    {
                        "status": "edition_requested"
                        if action == "regenerate"
                        else "feedback_saved",
                        "workflow": workflow,
                    },
                    ensure_ascii=False,
                )
        except ValueError as error:
            return "Error: " + str(error)
        except (OSError, httpx.HTTPError, KeyError, TypeError):
            # No provider responses, tokens or private identifiers in diagnostics.
            return "Error: The briefing operation was not confirmed. Inspect it before retrying. A saved workflow or queued edition may already exist."
