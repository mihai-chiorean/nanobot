"""Spawn tool for creating background subagents."""

from typing import Any, TYPE_CHECKING

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "Available agent types: 'default' (general-purpose), 'code' (code analysis, "
            "file I/O, shell execution, testing), 'research' (web search, content synthesis), "
            "'security' (vulnerability scanning, security audit), 'planner' (task decomposition, "
            "architecture planning). Custom types can be configured in agents.types."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
                "agent_type": {
                    "type": "string",
                    "description": (
                        "The type of specialized agent to spawn. "
                        "Built-in types: 'default', 'code', 'research', 'security', 'planner'. "
                        "Each type has a focused tool set and system prompt. "
                        "Defaults to 'default' (all tools available)."
                    ),
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, agent_type: str = "default", **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            session_key=self._session_key,
            agent_type=agent_type,
        )
