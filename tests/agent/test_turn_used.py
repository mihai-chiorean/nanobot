"""TP-09 (MIT-1870): per-turn "Used:" source families and account_intent."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.loop import _TurnProvenanceHook
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.turn_provenance import TurnProvenance, account_intent, family_for
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _mcp_tool(server_name: str, original_name: str) -> SimpleNamespace:
    """A stand-in for an MCP wrapper (agent/tools/mcp.py keeps both fields)."""
    return SimpleNamespace(
        _server_name=server_name,
        _original_name=original_name,
        name=f"mcp_{server_name}_{original_name}",
    )


def _builtin(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


class TestFamilyFor:
    def test_mcp_wrapper_maps_by_original_name_not_wire_name(self):
        # mcp_ziggy_gmail_browser_open must be "browser", not "gmail": the
        # server key ziggy_gmail contains "gmail", so names cannot be split.
        tool = _mcp_tool("ziggy_gmail", "browser_open")
        assert family_for(tool) == ("browser", "Your signed-in sites", True)

    @pytest.mark.parametrize(
        "original_name",
        ["browser_open", "browser_find", "browser_act", "browser_fill_form"],
    )
    def test_browser_tool_family(self, original_name):
        assert family_for(_mcp_tool("browser", original_name)) == (
            "browser",
            "Your signed-in sites",
            True,
        )

    def test_gmail_tools_are_gmail(self):
        assert family_for(_mcp_tool("ziggy_gmail", "gmail_search")) == (
            "gmail",
            "Gmail",
            True,
        )

    def test_builtin_web_search(self):
        assert family_for(_builtin("web_search")) == ("web_search", "Web search", False)

    def test_web_pages_from_builtin_fetch_and_mcp_reader(self):
        assert family_for(_builtin("web_fetch")) == ("web_pages", "Web pages", False)
        assert family_for(_mcp_tool("browser", "browser_read_page")) == (
            "web_pages",
            "Web pages",
            False,
        )

    def test_logins(self):
        assert family_for(_mcp_tool("browser", "site_login")) == (
            "logins",
            "Saved logins",
            True,
        )
        assert family_for(_mcp_tool("browser", "vault_save")) == (
            "logins",
            "Saved logins",
            True,
        )

    def test_work_apps_and_scholarly(self):
        assert family_for(_mcp_tool("work", "work_app_invoke")) == (
            "work_apps",
            "Work apps",
            False,
        )
        assert family_for(_mcp_tool("scholar", "scholarly_search")) == (
            "scholarly",
            "Scholarly search",
            False,
        )

    def test_unknown_mcp_tool_is_private_connector(self):
        assert family_for(_mcp_tool("slack", "slack_post")) == (
            "mcp:slack",
            "Connector",
            True,
        )

    def test_unlisted_builtin_is_none(self):
        assert family_for(_builtin("read_file")) is None
        assert family_for(_builtin("exec")) is None

    def test_mcp_web_search_is_a_connector_not_the_builtin_family(self):
        # Built-ins are matched by name; an MCP tool of the same original
        # name falls to the connector default.
        assert family_for(_mcp_tool("searchco", "web_search")) == (
            "mcp:searchco",
            "Connector",
            True,
        )


class TestNoteToolResult:
    def test_first_use_order_and_upsert(self):
        record = TurnProvenance()
        record.note_tool_result(_builtin("web_search"), "ok")
        record.note_tool_result(_mcp_tool("ziggy_gmail", "gmail_search"), "ok")
        record.note_tool_result(_builtin("web_search"), "ok")
        assert record.used == [
            {"family": "web_search", "label": "Web search", "private": False, "calls": 2, "errors": 0},
            {"family": "gmail", "label": "Gmail", "private": True, "calls": 1, "errors": 0},
        ]
        assert record.other_steps == 0

    def test_error_call_counts_in_errors(self):
        record = TurnProvenance()
        record.note_tool_result(_mcp_tool("ziggy_gmail", "gmail_search"), "error")
        record.note_tool_result(_mcp_tool("ziggy_gmail", "gmail_read"), "ok")
        assert record.used == [
            {"family": "gmail", "label": "Gmail", "private": True, "calls": 2, "errors": 1},
        ]

    def test_unlisted_builtin_only_bumps_other_steps(self):
        record = TurnProvenance()
        record.note_tool_result(_builtin("read_file"), "ok")
        record.note_tool_result(_builtin("exec"), "ok")
        record.note_tool_result(_builtin("read_file"), "error")
        assert record.used == []
        assert record.other_steps == 3

    def test_web_pages_merges_builtin_and_mcp_entries(self):
        record = TurnProvenance()
        record.note_tool_result(_builtin("web_fetch"), "ok")
        record.note_tool_result(_mcp_tool("browser", "browser_read_page"), "ok")
        assert record.used == [
            {"family": "web_pages", "label": "Web pages", "private": False, "calls": 2, "errors": 0},
        ]

    def test_account_intent_is_a_stored_boolean(self):
        assert TurnProvenance(account_intent=account_intent("check my inbox")).account_intent is True
        assert TurnProvenance().account_intent is False


class TestAccountIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "how much is a flight to Honolulu",
            "what's the weather in Berlin",
            "summarize the theory of relativity",
        ],
    )
    def test_non_account_questions(self, text):
        assert account_intent(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            "check my inbox",
            "any new e-mail from Anna?",
            "did that confirmation arrive?",
            "I cannot log in to my bank",
            "where is my order",
        ],
    )
    def test_account_questions(self, text):
        assert account_intent(text) is True

    def test_empty_text(self):
        assert account_intent("") is False


class _ExecutedTool(Tool):
    def __init__(self, name: str, *, server_name: str | None = None, original_name: str | None = None, fail: bool = False) -> None:
        self._name = name
        if server_name is not None:
            self._server_name = server_name
            self._original_name = original_name or name
        self._fail = fail

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"test tool {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        if self._fail:
            raise RuntimeError("boom")
        return "result"


@pytest.mark.asyncio
async def test_runner_hook_counts_executed_calls_and_skips_refused():
    """The hook fires per executed call; a prepare_call refusal is not counted."""
    provider = MagicMock(spec=LLMProvider)
    calls = {"n": 0}

    async def chat_stream_with_retry(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="working",
                tool_calls=[
                    ToolCallRequest(id="c1", name="mcp_ziggy_gmail_gmail_search", arguments={}),
                    ToolCallRequest(id="c2", name="mcp_ziggy_gmail_browser_open", arguments={}),
                    ToolCallRequest(id="c3", name="read_file", arguments={}),
                    ToolCallRequest(id="c4", name="no_such_tool", arguments={}),
                ],
            )
        return LLMResponse(content="done", tool_calls=[], usage=None)

    provider.chat_stream_with_retry = chat_stream_with_retry
    registry = ToolRegistry()
    registry.register(
        _ExecutedTool("mcp_ziggy_gmail_gmail_search", server_name="ziggy_gmail", original_name="gmail_search")
    )
    registry.register(
        _ExecutedTool("mcp_ziggy_gmail_browser_open", server_name="ziggy_gmail", original_name="browser_open", fail=True)
    )
    registry.register(_ExecutedTool("read_file"))

    record = TurnProvenance(account_intent=True)
    hook = _TurnProvenanceHook(record, registry)
    runner_spec = make_run_spec(
        provider,
        initial_messages=[],
        tools=registry,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        hook=hook,
        concurrent_tools=False,
    )
    from nanobot.agent.runner import AgentRunner

    result = await AgentRunner().run(runner_spec)
    assert result.final_content == "done"
    # The refused call (unknown tool name) appears in none of the counts.
    assert record.used == [
        {"family": "gmail", "label": "Gmail", "private": True, "calls": 1, "errors": 0},
        {"family": "browser", "label": "Your signed-in sites", "private": True, "calls": 1, "errors": 1},
    ]
    assert record.other_steps == 1
