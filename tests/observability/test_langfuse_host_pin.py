"""Langfuse must never default its destination to somebody else's cloud.

`LANGFUSE_SECRET_KEY` alone used to switch tracing on, and the Langfuse SDK
falls back to its Cloud SaaS endpoint when `LANGFUSE_HOST` is unset — so a
dropped line in an env file, or an `EnvironmentFile=-` that no longer exists,
would have shipped every prompt and completion off-site with no log line.
"""

from __future__ import annotations

import pytest

from nanobot.observability.langfuse import (
    _detect_enabled,
    langfuse_destination_is_pinned,
)


def test_unset_host_is_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert langfuse_destination_is_pinned() is False


def test_blank_host_is_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "   ")
    assert langfuse_destination_is_pinned() is False


def test_explicit_host_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "http://edge-builder-1.local:3000")
    assert langfuse_destination_is_pinned() is True


def test_explicit_cloud_host_is_still_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloud is a legitimate target — it just has to be asked for by name."""
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    assert langfuse_destination_is_pinned() is True


def test_tracing_disabled_when_host_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-not-a-real-key")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert _detect_enabled() is False


def test_tracing_enabled_when_both_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langfuse")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-not-a-real-key")
    monkeypatch.setenv("LANGFUSE_HOST", "http://edge-builder-1.local:3000")
    assert _detect_enabled() is True
