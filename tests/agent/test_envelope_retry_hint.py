"""Retry-hint suppression for do-not-retry failure envelopes (D5-15, FR-FAIL-004).

The envelopes and the hint text are pinned to the literals in the D5 design
(docs/design/browser/D5-agent-tools.md §3.4/§3.11), not imported from the
modules under test, so the evidence is independent of the fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.execution import _envelope_forbids_retry, execute_tool_calls
from nanobot.providers.base import ToolCallRequest

_RETRY_HINT = "\n\n[Analyze the error above and try a different approach.]"

# Design-shaped conclude envelope: the tool says "Do not retry this URL", so
# the generic "try a different approach" hint contradicts it.
_CONCLUDE_ENVELOPE = (
    '{"ok":false,"outcome":"blocked","source":"policy","error":'
    '{"code":"site_not_permitted","class":"input","message":'
    '"This site is not in the permitted list. Do not retry this URL.",'
    '"retryable":false,"human":"none","next":"conclude"}}'
)
_ASK_USER_ENVELOPE = (
    '{"ok":false,"outcome":"needs_approval","source":"auth","error":'
    '{"code":"needs_approval","class":"input","message":'
    '"Sign-in needs approval. Ask the user to approve the credential fill.",'
    '"retryable":false,"human":"answer","next":"ask_user"}}'
)
_HAND_OFF_ENVELOPE = (
    '{"ok":false,"outcome":"failed","source":"automation","error":'
    '{"code":"captcha","class":"input","message":'
    '"A human must solve this challenge.",'
    '"retryable":false,"human":"solve","next":"hand_off"}}'
)
_RETRY_ENVELOPE = (
    '{"ok":false,"outcome":"failed","source":"network","error":'
    '{"code":"timeout","class":"transient","message":"The page did not load.",'
    '"retryable":true,"retry_after_seconds":5,"human":"none","next":"retry"}}'
)
_PLAIN_ERROR = "Error: TimeoutError: page timed out after 30s"


async def _run_once(result: ToolResult) -> tuple[object, dict[str, str]]:
    """Drive one call through the real execute_tool_calls path (non-concurrent)."""
    tools = SimpleNamespace(execute=AsyncMock(return_value=result))
    results, events, fatal_error = await execute_tool_calls(
        tools,
        [ToolCallRequest(id="c1", name="web_fetch", arguments={"url": "https://example.com"})],
        concurrent=False,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=AgentHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )
    assert fatal_error is None
    assert events[0]["status"] == "error"
    return results[0], events[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [_CONCLUDE_ENVELOPE, _ASK_USER_ENVELOPE, _HAND_OFF_ENVELOPE])
async def test_hint_suppressed_for_conclude_envelope(envelope: str):
    payload, _event = await _run_once(ToolResult.error(envelope))
    assert payload == envelope
    assert "Analyze the error above" not in payload


@pytest.mark.asyncio
async def test_hint_kept_for_retry_envelope():
    payload, _event = await _run_once(ToolResult.error(_RETRY_ENVELOPE))
    assert payload == _RETRY_ENVELOPE + _RETRY_HINT


@pytest.mark.asyncio
async def test_hint_kept_for_plain_error_text():
    payload, _event = await _run_once(ToolResult.error(_PLAIN_ERROR))
    assert payload == _PLAIN_ERROR + _RETRY_HINT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_CONCLUDE_ENVELOPE, True),
        (_ASK_USER_ENVELOPE, True),
        (_HAND_OFF_ENVELOPE, True),
        (_RETRY_ENVELOPE, False),
        (_PLAIN_ERROR, False),
        # Negative controls: near-misses must NOT suppress the hint.
        ("not json at all, mentions conclude", False),
        ('{"ok": false, "error": {"next": "conclude"', False),  # truncated JSON
        ('["ok", false, "conclude"]', False),  # valid JSON, not a dict
        ('{"ok": false, "error": "conclude"}', False),  # error is not a dict
        ('{"ok": false}', False),  # no error at all
        ('{"ok": false, "error": {"next": "retry"}}', False),
        ('{"ok": false, "error": {}}', False),  # no next
        ('{"ok": "false", "error": {"next": "conclude"}}', False),  # ok must be exactly False
        ('{"ok": 0, "error": {"next": "conclude"}}', False),
        ('{"error": {"next": "conclude"}}', False),  # missing ok
        ('{"ok": false, "error": {"next": "give_up"}}', False),  # unknown next
        ("", False),
    ],
)
def test_envelope_forbids_retry_classifies_exactly(text: str, expected: bool):
    assert _envelope_forbids_retry(text) is expected
