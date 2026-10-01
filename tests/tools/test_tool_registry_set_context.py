"""Regression tests for ToolRegistry.set_context — MIT-138 / MIT-142.

Covers the sender-id propagation path from the registry into the filesystem
tools module.  MIT-142: the mirror is a ``contextvars.ContextVar``, not a
module-level mutable, so concurrent turns cannot leak sender identity into
each other's context.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextvars import ContextVar

from nanobot.agent.tools import filesystem as _fs
from nanobot.agent.tools.registry import ToolRegistry


def test_set_context_does_not_raise() -> None:
    """set_context must complete cleanly — no AttributeError from the
    `_fs._current_sender_id.set(sender_id)` call."""
    registry = ToolRegistry()
    # No exception should surface here.
    registry.set_context(session_id="sess-1", channel="slack", sender_id="user-42")


def test_set_context_propagates_sender_to_filesystem_module() -> None:
    """After set_context, the filesystem module's sender-id mirror must match
    the value the registry was given."""
    registry = ToolRegistry()
    registry.set_context(session_id="sess-2", channel="matrix", sender_id="alice")

    assert _fs._current_sender_id.get() == "alice"
    assert _fs.current_sender_id() == "alice"

    # Subsequent calls overwrite, not append.
    registry.set_context(session_id="sess-3", channel="matrix", sender_id="bob")
    assert _fs._current_sender_id.get() == "bob"
    assert _fs.current_sender_id() == "bob"


def test_set_context_stores_fields_on_registry() -> None:
    """set_context must persist the full triple on the registry too, so
    per-call audit logging can fall back to them when no override is given."""
    registry = ToolRegistry()
    registry.set_context(session_id="sess-4", channel="teams", sender_id="carol")

    assert registry._session_id == "sess-4"
    assert registry._channel == "teams"
    assert registry._sender_id == "carol"


def test_set_context_default_sender_id_is_empty_string() -> None:
    """sender_id is optional — calling set_context without it must not raise
    and must normalise to an empty string in the filesystem module (matches
    the ContextVar's documented default)."""
    registry = ToolRegistry()
    # Start from a known non-default value so the reset to "" is proven,
    # not just inherited from a fresh context.
    registry.set_context(session_id="sess-5", channel="cli", sender_id="dave")
    assert _fs.current_sender_id() == "dave"

    registry.set_context(session_id="sess-5", channel="cli")

    assert registry._sender_id == ""
    assert _fs._current_sender_id.get() == ""
    assert _fs.current_sender_id() == ""


def test_filesystem_module_defines_current_sender_id_at_import() -> None:
    """Guard the module-level symbol itself — importing the filesystem
    module without first calling set_context must still leave the symbol
    defined, as a ContextVar with its documented default value."""
    assert hasattr(_fs, "_current_sender_id")
    assert isinstance(_fs._current_sender_id, ContextVar)
    assert isinstance(_fs.current_sender_id(), str)
    # The documented default: reading without a set_context in the current
    # context yields the empty string, not AttributeError.  A fresh empty
    # Context proves this independently of earlier tests' writes.
    assert contextvars.Context().run(_fs.current_sender_id) == ""


async def test_concurrent_set_context_does_not_leak_between_tasks() -> None:
    """The MIT-142 regression: two concurrent turns setting different
    sender ids must each observe their own value.

    A module-level global cannot pass this: the second task's write would
    overwrite the first task's value before it reads.  A ContextVar scopes
    the value per task, so each task reads back what it set.  The barrier
    forces the interleaving (both tasks write before either reads).
    """
    barrier = asyncio.Barrier(2)

    async def run_turn(sender_id: str) -> str:
        registry = ToolRegistry()
        registry.set_context(session_id=f"sess-{sender_id}", channel="cli", sender_id=sender_id)
        await barrier.wait()  # both tasks have set their sender by now
        return _fs.current_sender_id()

    alice_seen, bob_seen = await asyncio.gather(run_turn("alice"), run_turn("bob"))

    assert alice_seen == "alice"
    assert bob_seen == "bob"
