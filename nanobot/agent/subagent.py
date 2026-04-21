"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool

# Built-in agent type default prompts
_BUILTIN_TYPE_PROMPTS: dict[str, str] = {
    "code": (
        "You are a **code specialist** subagent.\n\n"
        "## Focus Areas\n"
        "- Code analysis, reading, writing, and refactoring\n"
        "- File I/O operations and directory exploration\n"
        "- Shell command execution for building, testing, and linting\n"
        "- Debugging and test execution\n\n"
        "## Guidelines\n"
        "- Always read existing code before modifying it\n"
        "- Run tests after making changes when possible\n"
        "- Provide clear explanations of code changes\n"
        "- Follow the project's existing coding style and conventions"
    ),
    "research": (
        "You are a **research specialist** subagent.\n\n"
        "## Focus Areas\n"
        "- Web search and information gathering\n"
        "- Fetching and reading web pages for detailed content\n"
        "- Synthesizing information from multiple sources\n"
        "- Summarizing findings clearly and concisely\n\n"
        "## Guidelines\n"
        "- Use web search to find relevant, up-to-date information\n"
        "- Cross-reference multiple sources when possible\n"
        "- Cite sources in your findings\n"
        "- Distinguish between facts and opinions"
    ),
    "security": (
        "You are a **security specialist** subagent.\n\n"
        "## Focus Areas\n"
        "- Security scanning and vulnerability analysis\n"
        "- Code review for security issues (injection, auth flaws, data leaks)\n"
        "- Dependency audit and known CVE checking\n"
        "- Configuration review for security best practices\n\n"
        "## Guidelines\n"
        "- Classify findings by severity (critical, high, medium, low)\n"
        "- Provide actionable remediation steps for each finding\n"
        "- Check for OWASP Top 10 vulnerabilities\n"
        "- Review file permissions, secrets in code, and auth mechanisms"
    ),
    "planner": (
        "You are a **planning specialist** subagent.\n\n"
        "## Focus Areas\n"
        "- Task decomposition and work breakdown\n"
        "- Architecture design and system planning\n"
        "- Dependency analysis and ordering\n"
        "- Estimation and risk identification\n\n"
        "## Guidelines\n"
        "- Break complex tasks into clear, actionable steps\n"
        "- Identify dependencies between tasks\n"
        "- Consider edge cases and potential blockers\n"
        "- Provide structured output (numbered lists, phases, milestones)"
    ),
}

# Default tool whitelists for built-in agent types (empty = all tools)
_BUILTIN_TYPE_TOOLS: dict[str, list[str]] = {
    "code": ["read_file", "write_file", "edit_file", "list_dir", "exec"],
    "research": ["web_search", "web_fetch", "read_file", "write_file"],
    "security": ["read_file", "list_dir", "exec", "web_search", "web_fetch"],
    "planner": ["read_file", "list_dir", "web_search", "web_fetch"],
}


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        agent_types: "dict[str, AgentTypeConfig] | None" = None,
    ):
        from nanobot.config.schema import ExecToolConfig, AgentTypeConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self._agent_types: dict[str, AgentTypeConfig] = agent_types or {}
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        agent_type: str = "default",
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id}

        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, agent_type)
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        type_suffix = f" (type: {agent_type})" if agent_type != "default" else ""
        logger.info("Spawned subagent [{}]{}: {}", task_id, type_suffix, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}, type: {agent_type}). I'll notify you when it completes."

    def _build_all_tools(self) -> ToolRegistry:
        """Build the full set of subagent tools (no message tool, no spawn tool)."""
        tools = ToolRegistry()
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
        tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        tools.register(WebSearchTool(api_key=self.brave_api_key))
        tools.register(WebFetchTool())
        return tools

    def _build_filtered_tools(self, whitelist: list[str]) -> ToolRegistry:
        """Build a ToolRegistry containing only the whitelisted tools."""
        all_tools = self._build_all_tools()
        if not whitelist:
            return all_tools
        filtered = ToolRegistry()
        for name in whitelist:
            tool = all_tools.get(name)
            if tool:
                filtered.register(tool)
            else:
                logger.warning("Agent type requested unknown tool '{}', skipping", name)
        return filtered

    def _resolve_type_config(self, agent_type: str) -> tuple[str, float, int, int, list[str], str]:
        """Resolve effective model, temperature, max_tokens, max_iterations, tools, and extra prompt for an agent type.

        Returns:
            (model, temperature, max_tokens, max_iterations, tool_whitelist, extra_prompt)
        """
        from nanobot.config.schema import AgentTypeConfig

        type_cfg: AgentTypeConfig | None = self._agent_types.get(agent_type)

        # Effective model / provider
        model = (type_cfg.model if type_cfg and type_cfg.model else self.model)
        temperature = (type_cfg.temperature if type_cfg and type_cfg.temperature is not None else self.temperature)
        max_tokens = (type_cfg.max_tokens if type_cfg and type_cfg.max_tokens is not None else self.max_tokens)
        max_iterations = (type_cfg.max_iterations if type_cfg else 15)

        # Tool whitelist: explicit config > built-in defaults > all tools
        if type_cfg and type_cfg.tools:
            tool_whitelist = type_cfg.tools
        elif agent_type in _BUILTIN_TYPE_TOOLS:
            tool_whitelist = _BUILTIN_TYPE_TOOLS[agent_type]
        else:
            tool_whitelist = []  # empty = all tools

        # Extra system prompt: explicit config > built-in defaults > none
        if type_cfg and type_cfg.system_prompt:
            extra_prompt = type_cfg.system_prompt
        elif agent_type in _BUILTIN_TYPE_PROMPTS:
            extra_prompt = _BUILTIN_TYPE_PROMPTS[agent_type]
        else:
            extra_prompt = ""

        return model, temperature, max_tokens, max_iterations, tool_whitelist, extra_prompt

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        agent_type: str = "default",
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task (type={}): {}", task_id, agent_type, label)

        try:
            # Resolve agent type configuration
            model, temperature, max_tokens, max_iterations, tool_whitelist, extra_prompt = (
                self._resolve_type_config(agent_type)
            )

            # Build tools — filtered if the type specifies a whitelist
            tools = self._build_filtered_tools(tool_whitelist)

            # Build messages with subagent-specific prompt
            system_prompt = self._build_subagent_prompt(task, agent_type, extra_prompt)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Run agent loop (limited iterations)
            iteration = 0
            final_result: str | None = None

            while iteration < max_iterations:
                iteration += 1

                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                if response.has_tool_calls:
                    # Add assistant message with tool calls
                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ]
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    # Execute tools
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
                        })
                else:
                    final_result = response.content
                    break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")
    
    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        # Sanitize subagent result — may contain attacker-controlled web content
        from nanobot.utils.security import sanitize_input
        sanitized_result, _ = sanitize_input(result, log_detections=True)

        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{sanitized_result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""
        
        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )
        
        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])
    
    def _build_subagent_prompt(
        self,
        task: str,
        agent_type: str = "default",
        extra_prompt: str = "",
    ) -> str:
        """Build a focused system prompt for the subagent.

        Args:
            task: The task description.
            agent_type: The agent type name (e.g. "code", "research").
            extra_prompt: Additional role-specific instructions to include.
        """
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"

        type_label = f" ({agent_type})" if agent_type != "default" else ""

        role_section = ""
        if extra_prompt:
            role_section = f"\n## Role\n{extra_prompt}\n"

        return f"""# Subagent{type_label}

## Current Time
{now} ({tz})

You are a subagent spawned by the main agent to complete a specific task.
{role_section}
## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}
Skills are available at: {self.workspace}/skills/ (read SKILL.md files as needed)

When you have completed the task, provide a clear summary of your findings or actions."""
    
    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
