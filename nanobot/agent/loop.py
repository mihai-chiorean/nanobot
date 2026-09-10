"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from contextlib import AsyncExitStack, nullcontext, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.memory import Consolidator, Dream
from nanobot.agent.reasoning_policy import (
    parse_reasoning_profile,
    resolve_reasoning_profile,
)
from nanobot.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.ask import (
    AskUserTool,
    ask_user_options_from_messages,
    ask_user_outbound,
    ask_user_tool_result_messages,
    pending_ask_user_id,
)
from nanobot.agent.tools.audit import AuditLogger
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.notebook import NotebookEditTool
from nanobot.agent.tools.publish_file import (
    PublishFileTool,
    PublishFileTurn,
    bind_publish_file_turn,
    reset_publish_file_turn,
)
from nanobot.agent.tools.recall import IngestTool, RecallTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schedule_work import ScheduleWorkTool
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.agent.tools.self import MyTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.agent.tools.work import PublishArtifactTool, ReportProgressTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.chat_inbox import ChatInboxStore
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.config.schema import AgentDefaults
from nanobot.observability import observe_turn
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import ProviderSnapshot
from nanobot.providers.request_context import (
    reset_scheduling_class,
    set_scheduling_class,
)
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.document import extract_documents
from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.helpers import truncate_text as truncate_text_fn
from nanobot.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE
from nanobot.work.context import reset_work_context, set_work_context
from nanobot.work.store import WorkEvent, WorkStore

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, ToolsConfig, WebToolsConfig
    from nanobot.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"
_MAX_PERSISTED_REASONING_CHARS = 128 * 1024

# Only Mihai can modify system internals (architecture, skills, prompts, code)
OWNER_USER_ID = "1476998117906845890"

# Patterns that indicate system modification intent (checked against user messages)
_SYSTEM_MOD_PATTERNS = [
    # Architecture
    r"update\s+architecture", r"modify\s+architecture", r"change\s+architecture",
    # Skills / plugins
    r"(add|install|create|remove|delete|uninstall)\s+(a\s+)?(skill|plugin|extension|tool)",
    # System prompts / personality
    r"(change|modify|update|edit|rewrite)\s+(your|the|ziggy.s?|nanobot.s?)?\s*(system\s+prompt|personality|soul|identity|instructions|behavior)",
    r"(change|modify|update)\s+.*SOUL\.md",
    r"(change|modify|update)\s+.*AGENTS\.md",
    # Code changes to nanobot itself
    r"(change|modify|edit|update|rewrite)\s+(your|the|ziggy.s?|nanobot.s?)?\s*(source\s+)?code",
]

_compiled_system_mod = [re.compile(p, re.IGNORECASE) for p in _SYSTEM_MOD_PATTERNS]


def is_system_modification(content: str) -> bool:
    """Check if message requests system-level changes (skills, prompts, code, config)."""
    text = content.strip()
    for pattern in _compiled_system_mod:
        if pattern.search(text):
            return True
    return False


class _LoopHook(AgentHook):
    """Core hook for the main loop."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._loop = agent_loop
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._on_status = on_status
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._metadata = metadata or {}
        self._session_key = session_key
        self._stream_buf = ""

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        from nanobot.utils.helpers import strip_think

        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]
        if incremental and self._on_stream:
            await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._loop._current_iteration = context.iteration
        if self._on_status is not None:
            from nanobot.utils.helpers import pick_thinking_emoji
            emoji = pick_thinking_emoji()
            label = "Getting started..." if context.iteration == 0 else "Thinking..."
            await self._on_status(f"{emoji} {label}")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream and not context.streamed_content:
                thought = self._loop._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._loop._strip_think(self._loop._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            await invoke_on_progress(
                self._on_progress,
                tool_hint,
                tool_hint=True,
                tool_events=tool_events,
            )
        if self._on_status is not None and context.tool_calls:
            from nanobot.utils.helpers import pick_tool_emoji, summarize_tool_call
            emoji = pick_tool_emoji()
            tc0 = context.tool_calls[0]
            summary = summarize_tool_call(tc0.name, tc0.arguments or {})
            if len(context.tool_calls) > 1:
                summary += f" (+{len(context.tool_calls) - 1} more)"
            await self._on_status(f"{emoji} {summary}")
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        self._loop._set_tool_context(
            self._channel,
            self._chat_id,
            self._message_id,
            self._metadata,
            session_key=self._session_key,
        )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await invoke_on_progress(
                    self._on_progress,
                    "",
                    tool_hint=False,
                    tool_events=tool_events,
                )
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )
        # Ziggy: Prometheus metrics + audit log for each LLM call
        response = context.response
        latency_ms = context.latency_ms
        if latency_ms is not None:
            try:
                from nanobot.dashboard.server import _PROM_AVAILABLE
                if _PROM_AVAILABLE:
                    from nanobot.dashboard.server import PROM_LLM_DURATION
                    PROM_LLM_DURATION.observe(latency_ms)
            except Exception:
                pass
        try:
            self._loop._audit_logger.log_llm_call(
                session_id=self._chat_id,
                channel=self._channel,
                model=self._loop.model,
                tokens_in=u.get("prompt_tokens"),
                tokens_out=u.get("completion_tokens"),
                latency_ms=latency_ms,
                ttft_ms=getattr(response, "ttft_ms", None) if response else None,
            )
        except Exception:
            pass
        # Update status with the last reasoning sentence when available — but
        # only for tool-calling iterations (tool_calls is non-empty), i.e. the
        # loop is continuing.  For final-content iterations the content stream
        # is already flowing into the Discord buf; clobbering it here would
        # replace partial response text with the reasoning snippet.
        # Reasoning arrives as a complete string post-LLM (not incrementally)
        # for both MiniMax via custom_provider and openai_compat_provider.
        if self._on_status is not None and response is not None and context.tool_calls:
            rc = getattr(response, "reasoning_content", None)
            if rc:
                from nanobot.utils.helpers import extract_latest_sentence, pick_thinking_emoji
                sentence = extract_latest_sentence(rc)
                if sentence:
                    emoji = pick_thinking_emoji()
                    try:
                        await self._on_status(f"{emoji} {sentence}")
                    except Exception:
                        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._loop._strip_think(content)


class _WorkHook(AgentHook):
    """Persist tool and progress events for one Work task."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        *,
        task_id: str,
        channel: str,
        chat_id: str,
    ) -> None:
        super().__init__()
        self._loop = agent_loop
        self._task_id = task_id
        self._channel = channel
        self._chat_id = chat_id
        self._last_published_seq = 0

    async def _publish(self, event: WorkEvent | dict[str, Any] | None) -> None:
        if event is not None:
            await self._loop._publish_work_event(self._channel, self._chat_id, event)
            data = event.to_api() if hasattr(event, "to_api") else event
            seq = data.get("seq") if isinstance(data, dict) else None
            if isinstance(seq, int):
                self._last_published_seq = max(self._last_published_seq, seq)

    async def before_iteration(self, context: AgentHookContext) -> None:
        if context.iteration == 0:
            event = await self._loop.work_store.run_io(
                self._loop.work_store.update_status, self._task_id, "running"
            )
            await self._publish(event)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            event = await self._loop.work_store.run_io(
                self._loop.work_store.append_event,
                self._task_id,
                "tool.started",
                {"name": tool_call.name, "arguments": tool_call.arguments or {}},
                actor="main_agent",
            )
            await self._publish(event)

    async def after_iteration(self, context: AgentHookContext) -> None:
        # Work tools append step and artifact events synchronously. Replay any
        # events they added before emitting the generic tool completion rows.
        events = await self._loop.work_store.run_io(
            self._loop.work_store.list_events,
            self._task_id,
            after_seq=self._last_published_seq,
        )
        for event in events:
            await self._publish(event)
        for tool_event in context.tool_events or []:
            event = await self._loop.work_store.run_io(
                self._loop.work_store.append_event,
                self._task_id,
                "tool.finished",
                dict(tool_event),
                actor="main_agent",
            )
            await self._publish(event)
        if context.stop_reason == "ask_user":
            event = await self._loop.work_store.run_io(
                self._loop.work_store.update_status,
                self._task_id,
                "waiting",
                result_summary=context.final_content,
            )
            await self._publish(event)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    _MAX_CHAT_PROCESSING_RETRIES = 5
    _TERMINAL_CHAT_RETRY = -1

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        web_config: WebToolsConfig | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        provider_snapshot_loader: Callable[[], ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig, ToolsConfig, WebToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._provider_signature = provider_signature
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.web_config = web_config or WebToolsConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.rag_config = _tc.rag
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(
            workspace,
            timezone=timezone,
            disabled_skills=disabled_skills,
            workflow_scheduling=cron_service is not None or _tc.briefing.enable,
        )
        self.sessions = session_manager or SessionManager(workspace)
        self.chat_inbox = ChatInboxStore(workspace)
        self.work_store = WorkStore(workspace)
        self.tools = ToolRegistry()
        self._briefing_config = _tc.briefing
        self._audit_logger = AuditLogger(self.workspace / "audit.jsonl")
        self.tools.set_audit_logger(self._audit_logger)
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_config=self.web_config,
            max_tool_result_chars=self.max_tool_result_chars,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
        )
        self._register_default_tools()
        if _tc.my.enable:
            self.tools.register(MyTool(loop=self, modify_allowed=_tc.my.allow_set))
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(self, snapshot: ProviderSnapshot) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        if self.provider is provider and self.model == model:
            return
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self.dream.set_provider(provider, model)
        self._provider_signature = snapshot.signature
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        if snapshot.signature == self._provider_signature:
            return
        self._apply_provider_snapshot(snapshot)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = (
            self.workspace if (self.restrict_to_workspace or self.exec_config.sandbox) else None
        )
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(AskUserTool())
        self.tools.register(
            ReadFileTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                extra_allowed_dirs=extra_read,
            )
        )
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        for cls in (GlobTool, GrepTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(NotebookEditTool(workspace=self.workspace, allowed_dir=allowed_dir))
        if self.exec_config.enable:
            self.tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    sandbox=self.exec_config.sandbox,
                    path_append=self.exec_config.path_append,
                    allowed_env_keys=self.exec_config.allowed_env_keys,
                    allow_loopback=self.exec_config.allow_loopback,
                )
            )
        if self.web_config.enable:
            self.tools.register(
                WebSearchTool(
                    config=self.web_config.search,
                    proxy=self.web_config.proxy,
                    user_agent=self.web_config.user_agent,
                )
            )
            self.tools.register(
                WebFetchTool(
                    config=self.web_config.fetch,
                    proxy=self.web_config.proxy,
                    user_agent=self.web_config.user_agent,
                )
            )
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound, workspace=self.workspace))
        self.tools.register(SpawnTool(manager=self.subagents))
        self.tools.register(ReportProgressTool())
        self.tools.register(PublishArtifactTool())
        self.tools.register(PublishFileTool(workspace=self.workspace))
        if self._briefing_config.enable:
            from nanobot.agent.tools.briefing import BriefingTool

            self.tools.register(BriefingTool(self._briefing_config, self.sessions))
        if self.cron_service:
            self.tools.register(
                CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC")
            )
            self.tools.register(
                ScheduleWorkTool(
                    self.cron_service,
                    self.work_store,
                    default_timezone=self.context.timezone or "UTC",
                    model_name=self.model,
                )
            )
        # Ziggy: opt-in semantic recall. Ingest is always workspace-bounded,
        # independent of the broader filesystem tool policy.
        if self.rag_config.enable:
            self.tools.register(RecallTool(workspace=self.workspace))
            self.tools.register(IngestTool(workspace=self.workspace, allowed_dir=self.workspace))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
            else:
                logger.warning("No MCP servers connected successfully (will retry next message)")
        except asyncio.CancelledError:
            logger.warning("MCP connection cancelled (will retry next message)")
            self._mcp_stacks.clear()
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        # When the caller threads a thread-scoped session_key (e.g. slack with
        # reply_in_thread: true), honor it so spawn announces route back to
        # the originating thread session. Falls back to unified mode or
        # channel:chat_id for callers that don't have a thread-scoped key.
        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"
        for name in ("message", "spawn", "cron", "schedule_work", "briefing", "my"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    if name == "spawn":
                        tool.set_context(channel, chat_id, effective_key=effective_key)
                        if hasattr(tool, "set_origin_message_id"):
                            tool.set_origin_message_id(message_id)
                    elif name == "briefing":
                        tool.set_context(channel, chat_id, metadata=metadata, session_key=session_key, message_id=message_id)
                    elif name in {"cron", "schedule_work"}:
                        tool.set_context(channel, chat_id, metadata=metadata, session_key=session_key)
                    elif name == "message":
                        tool.set_context(channel, chat_id, message_id, metadata=metadata)
                    else:
                        tool.set_context(channel, chat_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from nanobot.utils.helpers import strip_think

        return strip_think(text) or None

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hints with smart abbreviation."""
        from nanobot.utils.tool_hints import format_tool_hints

        return format_tool_hints(tool_calls)

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        msg.metadata["_command_at_most_once"] = True
        try:
            command_started = await self._begin_at_most_once_command(msg)
        except Exception:
            logger.exception(
                "Could not persist at-most-once marker for command '{}'",
                raw,
            )
            msg.metadata.pop("_command_at_most_once", None)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._command_not_started_message(),
                metadata=dict(msg.metadata or {}),
            ))
            return
        if not command_started:
            msg.metadata.pop("_command_at_most_once", None)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._command_failure_message(),
                metadata=dict(msg.metadata or {}),
            ))
            return
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        try:
            result = await dispatch_fn(ctx)
        except Exception:
            logger.exception("At-most-once command '{}' failed during dispatch", raw)
            await self._record_command_failure_safely(msg)
            msg.metadata.pop("_command_at_most_once", None)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._command_failure_message(),
                metadata=dict(msg.metadata or {}),
            ))
            return
        await self._complete_at_most_once_command(msg, result)
        msg.metadata.pop("_command_at_most_once", None)
        if result:
            result.metadata.pop("_command_at_most_once", None)
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def active_session_keys(self) -> set[str]:
        """Return sessions with agent or subagent work currently executing."""
        return {
            key
            for key, tasks in self._active_tasks.items()
            if any(not task.done() for task in tasks)
        }

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    @staticmethod
    def _work_task_id(metadata: dict[str, Any] | None) -> str | None:
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("work_task_id")
        return value if isinstance(value, str) and value.startswith("work_") else None

    async def _publish_work_event(
        self, channel: str, chat_id: str, event: WorkEvent | dict[str, Any]
    ) -> None:
        payload = event.to_api() if hasattr(event, "to_api") else event
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                metadata={"_work_event": payload},
            )
        )

    async def _record_work_status(
        self,
        msg: InboundMessage,
        status: str,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        task_id = self._work_task_id(msg.metadata)
        if task_id is None:
            return
        event = await self.work_store.run_io(
            self.work_store.update_status,
            task_id,
            status,
            error=error,
            result_summary=result_summary,
        )
        if event is not None:
            await self._publish_work_event(msg.channel, msg.chat_id, event)

    def _work_artifact_refs(self, msg: InboundMessage) -> list[dict[str, Any]]:
        task_id = self._work_task_id(msg.metadata)
        if task_id is None:
            return []
        refs: list[dict[str, Any]] = []
        for artifact in self.work_store.list_artifacts(task_id):
            artifact_id = artifact.get("artifact_id")
            if isinstance(artifact_id, str):
                refs.append(
                    {
                        "artifact_id": artifact_id,
                        "name": artifact.get("name"),
                        "kind": artifact.get("kind"),
                        "mime": artifact.get("mime"),
                    }
                )
        return refs

    def _attach_work_artifacts_to_last_assistant(
        self, session: Session, artifact_refs: list[dict[str, Any]]
    ) -> None:
        if not artifact_refs:
            return
        for entry in reversed(session.messages):
            if not isinstance(entry, dict) or entry.get("role") != "assistant":
                continue
            existing = entry.get("work_artifacts")
            if isinstance(existing, list):
                known = {
                    item.get("artifact_id")
                    for item in existing
                    if isinstance(item, dict)
                }
                entry["work_artifacts"] = [
                    *existing,
                    *[
                        item
                        for item in artifact_refs
                        if item.get("artifact_id") not in known
                    ],
                ]
            else:
                entry["work_artifacts"] = list(artifact_refs)
            session.updated_at = datetime.now()
            self.sessions.save(session)
            return

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        publish_file_turn: PublishFileTurn | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        loop_hook = _LoopHook(
            self,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_status=on_status,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
        )
        task_id = self._work_task_id(metadata)
        hooks: list[AgentHook] = [loop_hook]
        if task_id:
            hooks.append(
                _WorkHook(
                    self,
                    task_id=task_id,
                    channel=channel,
                    chat_id=chat_id,
                )
            )
        hooks.extend(self._extra_hooks)
        hook: AgentHook = CompositeHook(hooks) if len(hooks) > 1 else loop_hook

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = extract_documents(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                runtime_ctx = self.context._build_runtime_context(
                    pending_msg.channel,
                    self._runtime_chat_id(pending_msg),
                    self.context.timezone,
                )
                if isinstance(user_content, str):
                    merged: str | list[dict[str, Any]] = f"{runtime_ctx}\n\n{user_content}"
                else:
                    merged = [{"type": "text", "text": runtime_ctx}] + user_content
                result: dict[str, Any] = {"role": "user", "content": merged}
                client_message_id = pending_msg.metadata.get("client_message_id")
                if isinstance(client_message_id, str):
                    result["_client_message_ids"] = [client_message_id]
                return result

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        run_tools = (
            ToolRegistry()
            if isinstance(metadata, dict) and metadata.get("shared_room")
            else self.tools
        )
        run_reasoning_effort = None
        run_max_tokens = None
        run_temperature = None
        run_reasoning_profile = None
        run_scheduling_class = "foreground"
        background_work = False
        allow_reasoning_escalation = False
        if isinstance(metadata, dict):
            background_work = metadata.get("work_mode") in {"background", "scheduled"}
            if background_work:
                run_scheduling_class = "background"
            requested_profile = parse_reasoning_profile(metadata.get("reasoning_profile"))
            uses_raw_generation_controls = (
                "reasoning_effort" in metadata or "max_tokens" in metadata
            )
            if requested_profile is not None and not uses_raw_generation_controls:
                decision = resolve_reasoning_profile(
                    requested_profile,
                    initial_messages,
                    background_work=background_work,
                )
                run_reasoning_profile = decision.generation.name.value
                run_reasoning_effort = decision.generation.reasoning_effort
                run_temperature = decision.generation.temperature
                run_max_tokens = decision.generation.max_tokens
                allow_reasoning_escalation = decision.allow_escalation
                logger.info(
                    "Reasoning profile requested={} resolved={} source={} classifier_candidate={}",
                    decision.requested.value,
                    decision.generation.name.value,
                    decision.source,
                    decision.classifier_candidate,
                )
            else:
                candidate_effort = metadata.get("reasoning_effort")
                if isinstance(candidate_effort, str):
                    run_reasoning_effort = candidate_effort
                candidate_max_tokens = metadata.get("max_tokens")
                if isinstance(candidate_max_tokens, int) and not isinstance(
                    candidate_max_tokens, bool
                ):
                    run_max_tokens = candidate_max_tokens
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        scheduling_token = set_scheduling_class(run_scheduling_class)
        work_tokens = set_work_context(
            store=self.work_store if task_id else None,
            task_id=task_id,
            workspace=self.workspace if task_id else None,
        )
        publication_token = (
            bind_publish_file_turn(publish_file_turn)
            if publish_file_turn is not None
            else None
        )
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=run_tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=self.workspace,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                temperature=run_temperature,
                reasoning_effort=run_reasoning_effort,
                max_tokens=run_max_tokens,
                reasoning_profile=run_reasoning_profile,
                allow_reasoning_escalation=allow_reasoning_escalation,
                progress_callback=on_progress,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
            ))
        finally:
            if publication_token is not None:
                reset_publish_file_turn(publication_token)
            reset_work_context(work_tokens)
            reset_scheduling_class(scheduling_token)
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._recover_interrupted_commands()
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, msg.session_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                self._pending_queues[effective_key].put_nowait(pending_msg)
                logger.info(
                    "Routed follow-up message to pending queue for session {}",
                    effective_key,
                )
                continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        # Ziggy: guard — only owner can request system modifications
        if is_system_modification(msg.content):
            if msg.sender_id != OWNER_USER_ID:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="I can't make changes to my own system, skills, or configuration. "
                            "Only my owner can authorize that. Let me know if there's something "
                            "else I can help you with!",
                ))
                await self._mark_chat_message_processed(msg)
                return

        # Register a pending queue so follow-up messages for this session are
        # routed here (mid-turn injection) instead of spawning a new task.
        pending: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._pending_queues[session_key] = pending
        retry_delay: int | None = None

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = on_status = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                        async def on_status(text: str) -> None:
                            # _status_delta is keyed by chat_id only (no _stream_id)
                            # so the status message survives tool-call segment boundaries.
                            meta = dict(msg.metadata or {})
                            meta["_status_delta"] = True
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=text,
                                metadata=meta,
                            ))

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        on_status=on_status,
                        pending_queue=(
                            None if msg.metadata.get("shared_room") else pending
                        ),
                    )
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    if msg.metadata.get("_command_at_most_once"):
                        try:
                            await self._record_command_failure(msg)
                        except Exception:
                            logger.exception(
                                "Could not persist cancelled command outcome for session {}",
                                session_key,
                            )
                    await self._record_work_status(msg, "cancelled")
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self._record_work_status(
                        msg,
                        "failed",
                        error="Sorry, I encountered an error.",
                    )
                    is_command_failure = bool(
                        msg.metadata.get("_command_at_most_once")
                    )
                    retry_delay = (
                        None
                        if is_command_failure
                        else await self._prepare_chat_message_retry(msg)
                    )
                    if is_command_failure:
                        await self._record_command_failure_safely(msg)
                        msg.metadata.pop("_command_at_most_once", None)
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=self._command_failure_message(),
                            metadata=dict(msg.metadata or {}),
                        ))
                    elif retry_delay is None:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="Sorry, I encountered an error.",
                        ))
                    elif retry_delay == self._TERMINAL_CHAT_RETRY:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=self._terminal_chat_failure_message(),
                            metadata=dict(msg.metadata or {}),
                        ))
                    else:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=f"Retrying in {retry_delay} seconds.",
                            metadata={
                                **dict(msg.metadata or {}),
                                "_status_delta": True,
                            },
                        ))
        finally:
            # Keep the pending queue registered while backing off so later
            # prompts remain behind the failed one without holding a model
            # concurrency slot or a session lock.
            while retry_delay is not None and retry_delay >= 0:
                await asyncio.sleep(retry_delay)
                retry_delay = await self._retry_chat_message_in_place(
                    msg,
                    session_key,
                    pending,
                    lock,
                )
            # Drain any messages still in the pending queue and re-publish
            # them to the bus so they are processed as fresh inbound messages
            # rather than silently lost.
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover, session_key,
                    )

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        MIT-202: the entire message processing is wrapped in a Langfuse
        root span so every LLM call, tool dispatch, and subagent task
        for this turn shares a single ``trace_id`` with ``session_id``
        and ``user_id`` propagated to every child observation.
        """
        effective_key = session_key or msg.session_key
        turn_channel = msg.channel if msg.channel != "system" else (
            msg.chat_id.split(":", 1)[0] if ":" in msg.chat_id else "cli"
        )
        turn_chat_id = msg.chat_id if msg.channel != "system" else (
            msg.chat_id.split(":", 1)[1] if ":" in msg.chat_id else msg.chat_id
        )
        turn_input = msg.content[:200] if isinstance(msg.content, str) else None
        with observe_turn(
            name=f"turn:{turn_channel}",
            session_id=effective_key,
            user_id=msg.sender_id,
            channel=turn_channel,
            chat_id=turn_chat_id,
            input_preview=turn_input,
            tags=["ziggy", f"channel:{turn_channel}"],
        ):
            return await self._process_message_impl(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                on_status=on_status,
                pending_queue=pending_queue,
            )

    async def _process_message_impl(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Original processing body; see ``_process_message`` for the trace wrapper."""
        self._refresh_provider_snapshot()
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            # Honor session_key_override so subagent announces from threaded
            # callers route to the originating thread session, not the
            # channel-level session derived from chat_id.
            key = msg.session_key_override or f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            if self._restore_runtime_checkpoint(session):
                self.sessions.save(session)
            if self._restore_pending_user_turn(session):
                self.sessions.save(session)

            session, pending = self.auto_compact.prepare_session(session, key)

            await self.consolidator.maybe_consolidate_by_tokens(
                session,
                session_summary=pending,
            )
            # Persist subagent follow-ups into durable history BEFORE prompt
            # assembly. ContextBuilder merges adjacent same-role messages for
            # provider compatibility, which previously caused the follow-up to
            # disappear from session.messages while still being visible to the
            # LLM via the merged prompt. See _persist_subagent_followup.
            is_subagent = msg.sender_id == "subagent"
            if is_subagent and self._persist_subagent_followup(session, msg):
                self.sessions.save(session)
            self._set_tool_context(
                channel, chat_id, msg.metadata.get("message_id"),
                msg.metadata, session_key=key,
            )
            _hist_kwargs: dict[str, Any] = {
                "max_messages": self._max_messages,
                "max_tokens": self._replay_token_budget(),
                "include_timestamps": True,
            }
            history = session.get_history(**_hist_kwargs)
            current_role = "assistant" if is_subagent else "user"

            # Subagent content is already in `history` above; passing it again
            # as current_message would double-project it into the prompt.
            messages = self.context.build_messages(
                history=history,
                current_message="" if is_subagent else msg.content,
                channel=channel,
                chat_id=chat_id,
                session_summary=pending,
                current_role=current_role,
                sender_id=msg.sender_id,
            )
            final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
                messages, session=session, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
                metadata=msg.metadata,
                session_key=key,
                pending_queue=pending_queue,
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
            self._clear_runtime_checkpoint(session)
            self.sessions.save(session)
            self._schedule_background(self.consolidator.maybe_consolidate_by_tokens(session))
            options = ask_user_options_from_messages(all_msgs) if stop_reason == "ask_user" else []
            content, buttons = ask_user_outbound(
                final_content or "Background task completed.",
                options,
                channel,
            )
            # Reconstruct channel-specific metadata from session.key so the
            # outbound reply lands in the originating thread (not the channel
            # top-level). The announce InboundMessage carries only
            # injected_event metadata; we recover thread_ts from the session
            # key, which slack writes as "slack:<chat_id>:<thread_ts>".
            outbound_metadata: dict[str, Any] = {}
            if channel == "slack" and key.startswith("slack:") and key.count(":") >= 2:
                outbound_metadata["slack"] = {"thread_ts": key.split(":", 2)[2]}
            if origin_message_id := msg.metadata.get("origin_message_id"):
                outbound_metadata["origin_message_id"] = origin_message_id
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=content,
                buttons=buttons,
                metadata=outbound_metadata,
            )

        # Extract document text from media at the processing boundary so all
        # channels benefit without format-specific logic in ContextBuilder.
        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            msg = dataclasses.replace(msg, content=new_content, media=image_only)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        shared_room = bool(msg.metadata.get("shared_room"))
        client_message_id = msg.metadata.get("client_message_id")
        has_client_message = (
            isinstance(client_message_id, str)
            and self._session_has_client_message(session, client_message_id)
        )
        pending_client_message_ids = self._pending_user_turn_client_message_ids(session)
        resumes_pending_receipt = bool(
            has_client_message
            and isinstance(client_message_id, str)
            and client_message_id in pending_client_message_ids
        )
        if has_client_message and not resumes_pending_receipt:
            logger.info(
                "Skipping duplicate client message {} for session {}",
                client_message_id,
                key,
            )
            await self._mark_chat_message_processed(msg)
            return None
        if resumes_pending_receipt and isinstance(
            session.metadata.get(self._RUNTIME_CHECKPOINT_KEY),
            dict,
        ):
            self._restore_runtime_checkpoint(session)
            interrupted = (
                "The server restarted while this request was using tools. "
                "I preserved the partial result and did not repeat those tool calls."
            )
            session.add_message("assistant", interrupted)
            self.sessions.save(session)
            await self._mark_chat_message_processed(msg)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=interrupted,
                metadata=dict(msg.metadata or {}),
            )
        if not resumes_pending_receipt:
            if self._restore_runtime_checkpoint(session):
                self.sessions.save(session)
            if self._restore_pending_user_turn(session):
                self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)

        # Slash commands
        raw = msg.content.strip()
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        is_known_command = (
            not shared_room and self.commands.is_dispatchable_command(raw)
        )
        if is_known_command:
            msg.metadata["_command_at_most_once"] = True
            if not await self._begin_at_most_once_command(msg):
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self._command_failure_message(),
                    metadata=dict(msg.metadata or {}),
                )
        if not shared_room and (result := await self.commands.dispatch(ctx)):
            if is_known_command:
                await self._complete_at_most_once_command(msg, result)
                msg.metadata.pop("_command_at_most_once", None)
                result.metadata.pop("_command_at_most_once", None)
            else:
                await self._mark_chat_message_processed(msg)
            return result

        if not shared_room:
            await self.consolidator.maybe_consolidate_by_tokens(
                session,
                session_summary=pending,
            )

            self._set_tool_context(
                msg.channel, msg.chat_id, msg.metadata.get("message_id"),
                msg.metadata, session_key=key,
            )
            if message_tool := self.tools.get("message"):
                if isinstance(message_tool, MessageTool):
                    message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        if resumes_pending_receipt and isinstance(client_message_id, str):
            last_persisted = session.messages[-1] if session.messages else None
            if (
                history
                and isinstance(last_persisted, dict)
                and last_persisted.get("role") == "user"
                and self._message_has_client_id(last_persisted, client_message_id)
                and history[-1].get("role") == "user"
            ):
                history = history[:-1]

        pending_ask_id = None if shared_room else pending_ask_user_id(history)
        if pending_ask_id:
            initial_messages = ask_user_tool_result_messages(
                self.context.build_system_prompt(channel=msg.channel),
                history,
                pending_ask_id,
                msg.content,
            )
            if isinstance(client_message_id, str):
                initial_messages[-1]["_client_message_ids"] = [client_message_id]
        else:
            initial_messages = self.context.build_messages(
                history=history,
                current_message=msg.content,
                session_summary=pending,
                media=msg.media if msg.media else None,
                channel=msg.channel,
                chat_id=self._runtime_chat_id(msg),
                sender_id=msg.sender_id,
                shared_room=shared_room,
                participant_display_name=(
                    str(msg.metadata.get("participant_display_name") or "").strip()
                    or None
                ),
            )
        if isinstance(client_message_id, str):
            for message in reversed(initial_messages):
                if message.get("role") == "user":
                    message["_client_message_ids"] = [client_message_id]
                    break

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict[str, Any]] | None = None,
        ) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            if tool_events:
                from nanobot.utils.activity_history import record_tool_activity
                record_tool_activity(session, tool_events)
                self.sessions.save(session, fsync=True)
                meta["_tool_events"] = tool_events
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        # Persist the triggering user message up front so a mid-turn crash
        # doesn't silently lose the prompt on recovery. ``media`` rides along
        # as raw on-disk paths — sanitized image blocks are stripped from
        # JSONL, and webui replay needs the paths to mint signed URLs.
        user_persisted_early = resumes_pending_receipt
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if not resumes_pending_receipt and not pending_ask_id and (has_text or media_paths):
            extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
            if isinstance(client_message_id, str):
                extra["client_message_id"] = client_message_id
            if shared_room:
                for field in ("participant_id", "participant_display_name"):
                    value = msg.metadata.get(field)
                    if isinstance(value, str) and value:
                        extra[field] = value
                extra["shared_room"] = True
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(
                session,
                [client_message_id] if isinstance(client_message_id, str) else [],
            )
            self.sessions.save(session)
            user_persisted_early = True

        publish_file_turn = PublishFileTurn(
            self.sessions,
            key,
            # Publish links are a WebSocket-client capability. Background
            # Work may explicitly opt in so reports tied to its own session
            # can be handed back through the same authenticated route.
            enabled=(
                not shared_room
                and (
                    msg.channel == "websocket"
                    or msg.metadata.get("work_mode") in {"background", "scheduled"}
                )
            ),
        )
        persisted_before = len(session.messages)
        # ContextBuilder may merge the current user message into a preceding
        # discussion message. Capture the actual boundary before the loop can
        # append to it; assuming history + user would skip the first reply.
        initial_message_count = len(initial_messages)
        final_content, _, all_msgs, stop_reason, had_injections = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_status=on_status,
            on_retry_wait=_on_retry_wait,
            session=session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
            publish_file_turn=publish_file_turn,
        )

        if final_content is None or not final_content.strip():
            final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        # Skip the already-persisted user message when saving the turn
        save_skip = initial_message_count - (0 if user_persisted_early else 1)
        self._save_turn(session, all_msgs, save_skip)
        self.sessions.grant_published_files(
            session,
            publish_file_turn.publications,
            message_start=persisted_before,
        )
        session.enforce_file_cap(
            on_archive=None if shared_room else self.context.memory.raw_archive
        )
        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        completed_client_message_ids = self._client_message_ids(all_msgs)
        if isinstance(client_message_id, str):
            completed_client_message_ids.insert(0, client_message_id)
        await self._mark_chat_message_ids_processed(
            msg.chat_id,
            list(dict.fromkeys(completed_client_message_ids)),
        )
        if not shared_room:
            self._schedule_background(
                self.consolidator.maybe_consolidate_by_tokens(session)
            )

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        final_content, buttons = ask_user_outbound(
            final_content,
            ask_user_options_from_messages(all_msgs) if stop_reason == "ask_user" else [],
            msg.channel,
        )
        if on_stream is not None and stop_reason not in {"ask_user", "error"}:
            # WebSocket consumers need an explicit final payload after the
            # stream boundary (which may also precede ask_user or a retry).
            meta["_streamed"] = not (msg.channel == "websocket" and msg.metadata.get("explicit_final_message") is True)
            # max_iterations already fires on_stream_end; emit it for the
            # normal "completed" path so Discord's _finalize_stream runs and
            # typing stops.
            if on_stream_end is not None and stop_reason != "max_iterations":
                await on_stream_end(resuming=False)
        if stop_reason == "ask_user":
            await self._record_work_status(msg, "waiting", result_summary=final_content)
        elif stop_reason in {"error", "tool_error", "max_iterations", "empty_final_response", "incomplete_response"}:
            await self._record_work_status(
                msg,
                "failed",
                error=final_content,
                result_summary=final_content,
            )
        else:
            await self._record_work_status(
                msg,
                "succeeded",
                result_summary=final_content,
            )
            artifact_refs = self._work_artifact_refs(msg)
            if artifact_refs:
                meta["_work_artifacts"] = artifact_refs
                self._attach_work_artifacts_to_last_assistant(session, artifact_refs)

        # When follow-up messages were injected mid-turn, a later natural
        # language reply may address those follow-ups and should not be
        # suppressed just because MessageTool was used earlier in the turn.
        # However, if the turn falls back to the empty-final-response
        # placeholder, suppress it when the real user-visible output already
        # came from MessageTool.
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
            buttons=buttons,
        )

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        for m in messages[skip:]:
            entry = dict(m)
            client_message_ids = self._normalized_client_message_ids(
                entry.pop("_client_message_ids", None)
            )
            if len(client_message_ids) == 1:
                entry["client_message_id"] = client_message_ids[0]
            elif client_message_ids:
                entry["client_message_ids"] = client_message_ids
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "assistant":
                reasoning = entry.get("reasoning_content")
                if (
                    isinstance(reasoning, str)
                    and len(reasoning) > _MAX_PERSISTED_REASONING_CHARS
                ):
                    entry["reasoning_content"] = (
                        "[Earlier reasoning omitted from persisted history.]\n"
                        + reasoning[-_MAX_PERSISTED_REASONING_CHARS:]
                    )
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the entire runtime-context block (including any session summary).
                    # The block is bounded by _RUNTIME_CONTEXT_TAG and _RUNTIME_CONTEXT_END.
                    end_marker = ContextBuilder._RUNTIME_CONTEXT_END
                    end_pos = content.find(end_marker)
                    if end_pos >= 0:
                        after = content[end_pos + len(end_marker):].lstrip("\n")
                        if after:
                            entry["content"] = after
                        else:
                            continue
                    else:
                        # Fallback: no end marker found, strip the tag prefix
                        after_tag = content[len(ContextBuilder._RUNTIME_CONTEXT_TAG):].lstrip("\n")
                        if after_tag.strip():
                            entry["content"] = after_tag
                        else:
                            continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    @staticmethod
    def _session_has_client_message(session: Session, client_message_id: str) -> bool:
        return any(
            AgentLoop._message_has_client_id(message, client_message_id)
            for message in session.messages
            if isinstance(message, dict)
        )

    @staticmethod
    def _message_has_client_id(
        message: dict[str, Any],
        client_message_id: str,
    ) -> bool:
        return (
            message.get("client_message_id") == client_message_id
            or client_message_id
            in AgentLoop._normalized_client_message_ids(
                message.get("client_message_ids")
            )
        )

    @staticmethod
    def _normalized_client_message_ids(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(item for item in value if isinstance(item, str)))

    @classmethod
    def _client_message_ids(cls, messages: list[dict[str, Any]]) -> list[str]:
        result: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            result.extend(
                cls._normalized_client_message_ids(message.get("_client_message_ids"))
            )
        return list(dict.fromkeys(result))

    async def _mark_chat_message_processed(self, msg: InboundMessage) -> None:
        client_message_id = msg.metadata.get("client_message_id")
        if not isinstance(client_message_id, str):
            return
        await self._mark_chat_message_ids_processed(msg.chat_id, [client_message_id])

    async def _begin_at_most_once_command(self, msg: InboundMessage) -> bool:
        """Durably prevent automatic replay before dispatching a command."""
        client_message_id = msg.metadata.get("client_message_id")
        if not isinstance(client_message_id, str):
            return True
        return await self.chat_inbox.mark_command_started(
            msg.chat_id,
            client_message_id,
        )

    async def _finish_at_most_once_command(self, msg: InboundMessage) -> None:
        """Complete a command receipt without swallowing durability failures."""
        client_message_id = msg.metadata.get("client_message_id")
        if not isinstance(client_message_id, str):
            return
        await self.chat_inbox.mark_processed(msg.chat_id, client_message_id)

    async def _complete_at_most_once_command(
        self,
        msg: InboundMessage,
        result: OutboundMessage | None,
    ) -> None:
        """Complete a command, preserving its known result if SQLite is unavailable."""
        try:
            await self._finish_at_most_once_command(msg)
        except Exception:
            logger.exception(
                "Could not complete command receipt for session {}",
                self._effective_session_key(msg),
            )
            if result is None:
                return
            try:
                self._persist_command_outcome(msg, result)
            except Exception:
                logger.exception(
                    "Could not persist known command outcome for session {}",
                    self._effective_session_key(msg),
                )

    def _persist_command_outcome(
        self,
        msg: InboundMessage,
        result: OutboundMessage,
    ) -> None:
        """Durably retain a successful command result for later reconciliation."""
        client_message_id = msg.metadata.get("client_message_id")
        if not isinstance(client_message_id, str):
            return
        session = self.sessions.get_or_create(self._effective_session_key(msg))
        if not self._session_has_client_message(session, client_message_id):
            session.add_message(
                "user",
                msg.content,
                client_message_id=client_message_id,
            )
        already_recorded = any(
            message.get("command_outcome_id") == client_message_id
            for message in session.messages
            if isinstance(message, dict)
        )
        if not already_recorded:
            session.add_message(
                "assistant",
                result.content,
                command_outcome_id=client_message_id,
            )
        self.sessions.save(session, fsync=True)

    async def _record_command_failure(self, msg: InboundMessage) -> None:
        """Persist an uncertain command outcome before closing its receipt."""
        client_message_id = msg.metadata.get("client_message_id")
        session = self.sessions.get_or_create(self._effective_session_key(msg))
        if (
            isinstance(client_message_id, str)
            and not self._session_has_client_message(session, client_message_id)
        ):
            session.add_message(
                "user",
                msg.content,
                client_message_id=client_message_id,
            )
        already_recorded = bool(
            isinstance(client_message_id, str)
            and any(
                message.get("command_failure_id") == client_message_id
                for message in session.messages
                if isinstance(message, dict)
            )
        )
        if not already_recorded:
            session.add_message(
                "assistant",
                self._command_failure_message(),
                command_failure_id=client_message_id,
            )
        self.sessions.save(session, fsync=True)
        await self._finish_at_most_once_command(msg)

    async def _record_command_failure_safely(self, msg: InboundMessage) -> None:
        """Best-effort visible command failure; leave the marker for recovery."""
        try:
            await self._record_command_failure(msg)
        except Exception:
            logger.exception(
                "Could not durably record command failure for session {}",
                self._effective_session_key(msg),
            )

    async def _recover_interrupted_commands(self) -> None:
        """Surface command outcomes made uncertain by a previous process exit."""
        records = await self.chat_inbox.interrupted_commands()
        for record in records:
            try:
                session = self.sessions.get_or_create(
                    self._effective_session_key(record.message)
                )
                known_outcome = any(
                    message.get("command_outcome_id")
                    == record.client_message_id
                    for message in session.messages
                    if isinstance(message, dict)
                )
                if known_outcome:
                    await self._finish_at_most_once_command(record.message)
                    continue
                await self._record_command_failure(record.message)
            except Exception:
                logger.exception(
                    "Failed to recover interrupted command receipt {}:{}",
                    record.message.chat_id,
                    record.client_message_id,
                )

    async def _prepare_chat_message_retry(self, msg: InboundMessage) -> int | None:
        client_message_id = msg.metadata.get("client_message_id")
        if not isinstance(client_message_id, str):
            return None
        try:
            record = await self.chat_inbox.prepare_retry(
                msg.chat_id,
                client_message_id,
            )
        except KeyError:
            logger.debug(
                "No durable chat receipt to retry for {}:{}",
                msg.chat_id,
                client_message_id,
            )
            return None
        except Exception:
            logger.exception(
                "Failed to prepare durable chat retry for {}:{}",
                msg.chat_id,
                client_message_id,
            )
            return None
        if record.state == "processed":
            return None
        if record.retry_count >= self._MAX_CHAT_PROCESSING_RETRIES:
            await self._finalize_terminal_chat_failure(msg)
            return self._TERMINAL_CHAT_RETRY
        return min(30, 1 << min(record.retry_count, 5))

    async def _retry_chat_message_in_place(
        self,
        msg: InboundMessage,
        session_key: str,
        pending: asyncio.Queue,
        lock: asyncio.Lock,
    ) -> int | None:
        client_message_id = msg.metadata.get("client_message_id")
        if not isinstance(client_message_id, str):
            return None
        try:
            claimed = await self.chat_inbox.claim_retry_for_enqueue(
                msg.chat_id,
                client_message_id,
            )
            if not claimed:
                return None
            gate = self._concurrency_gate or nullcontext()
            async with lock, gate:
                response = await self._process_message(
                    msg,
                    pending_queue=(
                        None if msg.metadata.get("shared_room") else pending
                    ),
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Retry failed while processing message for session {}",
                session_key,
            )
            retry_delay = await self._prepare_chat_message_retry(msg)
            if retry_delay == self._TERMINAL_CHAT_RETRY:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self._terminal_chat_failure_message(),
                    metadata=dict(msg.metadata or {}),
                ))
            elif retry_delay is not None:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Retrying in {retry_delay} seconds.",
                    metadata={
                        **dict(msg.metadata or {}),
                        "_status_delta": True,
                    },
                ))
            else:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                    metadata=dict(msg.metadata or {}),
                ))
            return retry_delay

    @staticmethod
    def _terminal_chat_failure_message() -> str:
        return (
            "I could not complete this request after several attempts. "
            "It was saved, but processing has stopped so later messages can continue."
        )

    @staticmethod
    def _command_failure_message() -> str:
        return (
            "This command did not finish, and Ziggy did not replay it because "
            "commands may have side effects. Check the current state before trying again."
        )

    @staticmethod
    def _command_not_started_message() -> str:
        return (
            "This command could not be started safely because its durable receipt "
            "was unavailable. It was not executed and remains queued for recovery."
        )

    async def _finalize_terminal_chat_failure(self, msg: InboundMessage) -> None:
        session = self.sessions.get_or_create(self._effective_session_key(msg))
        message = self._terminal_chat_failure_message()
        if self._restore_runtime_checkpoint(session):
            pass
        elif session.metadata.get(self._PENDING_USER_TURN_KEY):
            self._clear_pending_user_turn(session)
        if not self._session_has_client_message(
            session,
            str(msg.metadata.get("client_message_id") or ""),
        ):
            extra: dict[str, Any] = {}
            client_message_id = msg.metadata.get("client_message_id")
            if isinstance(client_message_id, str):
                extra["client_message_id"] = client_message_id
            if msg.media:
                extra["media"] = list(msg.media)
            session.add_message("user", msg.content, **extra)
        if not (
            session.messages
            and session.messages[-1].get("role") == "assistant"
            and session.messages[-1].get("content") == message
        ):
            session.add_message("assistant", message)
        self.sessions.save(session)
        await self._mark_chat_message_processed(msg)

    async def _mark_chat_message_ids_processed(
        self,
        chat_id: str,
        client_message_ids: list[str],
    ) -> None:
        for client_message_id in client_message_ids:
            try:
                await self.chat_inbox.mark_processed(chat_id, client_message_id)
            except KeyError:
                logger.debug(
                    "No durable chat receipt for {}:{}",
                    chat_id,
                    client_message_id,
                )
            except Exception:
                logger.exception(
                    "Failed to mark durable chat receipt processed for {}:{}",
                    chat_id,
                    client_message_id,
                )

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(
        self,
        session: Session,
        client_message_ids: list[str] | None = None,
    ) -> None:
        normalized = self._normalized_client_message_ids(client_message_ids or [])
        session.metadata[self._PENDING_USER_TURN_KEY] = (
            {"client_message_ids": normalized}
            if normalized
            else True
        )

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _pending_user_turn_client_message_ids(self, session: Session) -> list[str]:
        pending = session.metadata.get(self._PENDING_USER_TURN_KEY)
        if isinstance(pending, dict):
            return self._normalized_client_message_ids(
                pending.get("client_message_ids")
            )
        if pending is not True:
            return []
        for message in reversed(session.messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            singular = message.get("client_message_id")
            result = [singular] if isinstance(singular, str) else []
            result.extend(
                self._normalized_client_message_ids(
                    message.get("client_message_ids")
                )
            )
            return list(dict.fromkeys(result))
        return []

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [], metadata=metadata or {},
        )
        current = asyncio.current_task()
        tasks = self._active_tasks.setdefault(session_key, [])
        registered = current is not None and current not in tasks
        if registered:
            tasks.append(current)
        try:
            return await self._process_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
            )
        finally:
            if registered and current in self._active_tasks.get(session_key, []):
                self._active_tasks[session_key].remove(current)
            if not self._active_tasks.get(session_key):
                self._active_tasks.pop(session_key, None)
