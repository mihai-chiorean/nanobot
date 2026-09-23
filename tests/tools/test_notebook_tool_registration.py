"""MIT-1031: ``notebook_edit`` must auto-register via the ToolLoader and be
room-policy DENIED while remaining fully functional for the owner.

The cutover (MIT-1031) moved this tool from a hardcoded ``loop.py``
``register()`` call (0.2.x) to the 0.3.0 convention of discovering it from the
``pkgutil`` scan in ``ToolLoader.discover``.  The behaviour that *actually*
changed is registration + room-policy classification, so that is what is pinned
here — through the real loader, not a hand-constructed tool.

Why a loader test is the load-bearing one: the whole risk of a "port it"
cutover is shipping a tool that silently fails to appear in the registry (no
crash, the agent simply stops being able to call it).  A unit test that builds
``NotebookEditTool(...)`` directly would stay green even if the loader never
discovered the module.
"""

from __future__ import annotations

import json
import time
from typing import Any

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.notebook import NotebookEditTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.room_policy import (
    ROOM_ALLOWED_TOOLS,
    RoomPolicy,
    room_denial_message,
    room_policy_for,
)
from nanobot.channels.websocket.rooms import RoomCredential, room_scope_metadata
from nanobot.config.schema import ToolsConfig

_ROOM_ID = "room_" + "a" * 32
_ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
_PARTICIPANT = "participant_" + "b" * 32


def _make_notebook(source: str = "print('hi')") -> dict:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "cells": [
            {"cell_type": "code", "source": source, "metadata": {}, "outputs": [], "execution_count": None}
        ],
    }


def _ctx(tmp_path, *, file_enable: bool = True) -> Any:
    """A ToolContext that mirrors production (``tools.config.file.enable=true``).

    ``_FsTool.enabled`` reads ``config.file.enable``; the production default is
    True, so the enable path here is the same one the live agent uses, not an
    artificially-flipped flag.  The disabled path is exercised separately.
    """
    cfg = ToolsConfig()
    cfg.file.enable = file_enable
    return ToolContext(
        config=cfg,
        workspace=str(tmp_path),
        file_state_store=FileStates(),
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# Discovery + registration through the real pkgutil loader (the MIT-1031 change)
# ---------------------------------------------------------------------------


def test_notebook_tool_is_discovered_by_the_loader():
    """The module is not in ``_SKIP_MODULES`` and the concrete Tool subclass is
    found by the ``pkgutil`` scan that ``ToolLoader.discover`` performs."""
    discovered = {cls.__name__ for cls in ToolLoader().discover()}
    assert "NotebookEditTool" in discovered, (
        "NotebookEditTool was not discovered — the loader would silently omit "
        "notebook_edit from every agent"
    )


def test_notebook_tool_registers_through_the_loader_with_real_config(tmp_path):
    """End-to-end: real ``ToolsConfig`` (``file.enable=True`` default), real
    loader, real registry.  Asserts the tool is present in BOTH the loader's
    returned ``registered`` list AND ``registry.has('notebook_edit')`` — the
    returned list is what the old tautological tests checked."""
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    registered = ToolLoader().load(ctx, registry)

    assert "notebook_edit" in registered, (
        f"notebook_edit missing from loader output: {sorted(registered)}"
    )
    assert registry.has("notebook_edit") is True
    tool = registry.get("notebook_edit")
    assert isinstance(tool, NotebookEditTool)
    # The registered tool is the *real* class with its real schema — a stub
    # would not carry the cell-edit parameters the real tool exposes.
    assert tool.name == "notebook_edit"
    props = tool.parameters.get("properties", {})
    assert {"path", "cell_index", "new_source", "cell_type", "edit_mode"} <= set(props), (
        f"notebook_edit registered with an unexpected schema: {sorted(props)}"
    )


def test_file_tool_toggle_gates_registration_both_directions(tmp_path):
    """notebook_edit subclasses ``_FsTool`` so it shares the ``file.enable``
    gate.  Toggling that config flag must flip registration BOTH ways — this
    is the "file/classification toggles actually register/deregister" check."""
    enabled_registry = ToolRegistry()
    enabled = ToolLoader().load(_ctx(tmp_path, file_enable=True), enabled_registry)
    assert "notebook_edit" in enabled
    assert enabled_registry.has("notebook_edit") is True

    disabled_registry = ToolRegistry()
    disabled = ToolLoader().load(_ctx(tmp_path, file_enable=False), disabled_registry)
    assert "notebook_edit" not in disabled
    assert disabled_registry.has("notebook_edit") is False


# ---------------------------------------------------------------------------
# Room-policy classification: notebook_edit is a file-write tool → DENIED
# ---------------------------------------------------------------------------


def test_notebook_edit_is_room_denied():
    """A file-write tool that can edit arbitrary workspace paths must not be
    reachable from a guest turn.  It is deliberately NOT in the allow-list."""
    assert "notebook_edit" not in ROOM_ALLOWED_TOOLS
    assert room_policy_for("notebook_edit") is RoomPolicy.DENIED


def test_notebook_edit_denied_in_shared_room_reaches_gate_and_blocks(tmp_path):
    """The gate is ``ToolRegistry.prepare_call``, reached via the production
    ``_execute_tool_once``/``execute_tools`` path — never simulated by a
    hand-written ``allowed=False``.  Denied means the call returns an
    ``is_error`` result AND the on-disk notebook is left untouched."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    nb_path = store_dir / "nb.ipynb"
    original = _make_notebook("original = 1")
    nb_path.write_text(json.dumps(original), encoding="utf-8")

    registry = ToolRegistry()
    registered = ToolLoader().load(_ctx(store_dir), registry)
    assert "notebook_edit" in registered

    params = {"path": str(nb_path), "cell_index": 0, "new_source": "pwned = 1"}

    guest = RequestContext(
        channel="websocket",
        chat_id=_ROOM_CHAT,
        session_key=f"websocket:{_ROOM_CHAT}",
        metadata=room_scope_metadata(
            RoomCredential(
                expires_at=time.monotonic() + 300,
                room_id=_ROOM_ID,
                chat_id=_ROOM_CHAT,
                participant_id=_PARTICIPANT,
                display_name="Guest",
                role="contributor",
            )
        ),
    )
    with request_context(guest):
        _tool, _params, error = registry.prepare_call("notebook_edit", dict(params))
    assert isinstance(error, ToolResult) and error.is_error is True, (
        "notebook_edit was NOT blocked under a guest room context — the "
        "tool-write gate in prepare_call failed to fire"
    )
    assert "shared conversation" in str(error)
    # The definitive non-effect: the guest never reached the tool body, so the
    # notebook on disk is byte-for-byte unchanged.
    assert json.loads(nb_path.read_text(encoding="utf-8")) == original


def test_notebook_edit_allowed_outside_room_and_reaches_execution(tmp_path):
    """Owner / normal-context positive control — proves the deny above is
    room-*specific* and not a rename artifact that blocks the tool everywhere.
    Also proves the tool really works when NOT gated, so a hypothetical broken
    ``enabled()`` could not masquerade as 'the room policy correctly blocked
    it'."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    nb_path = store_dir / "nb.ipynb"
    nb_path.write_text(json.dumps(_make_notebook("original = 1")), encoding="utf-8")

    registry = ToolRegistry()
    registered = ToolLoader().load(_ctx(store_dir), registry)
    assert "notebook_edit" in registered

    params = {"path": str(nb_path), "cell_index": 0, "new_source": "edited = 2"}
    plain = RequestContext(channel="websocket", chat_id="c1", session_key="s1", metadata={})
    with request_context(plain):
        tool, validated_params, error = registry.prepare_call("notebook_edit", dict(params))
    assert error is None, f"owner-path prepare_call wrongly rejected {params!r}: {error}"
    assert tool is not None and tool.name == "notebook_edit"

    # The gate opened → the real tool body runs and mutates the workspace file
    # (the side effect, asserted on disk).  This is what a vacuous test would
    # skip: it proves args are not merely forwarded but actually applied.
    result = _run(tool, validated_params)
    assert not getattr(result, "is_error", False), f"execution failed: {result}"
    saved = json.loads(nb_path.read_text(encoding="utf-8"))
    assert saved["cells"][0]["source"] == "edited = 2"


def _run(tool, params):
    import asyncio

    return asyncio.run(tool.execute(**params))


def test_denial_message_names_the_tool_and_matches_conventions():
    """The denial surfaced to a guest must be error-shaped and identify the
    tool, so an agent can tell *which* capability was refused rather than
    getting an empty ``ToolResult('')``."""
    msg = room_denial_message("notebook_edit")
    assert "notebook_edit" in msg
    assert "unavailable in a shared conversation" in msg
