"""Per-turn browser tool-call cap (D5-14, NFR-MODEL-002)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.browser_budget import (
    ENVELOPE,
    MAX_BROWSER_CALLS_PER_TURN,
    browser_budget_error,
    is_browser_tool,
)
from nanobot.agent.tools.execution import execute_tool_calls
from nanobot.providers.base import ToolCallRequest

# The refusal envelope is pinned against the literal from the D5 design
# (docs/design/browser/D5-agent-tools.md §3.11), not against the module.
_EXPECTED_ENVELOPE = (
    '{"ok":false,"outcome":"blocked","source":"auth","error":'
    '{"code":"turn_budget_exhausted","class":"input",'
    '"message":"This turn has used 30 browser actions. Stop browsing now: '
    'tell the user what you found, what is left, and ask whether to continue.",'
    '"retryable":false,"human":"none","next":"conclude"}}'
)


def test_is_browser_tool():
    for name in (
        "mcp_ziggy_gmail_browser_open",
        "mcp_ziggy_linkedin_search_people",
        "mcp_vault_site_login",
        "mcp_vault_vault_list_sites",
        "browser_act",
        "linkedin_read_page",
        "site_login",
        "vault_list_sites",
        # Spec quirk (issue/design regex, lazy server segment): on a
        # multi-word server the strip lands on the first ``browser_``/
        # ``linkedin_`` anywhere in the remainder, so these count too.
        "mcp_ziggy_gmail_search_browser_history",
        "mcp_ziggy_gmail_linkedin_bridge_poll",
    ):
        assert is_browser_tool(name), name
    for name in (
        "mcp_ziggy_gmail_gmail_search",
        "web_fetch",
        "web_search",
        "exec",
        "mcp_ziggy_gmail_list_site_logins",
        "browserless_run",
        "site_login_status",
    ):
        assert not is_browser_tool(name), name


def test_31st_browser_call_is_refused():
    counts: dict[str, int] = {}
    assert MAX_BROWSER_CALLS_PER_TURN == 30
    for call_no in range(1, MAX_BROWSER_CALLS_PER_TURN + 1):
        assert browser_budget_error("mcp_ziggy_gmail_browser_open", counts) is None, call_no
    assert counts["browser:*"] == MAX_BROWSER_CALLS_PER_TURN

    refusal = browser_budget_error("mcp_ziggy_gmail_browser_open", counts)
    assert refusal == _EXPECTED_ENVELOPE
    assert refusal == ENVELOPE
    payload = json.loads(refusal)
    assert payload["error"]["code"] == "turn_budget_exhausted"
    assert payload["ok"] is False
    assert payload["outcome"] == "blocked"
    assert payload["error"]["next"] == "conclude"
    # Once exhausted, later browser calls stay refused and keep counting.
    assert browser_budget_error("mcp_ziggy_linkedin_read_page", counts) == ENVELOPE
    assert counts["browser:*"] == MAX_BROWSER_CALLS_PER_TURN + 2


def test_non_browser_calls_do_not_count():
    counts: dict[str, int] = {}
    for call_no in range(1, 101):
        assert browser_budget_error("web_fetch", counts) is None, call_no
        assert browser_budget_error("mcp_ziggy_gmail_gmail_search", counts) is None, call_no
    assert counts == {}

    # Interleaving non-browser calls must not consume browser budget.
    for _ in range(MAX_BROWSER_CALLS_PER_TURN):
        assert browser_budget_error("mcp_ziggy_gmail_browser_open", counts) is None
        assert browser_budget_error("web_fetch", counts) is None
    assert counts["browser:*"] == MAX_BROWSER_CALLS_PER_TURN
    assert browser_budget_error("mcp_ziggy_gmail_browser_open", counts) == ENVELOPE


@pytest.mark.asyncio
async def test_execute_tool_calls_refuses_browser_calls_past_the_budget():
    """The runner's real path: one batch, 31 browser calls interleaved with
    web_fetch calls; the 31st browser call returns the envelope as a plain
    string (no retry hint) and is never executed."""
    tool_calls = []
    for i in range(MAX_BROWSER_CALLS_PER_TURN + 1):
        tool_calls.append(
            ToolCallRequest(id=f"b{i}", name="mcp_ziggy_gmail_browser_open", arguments={})
        )
        tool_calls.append(
            ToolCallRequest(
                id=f"w{i}", name="web_fetch", arguments={"url": f"https://example.com/{i}"}
            )
        )
    tools = SimpleNamespace(execute=AsyncMock(return_value="ok"))
    external_lookup_counts: dict[str, int] = {}

    results, events, fatal_error = await execute_tool_calls(
        tools,
        tool_calls,
        concurrent=False,
        external_lookup_counts=external_lookup_counts,
        workspace_violation_counts={},
        hook=AgentHook(),
        context=AgentHookContext(iteration=0, messages=[]),
    )

    assert fatal_error is None
    # 30 browser calls + 31 web_fetch calls actually executed.
    assert tools.execute.await_count == MAX_BROWSER_CALLS_PER_TURN * 2 + 1
    browser_indices = [i for i, tc in enumerate(tool_calls) if i % 2 == 0]
    assert results[browser_indices[-1]] == ENVELOPE
    assert "Analyze the error above" not in results[browser_indices[-1]]
    assert events[browser_indices[-1]] == {
        "name": "mcp_ziggy_gmail_browser_open",
        "status": "error",
        "detail": "browser turn budget exhausted",
    }
    for i in browser_indices[:-1]:
        assert results[i] == "ok"
    assert external_lookup_counts["browser:*"] == MAX_BROWSER_CALLS_PER_TURN + 1
