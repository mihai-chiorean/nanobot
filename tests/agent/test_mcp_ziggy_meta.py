"""Tests for ziggy.dev task-context meta on MCP tools/call (MIT-1379, MIT-1820).

Connectors need the Work task id and attendance flag on every MCP call so they
can park/resume Work logins. The meta rides on ``params._meta`` via the MCP
SDK's ``call_tool(..., meta=...)`` keyword; the model's arguments are never
touched and no session key is ever sent (DEC-20). Non-read-only calls also
carry ``ziggy.dev/idempotency_key`` (SR-11): a stable hash of the turn scope,
tool name and arguments so a replayed turn dedupes its write downstream.
"""

import re
from types import SimpleNamespace

import pytest
from mcp import types as mcp_types

from nanobot.agent.tools.context import (
    RequestContext,
    request_context,
    turn_idempotency_scope,
)
from nanobot.agent.tools.mcp import (
    MCPToolWrapper,
    _idempotency_key,
    _idempotency_scope,
)
from nanobot.session.turn_continuation import (
    RECOVERY_ORIGIN_SCOPE_META,
    resolve_idempotency_scope,
)

_WORK_TASK_ID = "work_" + "0123456789abcdef" * 2
_IDEMPOTENCY_KEY_META = "ziggy.dev/idempotency_key"


class _RecordingSession:
    """Fake ClientSession recording how call_tool was invoked."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append({"name": name, "arguments": arguments, **kwargs})
        return SimpleNamespace(
            content=[mcp_types.TextContent(type="text", text="ok")],
            isError=False,
        )


def _make_tool_def(name="browser_click"):
    return SimpleNamespace(
        name=name,
        description="A test tool",
        inputSchema={"type": "object", "properties": {}},
    )


class _ReadOnlyWrapper(MCPToolWrapper):
    """Negative control: a tool annotated read-only (SR-08) must stay unkeyed."""

    @property
    def read_only(self) -> bool:
        return True


def _make_wrapper(
    session: _RecordingSession, wrapper_cls: type[MCPToolWrapper] = MCPToolWrapper
) -> MCPToolWrapper:
    return wrapper_cls(session, "ziggy_browser", _make_tool_def(), tool_timeout=5)


def _make_context(metadata=None, **overrides):
    defaults = dict(channel="web", chat_id="chat-1", metadata=metadata or {})
    defaults.update(overrides)
    return RequestContext(**defaults)


async def _execute(
    ctx,
    arguments=None,
    wrapper_cls: type[MCPToolWrapper] = MCPToolWrapper,
):
    session = _RecordingSession()
    wrapper = _make_wrapper(session, wrapper_cls)
    kwargs = {"target": "button#login"} if arguments is None else arguments
    if ctx is None:
        await wrapper.execute(**kwargs)
    else:
        with request_context(ctx):
            await wrapper.execute(**kwargs)
    assert len(session.calls) == 1
    return session.calls[0]


@pytest.mark.asyncio
async def test_chat_turn_sends_attended_chat_meta():
    ctx = _make_context(metadata={"some_other_key": "x"})
    call = await _execute(ctx)
    assert call["meta"] == {"ziggy.dev/origin": "chat", "ziggy.dev/attended": True}


@pytest.mark.asyncio
async def test_work_turn_sends_work_task_id_unattended():
    ctx = _make_context(metadata={"work_task_id": _WORK_TASK_ID, "work_mode": True})
    call = await _execute(ctx)
    meta = call["meta"]
    assert _IDEMPOTENCY_KEY_META in meta
    meta_without_key = {
        key: value for key, value in meta.items() if key != _IDEMPOTENCY_KEY_META
    }
    assert meta_without_key == {
        "ziggy.dev/origin": "work",
        "ziggy.dev/attended": False,
        "ziggy.dev/work_task_id": _WORK_TASK_ID,
    }


@pytest.mark.asyncio
async def test_work_mode_flag_without_task_id_is_unattended_work():
    ctx = _make_context(metadata={"work_mode": True})
    call = await _execute(ctx)
    assert call["meta"] == {"ziggy.dev/origin": "work", "ziggy.dev/attended": False}


@pytest.mark.asyncio
async def test_malformed_work_task_id_is_treated_as_chat():
    # Negative control: ids that are not work_<32 hex> must not leak through
    # as work context, and must not flip the turn to unattended.
    for bad in ("work_zzz", "work_" + "0123456789abcdef" * 2 + "a", "task_abc", "work_"):
        ctx = _make_context(metadata={"work_task_id": bad})
        call = await _execute(ctx)
        assert call["meta"] == {
            "ziggy.dev/origin": "chat",
            "ziggy.dev/attended": True,
        }, bad


@pytest.mark.asyncio
async def test_no_request_context_sends_no_meta():
    call = await _execute(None)
    assert call["meta"] is None


@pytest.mark.asyncio
async def test_arguments_unchanged():
    ctx = _make_context(metadata={"work_task_id": _WORK_TASK_ID, "work_mode": True})
    arguments = {"target": "input#password", "value": "s3cret"}
    original = dict(arguments)
    call = await _execute(ctx, arguments=arguments)
    assert call["arguments"] == original
    assert arguments == original
    assert "_meta" not in call["arguments"]


@pytest.mark.asyncio
async def test_no_session_key_sent():
    # Negative control (DEC-20): even when the request context carries a
    # session key (chat resume), nothing session-shaped may ride the meta.
    ctx = _make_context(
        metadata={"work_task_id": _WORK_TASK_ID, "work_mode": True},
        session_key="room:abc123",
    )
    call = await _execute(ctx)
    meta = call["meta"]
    assert "room:abc123" not in str(meta)
    assert all("session" not in key.lower() for key in meta)
    assert "ziggy.dev/session_key" not in meta


# ---------------------------------------------------------------------------
# SR-11: ziggy.dev/idempotency_key on every non-read-only MCP call (MIT-1820)
# ---------------------------------------------------------------------------

_SESSION_KEY = "websocket:chat-7"


@pytest.mark.asyncio
async def test_chat_write_key_is_scoped_to_client_message_id():
    ctx = _make_context(
        metadata={"client_message_id": "cm-42"},
        session_key=_SESSION_KEY,
    )
    arguments = {"target": "button#login"}
    call = await _execute(ctx, arguments=arguments)
    key = call["meta"][_IDEMPOTENCY_KEY_META]
    assert re.fullmatch(r"idem_[a-f0-9]{40}", key)
    assert key == _idempotency_key("cm-42", "browser_click", arguments)
    # DEC-20: the key is a pure hash, no scope/chat text leaks.
    for secret in ("cm-42", "chat-1", _SESSION_KEY):
        assert secret not in key


@pytest.mark.asyncio
async def test_work_write_key_is_scoped_to_the_task_id():
    ctx = _make_context(
        metadata={
            "work_task_id": _WORK_TASK_ID,
            "work_mode": True,
            "client_message_id": "cm-ignored",
        },
        session_key=_SESSION_KEY,
    )
    arguments = {"target": "button#login"}
    call = await _execute(ctx, arguments=arguments)
    assert call["meta"][_IDEMPOTENCY_KEY_META] == _idempotency_key(
        _WORK_TASK_ID, "browser_click", arguments
    )


@pytest.mark.asyncio
async def test_key_stable_across_argument_order_and_changes_with_arguments():
    ctx = _make_context(
        metadata={"client_message_id": "cm-order"},
        session_key=_SESSION_KEY,
    )
    first = await _execute(ctx, arguments={"b": 2, "a": "x"})
    second = await _execute(ctx, arguments={"a": "x", "b": 2})
    assert first["meta"][_IDEMPOTENCY_KEY_META] == second["meta"][_IDEMPOTENCY_KEY_META]
    third = await _execute(ctx, arguments={"b": 3, "a": "x"})
    assert third["meta"][_IDEMPOTENCY_KEY_META] != first["meta"][_IDEMPOTENCY_KEY_META]


@pytest.mark.asyncio
async def test_read_only_tool_sends_no_idempotency_key():
    # Negative control (SR-08): an annotated read-only tool is replay-safe and
    # must not carry a key, even with a full turn scope available.
    ctx = _make_context(
        metadata={"client_message_id": "cm-42"},
        session_key=_SESSION_KEY,
    )
    call = await _execute(ctx, wrapper_cls=_ReadOnlyWrapper)
    assert call["meta"] == {
        "ziggy.dev/origin": "chat",
        "ziggy.dev/attended": True,
    }
    assert _IDEMPOTENCY_KEY_META not in call["meta"]


@pytest.mark.asyncio
async def test_scopeless_turn_sends_no_idempotency_key():
    # Negative control: no context, no stable metadata -> no key (pre-SR-11).
    call = await _execute(None)
    assert _IDEMPOTENCY_KEY_META not in (call["meta"] or {})
    ctx = _make_context(metadata={"unrelated": "x"})
    call = await _execute(ctx)
    assert _IDEMPOTENCY_KEY_META not in call["meta"]


@pytest.mark.asyncio
async def test_bound_turn_scope_wins_and_carries_no_scope_text():
    # The loop binds the scope it resolved and persisted for the turn; that
    # value wins over anything in the request metadata.
    ctx = _make_context(metadata={"client_message_id": "cm-42"}, session_key=_SESSION_KEY)
    with turn_idempotency_scope("turn_" + "ab" * 16):
        call = await _execute(ctx)
    key = call["meta"][_IDEMPOTENCY_KEY_META]
    assert key == _idempotency_key(
        "turn_" + "ab" * 16, "browser_click", {"target": "button#login"}
    )
    assert re.fullmatch(r"idem_[a-f0-9]{40}", key)
    assert "turn_" not in key


def test_idempotency_key_is_order_canonical_and_pure():
    arguments = {"to": "grandma@example.org", "body": "hi \u00e9"}
    key = _idempotency_key("scope-1", "gmail_send_draft", arguments)
    assert re.fullmatch(r"idem_[a-f0-9]{40}", key)
    assert key == _idempotency_key(
        "scope-1", "gmail_send_draft", {"body": "hi \u00e9", "to": "grandma@example.org"}
    )
    assert key != _idempotency_key("scope-2", "gmail_send_draft", arguments)
    assert key != _idempotency_key("scope-1", "gmail_send_message", arguments)
    assert "grandma" not in key and "scope-1" not in key


def test_idempotency_scope_priority_and_fallbacks():
    assert _idempotency_scope(None) is None
    assert _idempotency_scope(_make_context(metadata={})) is None
    # Work task id beats every other source; a malformed one never counts.
    assert (
        _idempotency_scope(
            _make_context(
                metadata={
                    "work_task_id": _WORK_TASK_ID,
                    "client_message_id": "cm-1",
                    RECOVERY_ORIGIN_SCOPE_META: "turn_origin",
                },
                session_key=_SESSION_KEY,
            )
        )
        == _WORK_TASK_ID
    )
    assert (
        _idempotency_scope(
            _make_context(
                metadata={"work_task_id": "work_zzz", "client_message_id": "cm-1"},
                session_key=_SESSION_KEY,
            )
        )
        == "cm-1"
    )
    # client_message_id beats the recovery scope; blank values never count.
    assert (
        _idempotency_scope(
            _make_context(
                metadata={
                    "client_message_id": "cm-1",
                    RECOVERY_ORIGIN_SCOPE_META: "turn_origin",
                },
                session_key=_SESSION_KEY,
            )
        )
        == "cm-1"
    )
    assert (
        _idempotency_scope(
            _make_context(
                metadata={
                    "client_message_id": "   ",
                    RECOVERY_ORIGIN_SCOPE_META: "turn_origin",
                },
                session_key=_SESSION_KEY,
            )
        )
        == "turn_origin"
    )
    # A new turn without any stable id gets a fresh, distinct scope each time.
    first = resolve_idempotency_scope({})
    second = resolve_idempotency_scope({})
    assert re.fullmatch(r"turn_[a-f0-9]{32}", first)
    assert first != second
    assert resolve_idempotency_scope({"client_message_id": "cm-1"}) == "cm-1"
    # Unbound and no stable metadata -> no scope, no key.
    assert _idempotency_scope(_make_context(session_key=_SESSION_KEY)) is None
