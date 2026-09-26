"""Tests for ziggy.dev task-context meta on MCP tools/call (MIT-1379).

Connectors need the Work task id and attendance flag on every MCP call so they
can park/resume Work logins. The meta rides on ``params._meta`` via the MCP
SDK's ``call_tool(..., meta=...)`` keyword; the model's arguments are never
touched and no session key is ever sent (DEC-20).
"""

from types import SimpleNamespace

import pytest
from mcp import types as mcp_types

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.mcp import MCPToolWrapper

_WORK_TASK_ID = "work_" + "0123456789abcdef" * 2


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


def _make_wrapper(session: _RecordingSession) -> MCPToolWrapper:
    return MCPToolWrapper(session, "ziggy_browser", _make_tool_def(), tool_timeout=5)


def _make_context(metadata=None, **overrides):
    defaults = dict(channel="web", chat_id="chat-1", metadata=metadata or {})
    defaults.update(overrides)
    return RequestContext(**defaults)


async def _execute(ctx, arguments=None):
    session = _RecordingSession()
    wrapper = _make_wrapper(session)
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
    assert call["meta"] == {
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
