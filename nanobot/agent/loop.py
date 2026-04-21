"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from collections import OrderedDict

from nanobot.agent.context import ContextBuilder
from nanobot.agent.hot_reload import ContextOptimizer
from nanobot.agent.memory import MemoryStore

# Maximum number of per-session locks to keep in memory
_MAX_SESSION_LOCKS = 500
from nanobot.agent.tools.audit import AuditLogger
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.recall import IngestTool, RecallTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig
    from nanobot.cron.service import CronService


# Only Mihai can modify system internals (architecture, skills, prompts, code)
OWNER_USER_ID = "1476998117906845890"

# Patterns that indicate system modification intent (checked against user messages)
_SYSTEM_MOD_PATTERNS = [
    # Architecture
    r"update\s+architecture", r"modify\s+architecture", r"change\s+architecture",
    # Skills / plugins
    r"(add|install|create|remove|delete|uninstall)\s+(a\s+)?(skill|plugin|extension|tool)\b",
    # System prompts / personality
    r"(change|modify|update|edit|rewrite)\s+(your|the|ziggy.s?|nanobot.s?)?\s*(system\s+prompt|personality|soul|identity|instructions|behavior)",
    r"(change|modify|update)\s+.*\bSOUL\.md\b",
    r"(change|modify|update)\s+.*\bAGENTS\.md\b",
    r"(change|modify|update)\s+.*\bHEARTBEAT\.md\b",
    # Code changes to nanobot itself
    r"(change|modify|edit|update|rewrite)\s+(your|the|ziggy.s?|nanobot.s?)?\s*(source\s+)?code",
    r"(change|modify|edit)\s+.*\.(py|ts|js)\s+(in|of|for)\s+(nanobot|ziggy|the\s+bot)",
    # Config
    r"(change|modify|update|edit)\s+(the\s+)?(config|configuration|settings)\b",
    # Workers / security settings
    r"set\s+(workers|security|model|provider)\s+",
]

_compiled_system_mod = [re.compile(p, re.IGNORECASE) for p in _SYSTEM_MOD_PATTERNS]


def is_system_modification(content: str) -> bool:
    """Check if message requests system-level changes (skills, prompts, code, config)."""
    text = content.strip()
    for pattern in _compiled_system_mod:
        if pattern.search(text):
            return True
    return False


async def notify_mihai_dm(bus: MessageBus, requester_id: str, content: str) -> None:
    """Send a DM to Mihai about unauthorized architecture request."""
    await bus.publish_outbound(OutboundMessage(
        channel="discord",
        chat_id=ALLOWED_USER_ID,
        content=f"⚠️ **Unauthorized Architecture Request**\n\n"
               f"**From:** {requester_id}\n"
               f"**Requested:** {content}\n\n"
               f"_Only you can modify architecture._",
    ))


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

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        agent_types: dict | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._memory_store = MemoryStore(workspace)

        self.context = ContextBuilder(workspace, memory_store=self._memory_store)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            agent_types=agent_types,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_lock = asyncio.Lock()
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._session_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._audit_logger = AuditLogger()
        self._context_optimizer = ContextOptimizer()
        self._register_default_tools()
        self.tools.set_audit_logger(self._audit_logger)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
        self.tools.register(RecallTool(workspace=self.workspace))
        self.tools.register(IngestTool(workspace=self.workspace, allowed_dir=allowed_dir))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy, lock-protected)."""
        if self._mcp_connected or not self._mcp_servers:
            return
        async with self._mcp_lock:
            # Double-check after acquiring lock
            if self._mcp_connected:
                return
            from nanobot.agent.tools.mcp import connect_mcp_servers
            try:
                self._mcp_stack = AsyncExitStack()
                await self._mcp_stack.__aenter__()
                await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
                self._mcp_connected = True
            except Exception as e:
                logger.error("Failed to connect MCP servers (will retry next message): {}", e)
                if self._mcp_stack:
                    try:
                        await self._mcp_stack.aclose()
                    except Exception:
                        pass
                    self._mcp_stack = None

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None, sender_id: str = "") -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
        self.tools.set_context(session_id=f"{channel}:{chat_id}", channel=channel, sender_id=sender_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>...</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            val = next(iter(tc.arguments.values()), None) if tc.arguments else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}...")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        import time as _time
        _loop_start = _time.perf_counter()

        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        # Context budget: reserve output tokens from a reasonable context limit.
        # Most models support at least 8k; use 120k as a generous default.
        _MAX_CONTEXT_TOKENS = 120_000
        context_budget = _MAX_CONTEXT_TOKENS - self.max_tokens

        while iteration < self.max_iterations:
            iteration += 1

            # Trim context if it exceeds the token budget
            _t0 = _time.perf_counter()
            system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
            if system_msg:
                sys_text = system_msg.get("content", "")
                _, trimmed = self._context_optimizer.optimize_context(
                    sys_text, messages[1:], context_budget,
                )
                messages = [system_msg] + trimmed
            _ctx_ms = (_time.perf_counter() - _t0) * 1000

            _t0 = _time.perf_counter()
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            _llm_ms = (_time.perf_counter() - _t0) * 1000
            _msg_count = len(messages)
            _usage = response.usage or {}
            logger.info(
                "Perf [iter {}]: llm={:.0f}ms ctx_trim={:.0f}ms msgs={} tokens_in={} tokens_out={}",
                iteration, _llm_ms, _ctx_ms, _msg_count,
                _usage.get("prompt_tokens", "?"), _usage.get("completion_tokens", "?"),
            )
            # Feed Prometheus
            try:
                from nanobot.dashboard.server import _PROM_AVAILABLE
                if _PROM_AVAILABLE:
                    from nanobot.dashboard.server import PROM_LLM_DURATION
                    PROM_LLM_DURATION.observe(_llm_ms)
            except Exception:
                pass
            # Persist LLM call to audit.jsonl
            self._audit_logger.log_llm_call(
                session_id=self.tools._session_id,
                channel=self.tools._channel,
                model=self.model,
                tokens_in=_usage.get("prompt_tokens"),
                tokens_out=_usage.get("completion_tokens"),
                latency_ms=_llm_ms,
                ttft_ms=getattr(response, "ttft_ms", None),
            )

            if response.has_tool_calls:
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    _t0 = _time.perf_counter()
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    _tool_ms = (_time.perf_counter() - _t0) * 1000
                    logger.info("Tool call: {}({}) [{:.0f}ms]", tool_call.name, args_str[:200], _tool_ms)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                )
                final_content = clean
                break

        _total_ms = (_time.perf_counter() - _loop_start) * 1000
        logger.info("Perf [total]: {:.0f}ms, {} iterations, {} tool calls", _total_ms, iteration, len(tools_used))

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self, num_workers: int = 4) -> None:
        """
        Run the agent loop with a worker pool.

        Args:
            num_workers: Number of concurrent workers (default 4)
        """
        self._running = True
        await self._connect_mcp()
        logger.info(f"Agent loop started with {num_workers} workers")

        # Start worker pool
        await self.bus.start_workers(num_workers, self._handle_message)

        # Wait until workers are done (they'll run forever until shutdown)
        try:
            await asyncio.Event().wait()  # Wait for shutdown signal
        except asyncio.CancelledError:
            pass
        finally:
            await self.bus.stop_workers()

    async def _handle_message(self, msg: InboundMessage) -> None:
        """Handle a single message, routing /stop to special handler."""
        if msg.content.strip().lower() == "/stop":
            await self._handle_stop(msg)
        else:
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(msg.session_key, []).append(task)
            task.add_done_callback(
                lambda t, k=msg.session_key: (
                    self._active_tasks.get(k, []) and
                    self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None
                )
            )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under a per-session lock."""
        # Guard: only the owner can request system modifications
        if is_system_modification(msg.content):
            if msg.sender_id != OWNER_USER_ID:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="I can't make changes to my own system, skills, or configuration. "
                            "Only my owner can authorize that. Let me know if there's something "
                            "else I can help you with!",
                ))
                await notify_mihai_dm(self.bus, msg.sender_id, msg.content)
                return

        if msg.session_key in self._session_locks:
            self._session_locks.move_to_end(msg.session_key)
        else:
            self._session_locks[msg.session_key] = asyncio.Lock()
        # Evict oldest unlocked entries if over limit
        while len(self._session_locks) > _MAX_SESSION_LOCKS:
            oldest_key, oldest_lock = next(iter(self._session_locks.items()))
            if oldest_lock.locked():
                break  # Don't evict an active lock
            del self._session_locks[oldest_key]
        import time as _time
        _lock_wait_start = _time.perf_counter()
        lock = self._session_locks[msg.session_key]
        async with lock:
            _lock_ms = (_time.perf_counter() - _lock_wait_start) * 1000
            if _lock_ms > 100:
                logger.warning("Perf: session lock wait {:.0f}ms for {}", _lock_ms, msg.session_key)
            _dispatch_start = _time.perf_counter()
            try:
                response = await self._process_message(msg)
                _total_ms = (_time.perf_counter() - _dispatch_start) * 1000
                logger.info("Perf [dispatch]: {:.0f}ms total for {}", _total_ms, msg.session_key)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    async def stop(self) -> None:
        """Stop the agent loop and worker pool."""
        self._running = False
        await self.bus.stop_workers()
        logger.info("Agent loop stopped")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"), sender_id=msg.sender_id)
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)
                if not lock.locked():
                    self._consolidation_locks.pop(session.key, None)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="nanobot commands:\n/new -- Start a new conversation\n/stop -- Stop the current task\n/help -- Show available commands")

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    if not lock.locked():
                        self._consolidation_locks.pop(session.key, None)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"), sender_id=msg.sender_id)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # Reset router tier classification for each new message
        if hasattr(self.provider, 'reset_tier'):
            self.provider.reset_tier(session_id=key)

        history = session.get_history(max_messages=self.memory_window)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = {k: v for k, v in m.items() if k != "reasoning_content"}
            role, content = entry.get("role"), entry.get("content")
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    continue
                if isinstance(content, list):
                    entry["content"] = [
                        {"type": "text", "text": "[image]"} if (
                            c.get("type") == "image_url"
                            and c.get("image_url", {}).get("url", "").startswith("data:image/")
                        ) else c for c in content
                    ]
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        return await self._memory_store.consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
