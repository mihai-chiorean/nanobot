"""Tests for MCP tool/resource/prompt transient error retry."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from nanobot.agent.tools.mcp import (
    MCPPromptWrapper,
    MCPResourceWrapper,
    MCPToolWrapper,
    _is_session_terminated,
    _is_transient,
)
from nanobot.agent.tools.registry import is_tool_error_result

# ---------------------------------------------------------------------------
# _is_transient helper
# ---------------------------------------------------------------------------


class _FakeClosedResourceError(Exception):
    pass


_FakeClosedResourceError.__name__ = "ClosedResourceError"


class _FakeEndOfStreamError(Exception):
    pass


_FakeEndOfStreamError.__name__ = "EndOfStream"


def _session_terminated_error() -> McpError:
    return McpError(ErrorData(code=-32000, message="Session terminated"))


def _connection_closed_error() -> McpError:
    return McpError(ErrorData(code=-32000, message="Connection closed"))


def test_is_transient_recognizes_closed_resource():
    assert _is_transient(_FakeClosedResourceError("gone"))


def test_is_transient_recognizes_broken_pipe():
    assert _is_transient(BrokenPipeError("pipe"))


def test_is_transient_recognizes_connection_reset():
    assert _is_transient(ConnectionResetError("reset"))


def test_is_transient_recognizes_connection_refused():
    assert _is_transient(ConnectionRefusedError("refused"))


def test_is_transient_recognizes_end_of_stream():
    assert _is_transient(_FakeEndOfStreamError("eof"))


def test_is_transient_rejects_value_error():
    assert not _is_transient(ValueError("nope"))


def test_is_transient_rejects_runtime_error():
    assert not _is_transient(RuntimeError("nope"))


def test_is_transient_rejects_timeout():
    assert not _is_transient(TimeoutError("timeout"))


def test_is_session_terminated_recognizes_mcp_error():
    assert _is_session_terminated(_session_terminated_error())


def test_is_session_terminated_recognizes_connection_closed_mcp_error():
    assert _is_session_terminated(_connection_closed_error())


# ---------------------------------------------------------------------------
# MCPToolWrapper retry behaviour
# ---------------------------------------------------------------------------


def _annotations(read_only=None, idempotent=None, destructive=None):
    """Mimic mcp.types.ToolAnnotations (camelCase fields)."""
    return SimpleNamespace(
        readOnlyHint=read_only,
        idempotentHint=idempotent,
        destructiveHint=destructive,
    )


def _make_tool_def(name="test_tool", annotations=None):
    return SimpleNamespace(
        name=name,
        description="A test tool",
        inputSchema={"type": "object", "properties": {}},
        annotations=annotations,
    )


def _make_read_only_tool_def(name="test_tool"):
    """Tool definition as an operator-trusted server would publish it."""
    return _make_tool_def(name, annotations=_annotations(read_only=True))


def _make_read_only_wrapper(session, tool_timeout=5):
    return MCPToolWrapper(
        session,
        "test_server",
        _make_read_only_tool_def(),
        tool_timeout=tool_timeout,
        trust_annotations=True,
    )


def _make_tool_result(text):
    """Build a mock tool result with proper MCP TextContent."""
    return SimpleNamespace(content=[mcp_types.TextContent(type="text", text=text)])


@pytest.mark.asyncio
async def test_tool_retries_on_transient_error():
    """A trusted read-only (idempotent) tool retries once, then succeeds."""
    session = AsyncMock()
    result = _make_tool_result("ok")
    exc = _FakeClosedResourceError("connection lost")
    session.call_tool = AsyncMock(side_effect=[exc, result])

    wrapper = _make_read_only_wrapper(session)

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute(foo="bar")

    assert output == "ok"
    assert session.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_tool_fails_after_retry_exhausted():
    """Tool should fail with retry message when both attempts hit transient errors."""
    session = AsyncMock()
    exc1 = _FakeClosedResourceError("still dead")
    exc2 = _FakeClosedResourceError("still dead again")
    session.call_tool = AsyncMock(side_effect=[exc1, exc2])

    wrapper = _make_read_only_wrapper(session)

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert "failed after retry" in output
    assert "ClosedResourceError" in output
    assert is_tool_error_result(output)
    assert session.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_tool_no_retry_on_non_transient_error():
    """Tool should NOT retry on non-transient errors like ValueError."""
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=ValueError("bad input"))

    wrapper = MCPToolWrapper(session, "test_server", _make_tool_def(), tool_timeout=5)
    output = await wrapper.execute()

    assert "ValueError" in output
    assert "retry" not in output
    assert session.call_tool.call_count == 1


@pytest.mark.asyncio
async def test_tool_no_retry_on_timeout():
    """Timeouts should not trigger retry (they have their own handling)."""
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())

    wrapper = MCPToolWrapper(session, "test_server", _make_tool_def(), tool_timeout=5)
    output = await wrapper.execute()

    assert "timed out" in output
    assert session.call_tool.call_count == 1


@pytest.mark.asyncio
async def test_tool_success_on_first_try_no_retry():
    """Normal success path — no retry logic involved."""
    session = AsyncMock()
    result = _make_tool_result("hello")
    session.call_tool = AsyncMock(return_value=result)

    wrapper = MCPToolWrapper(session, "test_server", _make_tool_def(), tool_timeout=5)
    output = await wrapper.execute()

    assert output == "hello"
    assert session.call_tool.call_count == 1


@pytest.mark.asyncio
async def test_tool_does_not_retry_on_cancelled_error():
    """`asyncio.CancelledError` must short-circuit the retry loop.

    Regression guard: the retry branch lives under ``except Exception``,
    but ``CancelledError`` inherits from ``BaseException``, not
    ``Exception``, so it naturally bypasses the retry branch today.  If a
    future refactor ever widens the retry branch to ``BaseException`` (or
    re-orders the handlers), ``/stop`` would start retrying instead of
    cancelling — this test pins that invariant.
    """
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=asyncio.CancelledError())

    wrapper = MCPToolWrapper(session, "test_server", _make_tool_def(), tool_timeout=5)

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        output = await wrapper.execute()

    assert "cancelled" in output
    assert session.call_tool.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_tool_retry_on_connection_reset():
    """ConnectionResetError (a stdlib exception) should also trigger retry."""
    session = AsyncMock()
    result = _make_tool_result("recovered")
    session.call_tool = AsyncMock(
        side_effect=[ConnectionResetError("reset by peer"), result]
    )

    wrapper = _make_read_only_wrapper(session)

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert output == "recovered"
    assert session.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_tool_retry_on_end_of_stream():
    """EndOfStream (anyio) should trigger retry."""
    session = AsyncMock()
    result = _make_tool_result("back")
    session.call_tool = AsyncMock(side_effect=[_FakeEndOfStreamError("eof"), result])

    wrapper = _make_read_only_wrapper(session)

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert output == "back"
    assert session.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_tool_reconnects_on_transient_failure():
    """Tool should reconnect when a stale session reports a transient stream failure."""
    old_session = AsyncMock()
    old_session.call_tool = AsyncMock(side_effect=_FakeClosedResourceError("closed"))
    new_session = AsyncMock()
    new_session.call_tool = AsyncMock(return_value=_make_tool_result("fresh"))

    wrapper = _make_read_only_wrapper(old_session)
    replacement = _make_read_only_wrapper(new_session)

    async def reconnect(server_name: str, tool_name: str, stale_tool):
        assert server_name == "test_server"
        assert tool_name == "mcp_test_server_test_tool"
        assert stale_tool is wrapper
        return replacement

    wrapper.set_reconnect_handler(reconnect)

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        output = await wrapper.execute(foo="bar")

    assert output == "fresh"
    assert old_session.call_tool.call_count == 1
    assert new_session.call_tool.call_count == 1
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# MCP annotations -> read_only/idempotent and the retry gate (MIT-1817)
# ---------------------------------------------------------------------------


def _trusted_wrapper(annotations, server_trusted=True):
    return MCPToolWrapper(
        AsyncMock(),
        "test_server",
        _make_tool_def(annotations=annotations),
        tool_timeout=5,
        trust_annotations=server_trusted,
    )


def test_trusted_read_only_hint_maps_to_read_only_and_idempotent():
    wrapper = _trusted_wrapper(_annotations(read_only=True))
    assert wrapper.read_only is True
    assert wrapper.idempotent is True


def test_trusted_idempotent_hint_only_maps_to_idempotent_not_read_only():
    wrapper = _trusted_wrapper(_annotations(idempotent=True))
    assert wrapper.read_only is False
    assert wrapper.idempotent is True


def test_untrusted_server_read_only_hint_is_ignored():
    wrapper = _trusted_wrapper(
        _annotations(read_only=True, idempotent=True),
        server_trusted=False,
    )
    assert wrapper.read_only is False
    assert wrapper.idempotent is False


def test_trusted_server_without_annotations_is_not_read_only_or_idempotent():
    wrapper = _trusted_wrapper(None)
    assert wrapper.read_only is False
    assert wrapper.idempotent is False


def test_untrusted_destructive_annotations_stay_writes():
    # Negative control: browser_act-style hints change nothing when the
    # server is not operator-trusted.
    wrapper = _trusted_wrapper(
        _annotations(read_only=False, idempotent=False, destructive=True),
        server_trusted=False,
    )
    assert wrapper.read_only is False
    assert wrapper.idempotent is False


def test_trusted_read_only_mcp_tool_is_not_concurrency_safe():
    wrapper = _trusted_wrapper(_annotations(read_only=True))
    assert wrapper.read_only is True
    assert wrapper.concurrency_safe is False


def test_default_base_tool_idempotent_follows_read_only():
    from nanobot.agent.tools.mcp import _MCPWrapperBase

    class _Stub(_MCPWrapperBase):
        _read_only = False

        @property
        def name(self):
            return "stub"

        @property
        def description(self):
            return "stub"

        @property
        def parameters(self):
            return {}

        @property
        def read_only(self):
            return self._read_only

        async def execute(self, **kwargs):
            return "ok"

    write_stub = _Stub()
    write_stub._set_mcp_connection(AsyncMock(), "srv")
    assert write_stub.read_only is False
    assert write_stub.idempotent is False

    read_stub = _Stub()
    read_stub._set_mcp_connection(AsyncMock(), "srv")
    read_stub._read_only = True
    assert read_stub.read_only is True
    assert read_stub.idempotent is True


@pytest.mark.asyncio
async def test_non_idempotent_tool_does_not_retry_on_transient_error():
    """A destructive tool runs once; the outcome may already have taken effect."""
    session = AsyncMock()
    exc = _FakeClosedResourceError("connection lost")
    session.call_tool = AsyncMock(side_effect=[exc, _make_tool_result("twice")])

    wrapper = MCPToolWrapper(
        session,
        "test_server",
        _make_tool_def(
            "browser_fill_form",
            annotations=_annotations(read_only=False, idempotent=False, destructive=True),
        ),
        tool_timeout=5,
        trust_annotations=True,
    )

    logged = []
    from loguru import logger

    sink_id = logger.add(lambda message: logged.append(str(message)))
    try:
        with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            output = await wrapper.execute(form="f")
    finally:
        logger.remove(sink_id)

    assert "outcome unknown" in output
    assert "do not repeat it" in output
    assert is_tool_error_result(output)
    assert session.call_tool.call_count == 1
    mock_sleep.assert_not_called()
    assert any(
        "mcp_retry_skipped" in line
        and "reason=not_idempotent" in line
        and "browser_fill_form" in line
        for line in logged
    )


@pytest.mark.asyncio
async def test_non_idempotent_tool_does_not_refresh_session_on_termination():
    """Session-refresh retry follows the same idempotency gate."""
    old_session = AsyncMock()
    old_session.call_tool = AsyncMock(side_effect=_session_terminated_error())
    new_session = AsyncMock()
    new_session.call_tool = AsyncMock(return_value=_make_tool_result("fresh"))

    # Untrusted server -> not idempotent even though the tool has hints.
    wrapper = MCPToolWrapper(
        old_session,
        "test_server",
        _make_tool_def(annotations=_annotations(read_only=True)),
        tool_timeout=5,
    )
    replacement = _make_read_only_wrapper(new_session)

    async def reconnect(server_name: str, tool_name: str, stale_tool):
        return replacement

    wrapper.set_reconnect_handler(reconnect)

    output = await wrapper.execute()

    assert "outcome unknown" in output
    assert is_tool_error_result(output)
    assert old_session.call_tool.call_count == 1
    assert new_session.call_tool.call_count == 0


@pytest.mark.asyncio
async def test_idempotent_hint_only_tool_retries_on_transient_error():
    """idempotentHint alone (not read-only) keeps the single transient retry."""
    session = AsyncMock()
    result = _make_tool_result("ok")
    session.call_tool = AsyncMock(side_effect=[_FakeClosedResourceError("gone"), result])

    wrapper = MCPToolWrapper(
        session,
        "test_server",
        _make_tool_def(annotations=_annotations(idempotent=True)),
        tool_timeout=5,
        trust_annotations=True,
    )

    assert wrapper.read_only is False
    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert output == "ok"
    assert session.call_tool.call_count == 2


@pytest.mark.asyncio
async def test_read_only_tool_reconnects_on_session_terminated():
    """Trusted read-only tools keep the session-refresh retry."""
    old_session = AsyncMock()
    old_session.call_tool = AsyncMock(side_effect=_session_terminated_error())
    new_session = AsyncMock()
    new_session.call_tool = AsyncMock(return_value=_make_tool_result("fresh"))

    wrapper = _make_read_only_wrapper(old_session)
    replacement = _make_read_only_wrapper(new_session)

    async def reconnect(server_name: str, tool_name: str, stale_tool):
        return replacement

    wrapper.set_reconnect_handler(reconnect)

    output = await wrapper.execute()

    assert output == "fresh"
    assert old_session.call_tool.call_count == 1
    assert new_session.call_tool.call_count == 1


# ---------------------------------------------------------------------------
# MCPResourceWrapper retry behaviour
# ---------------------------------------------------------------------------


def _make_resource_def(name="test_resource"):
    return SimpleNamespace(
        name=name,
        uri="file:///test",
        description="A test resource",
    )


def _make_resource_result(text):
    return SimpleNamespace(
        contents=[mcp_types.TextResourceContents(uri="file:///test", text=text)]
    )


@pytest.mark.asyncio
async def test_resource_retries_on_transient_error():
    """Resource should retry once on transient connection error."""
    session = AsyncMock()
    result = _make_resource_result("data")
    exc = _FakeClosedResourceError("gone")
    session.read_resource = AsyncMock(side_effect=[exc, result])

    wrapper = MCPResourceWrapper(session, "test_server", _make_resource_def())

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert output == "data"
    assert session.read_resource.call_count == 2


@pytest.mark.asyncio
async def test_resource_fails_after_retry_exhausted():
    """Resource should fail with retry message when both attempts fail."""
    session = AsyncMock()
    exc = _FakeClosedResourceError("dead")
    session.read_resource = AsyncMock(side_effect=[exc, exc])

    wrapper = MCPResourceWrapper(session, "test_server", _make_resource_def())

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert "failed after retry" in output
    assert session.read_resource.call_count == 2


@pytest.mark.asyncio
async def test_resource_no_retry_on_non_transient():
    """Resource should not retry on non-transient errors."""
    session = AsyncMock()
    session.read_resource = AsyncMock(side_effect=RuntimeError("bad"))

    wrapper = MCPResourceWrapper(session, "test_server", _make_resource_def())
    output = await wrapper.execute()

    assert "RuntimeError" in output
    assert session.read_resource.call_count == 1


@pytest.mark.asyncio
async def test_resource_reconnects_on_session_terminated():
    """Resource should reconnect once when the MCP SDK reports a dead session."""
    old_session = AsyncMock()
    old_session.read_resource = AsyncMock(side_effect=_session_terminated_error())
    new_session = AsyncMock()
    new_session.read_resource = AsyncMock(return_value=_make_resource_result("fresh"))

    wrapper = MCPResourceWrapper(old_session, "test_server", _make_resource_def())
    replacement = MCPResourceWrapper(new_session, "test_server", _make_resource_def())

    async def reconnect(server_name: str, tool_name: str, stale_tool):
        assert server_name == "test_server"
        assert tool_name == "mcp_test_server_resource_test_resource"
        assert stale_tool is wrapper
        return replacement

    wrapper.set_reconnect_handler(reconnect)

    output = await wrapper.execute()

    assert output == "fresh"
    assert old_session.read_resource.call_count == 1
    assert new_session.read_resource.call_count == 1


# ---------------------------------------------------------------------------
# MCPPromptWrapper retry behaviour
# ---------------------------------------------------------------------------


def _make_prompt_def(name="test_prompt"):
    return SimpleNamespace(
        name=name,
        description="A test prompt",
        arguments=[],
    )


def _make_prompt_result(text):
    return SimpleNamespace(
        messages=[
            SimpleNamespace(
                content=mcp_types.TextContent(type="text", text=text),
            )
        ]
    )


@pytest.mark.asyncio
async def test_prompt_retries_on_transient_error():
    """Prompt should retry once on transient connection error."""
    session = AsyncMock()
    result = _make_prompt_result("prompt text")
    exc = _FakeClosedResourceError("gone")
    session.get_prompt = AsyncMock(side_effect=[exc, result])

    wrapper = MCPPromptWrapper(session, "test_server", _make_prompt_def())

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert output == "prompt text"
    assert session.get_prompt.call_count == 2


@pytest.mark.asyncio
async def test_prompt_fails_after_retry_exhausted():
    """Prompt should fail with retry message when both attempts fail."""
    session = AsyncMock()
    exc = _FakeClosedResourceError("dead")
    session.get_prompt = AsyncMock(side_effect=[exc, exc])

    wrapper = MCPPromptWrapper(session, "test_server", _make_prompt_def())

    with patch("nanobot.agent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        output = await wrapper.execute()

    assert "failed after retry" in output
    assert session.get_prompt.call_count == 2


@pytest.mark.asyncio
async def test_prompt_no_retry_on_mcp_error():
    """McpError (application-level) should NOT trigger retry."""
    session = AsyncMock()
    session.get_prompt = AsyncMock(
        side_effect=McpError(ErrorData(code=-1, message="not found"))
    )

    wrapper = MCPPromptWrapper(session, "test_server", _make_prompt_def())
    output = await wrapper.execute()

    assert "not found" in output
    assert session.get_prompt.call_count == 1


@pytest.mark.asyncio
async def test_prompt_no_retry_on_non_transient():
    """Non-transient errors should not trigger retry for prompts."""
    session = AsyncMock()
    session.get_prompt = AsyncMock(side_effect=RuntimeError("bad"))

    wrapper = MCPPromptWrapper(session, "test_server", _make_prompt_def())
    output = await wrapper.execute()

    assert "RuntimeError" in output
    assert session.get_prompt.call_count == 1


@pytest.mark.asyncio
async def test_prompt_reconnects_on_session_terminated():
    """Prompt should reconnect once before falling back to McpError handling."""
    old_session = AsyncMock()
    old_session.get_prompt = AsyncMock(side_effect=_session_terminated_error())
    new_session = AsyncMock()
    new_session.get_prompt = AsyncMock(return_value=_make_prompt_result("fresh prompt"))

    wrapper = MCPPromptWrapper(old_session, "test_server", _make_prompt_def())
    replacement = MCPPromptWrapper(new_session, "test_server", _make_prompt_def())

    async def reconnect(server_name: str, tool_name: str, stale_tool):
        assert server_name == "test_server"
        assert tool_name == "mcp_test_server_prompt_test_prompt"
        assert stale_tool is wrapper
        return replacement

    wrapper.set_reconnect_handler(reconnect)

    output = await wrapper.execute()

    assert output == "fresh prompt"
    assert old_session.get_prompt.call_count == 1
    assert new_session.get_prompt.call_count == 1


@pytest.mark.asyncio
async def test_prompt_reconnects_on_connection_closed_exception():
    """Prompt should reconnect when the SDK reports a closed session as a generic exception."""
    old_session = AsyncMock()
    old_session.get_prompt = AsyncMock(side_effect=RuntimeError("Connection closed"))
    new_session = AsyncMock()
    new_session.get_prompt = AsyncMock(return_value=_make_prompt_result("fresh prompt"))

    wrapper = MCPPromptWrapper(old_session, "test_server", _make_prompt_def())
    replacement = MCPPromptWrapper(new_session, "test_server", _make_prompt_def())

    async def reconnect(server_name: str, tool_name: str, stale_tool):
        assert server_name == "test_server"
        assert tool_name == "mcp_test_server_prompt_test_prompt"
        assert stale_tool is wrapper
        return replacement

    wrapper.set_reconnect_handler(reconnect)

    output = await wrapper.execute()

    assert output == "fresh prompt"
    assert old_session.get_prompt.call_count == 1
    assert new_session.get_prompt.call_count == 1
