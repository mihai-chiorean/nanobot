"""Tool-hint strings must match production (``origin/feat/shared-rooms``, d8686664).

The iOS activity row renders ``summary = format_tool_hints([tool_call])`` on
tool-event frames (MIT-1417, nanobot #80), so 0.3.0 drift here is user-visible
(MIT-1426). Expected strings were captured by executing the production
formatter (``git show origin/feat/shared-rooms:nanobot/utils/tool_hints.py``)
on the same inputs — not hand-typed.

The ``find_files`` and long-query rows pin the *current* 0.3.0 wording
on purpose: those differences from production are intentional upstream
changes (480ca28a added the live ``find_files`` tool with its ``find {}``
hint; eddfa0dd added the 40-char truncation to stop long values overflowing
``tool_hint_max_length`` and being pushed to the chat/UI verbatim). Production
output for them, for the record:

* ``find_files({"query": "foo"})`` → production ``'find_files("foo")'``
  (production has no ``find_files`` entry; it fell through to the generic
  ``name + "(" + args + ")`` fallback)
* ``web_search`` long query → production emitted the full query verbatim
"""

from __future__ import annotations

import pytest

from nanobot.providers.base import ToolCallRequest
from nanobot.utils.tool_hints import format_tool_hints

# Longer than the formatter's 40-char hint budget, so upstream eddfa0dd
# truncates it; production (pre-40-char-truncation) did not.
LONG_QUERY = (
    "coffee shops near Golden Gate Park in Berkeley that open before 7am "
    "and roast their own beans"
)


def _summary(name: str, arguments: dict) -> str:
    call = ToolCallRequest(id="c1", name=name, arguments=arguments)
    return format_tool_hints([call])


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        # -- briefing action mapping (production d8686664; user-visible) --
        ("briefing", {"action": "create"}, "Creating your briefing"),
        ("briefing", {"action": "update"}, "Updating future editions"),
        ("briefing", {"action": "pause"}, "Pausing your briefing"),
        ("briefing", {"action": "resume"}, "Resuming your briefing"),
        ("briefing", {"action": "regenerate"}, "Requesting a new edition"),
        ("briefing", {"action": "feedback"}, "Saving edition feedback"),
        ("briefing", {"action": "inspect"}, "Checking your briefing"),
        ("briefing", {"action": "frobnicate"}, "Checking your briefing"),  # unknown action
        ("briefing", {}, "Checking your briefing"),  # missing action
        # -- glob: production registers the 'glob "{}"' template (is_path=False) --
        ("glob", {"pattern": "**/*.py"}, 'glob "**/*.py"'),
        # -- kept upstream, intentionally NOT production text (see module docstring) --
        # production: 'find_files("foo")' via the generic fallback
        ("find_files", {"query": "foo"}, "find foo"),
        # production: the full query verbatim; short queries are unaffected
        # by the truncation and pin the search "{}" format as a control
        ("web_search", {"query": "weather in berlin"}, 'search "weather in berlin"'),
        ("web_search", {"query": LONG_QUERY}, 'search "coffee shops near Golden Gate Park in B…"'),
        # -- unchanged formats: pinned so the port cannot shift them --
        ("exec", {"command": "ls -la"}, "$ ls -la"),
        ("read_file", {"path": "notes.md"}, "read notes.md"),
        ("mcp_github__create_issue", {"title": "Fix bug"}, 'github::create_issue("Fix bug")'),
        ("mcp_github__create_issue", {}, "github::create_issue"),
    ],
)
def test_tool_hint_matches_production(name: str, arguments: dict, expected: str) -> None:
    assert _summary(name, arguments) == expected
