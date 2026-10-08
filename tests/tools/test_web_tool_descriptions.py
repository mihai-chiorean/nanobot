"""Pin the web_search / web_fetch descriptions (MIT-1799).

The descriptions must lead with what to use the tool for so a public-fact
question (prices, fares, news, schedules) gets a positive signal pointing at
the web instead of the user's private accounts.
"""

from __future__ import annotations

from nanobot.agent.tools.web import WebFetchTool, WebSearchTool

WEB_SEARCH_DESCRIPTION = (
    "Search the public web for current facts: prices, fares, news, schedules, docs, "
    "releases. Start here for anything that isn't in the user's own accounts. "
    "Returns titles, URLs and snippets (count defaults to 5, max 10); snippets aren't "
    "live quotes, so use web_fetch on a result to confirm. Some providers support "
    "timeRange, authLevel and queryRewrite."
)

WEB_FETCH_DESCRIPTION = (
    "Fetch one public URL over HTTP and extract readable content "
    "(HTML → markdown/text). Output is capped at maxChars (default 50 000). "
    "Fast; try this before browser_read_page. "
    "May fail on login-walled or JS-heavy sites."
)


def test_web_search_description_points_public_facts_at_the_web() -> None:
    description = WebSearchTool.description
    assert "prices, fares" in description
    assert "isn't in the user's own accounts" in description


def test_web_fetch_description_orders_it_before_browser_read_page() -> None:
    description = WebFetchTool.description
    assert "before browser_read_page" in description


def test_web_search_description_is_the_pinned_text() -> None:
    assert WebSearchTool.description == WEB_SEARCH_DESCRIPTION


def test_web_fetch_description_is_the_pinned_text() -> None:
    assert WebFetchTool.description == WEB_FETCH_DESCRIPTION


def test_descriptions_stay_short() -> None:
    assert len(WebSearchTool.description) < 600
    assert len(WebFetchTool.description) < 600
