"""Open a password protected attachment in the current private conversation."""
# pyright: reportIncompatibleMethodOverride=false
from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.webui.attachment_unlock import AttachmentVault, attachment_vault


@tool_parameters(tool_parameters_schema(
    attachment_id=StringSchema("Attachment ID from the password protected PDF note."),
    password=StringSchema("Password supplied by the user. Never guess or echo it.", max_length=1024),
    required=["attachment_id", "password"],
))
class UnlockAttachmentTool(Tool):
    def __init__(self, vault: AttachmentVault) -> None:
        self.vault = vault

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.sessions is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if ctx.sessions is None:
            raise RuntimeError("unlock_attachment requires sessions")
        return cls(attachment_vault(ctx.sessions.sessions_dir))

    @property
    def name(self) -> str:
        return "unlock_attachment"

    @property
    def description(self) -> str:
        return ("Unlock an attached password protected PDF with the user's password and return "
                "its text. Ask for the password first; if wrong ask again. At most 5 attempts "
                "per attachment. Never use shell commands for PDF passwords or echo passwords.")

    async def execute(self, attachment_id: str, password: str, **kwargs: Any) -> str:
        ctx = current_request_context()
        if ctx is None or ctx.channel != "websocket" or ctx.metadata.get("shared_room"):
            return ToolResult.error("Error: Attachment not found in this conversation.")
        result = await self.vault.unlock(ctx.chat_id, attachment_id, password)
        return ToolResult.error(result) if result.startswith("Error:") else result
