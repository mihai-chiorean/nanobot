"""TP-09 (MIT-1870): ``turn_end`` carries ``used`` and ``other_steps``."""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.bus.outbound_events import TurnEndEvent
from nanobot.webui.outbound_wire import encode_turn_end

ROOT = Path(__file__).resolve().parents[2]
_USED = [
    {"family": "gmail", "label": "Gmail", "private": True, "calls": 2, "errors": 0},
    {"family": "web_search", "label": "Web search", "private": False, "calls": 1, "errors": 0},
]


def test_encode_includes_used_and_other_steps_when_set():
    payload = encode_turn_end(
        "chat",
        TurnEndEvent(used=[dict(entry) for entry in _USED], other_steps=3),
        {"webui_turn_id": "t1"},
    )
    assert payload["event"] == "turn_end"
    assert payload["chat_id"] == "chat"
    assert payload["turn_id"] == "t1"
    assert payload["used"] == _USED
    assert payload["other_steps"] == 3


def test_encode_omits_used_and_other_steps_when_absent():
    payload = encode_turn_end("chat", TurnEndEvent(), {"webui_turn_id": "t2"})
    assert payload == {"event": "turn_end", "chat_id": "chat", "turn_id": "t2"}
    # An empty list / zero steps means the turn ran no tools: no keys.
    payload = encode_turn_end("chat", TurnEndEvent(used=[], other_steps=0), None)
    assert payload == {"event": "turn_end", "chat_id": "chat"}


def test_encode_copies_entries_so_the_wire_frame_cannot_mutate_the_record():
    entry = {"family": "gmail", "label": "Gmail", "private": True, "calls": 1, "errors": 0}
    payload = encode_turn_end("chat", TurnEndEvent(used=[entry]), None)
    payload["used"][0]["calls"] = 99
    assert entry["calls"] == 1


def test_encode_output_matches_the_new_fixtures():
    fixtures = json.loads((ROOT / "packages/client-events/fixtures.json").read_text())
    turn_end_fixtures = [fixture for fixture in fixtures if fixture["event"] == "turn_end"]
    # Gmail plus web search / no tools / old runtime without used.
    assert len(turn_end_fixtures) == 3
    for fixture in turn_end_fixtures:
        fields = {
            key: value for key, value in fixture.items() if key not in {"event", "chat_id", "turn_id"}
        }
        metadata = {"webui_turn_id": fixture["turn_id"]} if "turn_id" in fixture else None
        assert encode_turn_end(fixture["chat_id"], TurnEndEvent(**fields), metadata) == fixture
