"""Runtime context for tool construction."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.exec_session import ExecSessionManager
    from nanobot.agent.tools.file_state import FileStates
    from nanobot.agent.tools.runtime_control import RuntimeControl
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ProviderConfig, ToolsConfig
    from nanobot.cron.service import CronService
    from nanobot.providers.factory import ProviderSnapshot
    from nanobot.security.workspace_access import WorkspaceSandboxStatus
    from nanobot.session.manager import SessionManager
    from nanobot.utils.llm_runtime import LLMRuntime
    from nanobot.work.store import WorkStore

_CURRENT_REQUEST_CONTEXT: ContextVar["RequestContext | None"] = ContextVar(
    "nanobot_tool_request_context",
    default=None,
)

#: ``RequestContext.attributes`` key carrying a park signal a tool stamped during
#: the turn (MCP result ``_meta["ziggy.dev/park"]``, D4-36/D5).  The carrier is
#: the per-turn ``RequestContext`` (bound through ``_CURRENT_REQUEST_CONTEXT``),
#: which tools can reach; ``TurnContext.attributes`` is a snapshot copy and is
#: *not* visible to tool calls.  ``AgentLoop._record_work_outcome`` reads this
#: key back to end a Work turn in ``waiting`` instead of ``succeeded``.
ZIGGY_PARK_ATTRIBUTE = "ziggy_park"

#: Turn-scoped index of the turn's persisted user message in
#: ``session.messages``, bound by ``AgentLoop`` when it persists the turn's
#: input (SR-11).  It is the last-resort idempotency scope for turns that
#: carry neither a Work task id nor a client message id, so two turns in one
#: session still get distinct scopes.  Deliberately a context var, not a
#: ``RequestContext`` field: the caller-visible metadata/attributes bags stay
#: exactly what the caller sent (facade contract).
_CURRENT_TURN_USER_INDEX: ContextVar[int | None] = ContextVar(
    "nanobot_turn_user_message_index",
    default=None,
)


def bind_turn_user_message_index(index: int | None) -> Token[int | None]:
    return _CURRENT_TURN_USER_INDEX.set(index)


def reset_turn_user_message_index(token: Token[int | None]) -> None:
    _CURRENT_TURN_USER_INDEX.reset(token)


def current_turn_user_message_index() -> int | None:
    return _CURRENT_TURN_USER_INDEX.get()


@contextmanager
def turn_user_message_index(index: int | None):
    """Bind the running turn's persisted-user-message index for tool scope."""
    token = bind_turn_user_message_index(index)
    try:
        yield index
    finally:
        reset_turn_user_message_index(token)


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    original_user_text: str | None = None
    runtime: LLMRuntime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sender_id: str | None = None
    turn_id: str | None = None
    workspace: Path | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


def bind_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _CURRENT_REQUEST_CONTEXT.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _CURRENT_REQUEST_CONTEXT.reset(token)


@contextmanager
def request_context(ctx: RequestContext):
    """Bind one immutable request snapshot and restore the previous value."""
    token = bind_request_context(ctx)
    try:
        yield ctx
    finally:
        reset_request_context(token)


def current_request_context() -> RequestContext | None:
    return _CURRENT_REQUEST_CONTEXT.get()


def current_request_session_key() -> str | None:
    ctx = current_request_context()
    return ctx.session_key if ctx else None


@dataclass
class ToolContext:
    config: ToolsConfig
    workspace: str
    bus: MessageBus | None = None
    subagent_manager: SubagentManager | None = None
    cron_service: CronService | None = None
    exec_session_manager: ExecSessionManager | None = None
    sessions: SessionManager | None = None
    file_state_store: FileStates | None = None
    provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None
    image_generation_provider_configs: dict[str, ProviderConfig] | None = None
    timezone: str = "UTC"
    workspace_sandbox: WorkspaceSandboxStatus | None = None
    runtime_control: RuntimeControl | None = None
    # Ziggy-local (MIT-1010): durable Work store backing the scheduled-work tools.
    work_store: WorkStore | None = None
    model_name: str = ""
