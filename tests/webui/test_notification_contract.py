import importlib.util
import io
import json
import tarfile
from pathlib import Path

from nanobot.bus.outbound_events import TurnEndEvent
from nanobot.events import ContextCompactionEvent, RecoveryStateEvent, RetryStatusEvent
from nanobot.webui.outbound_wire import encode_turn_end, project_notification

ROOT = Path(__file__).resolve().parents[2]


def test_wire_fixtures_are_real_python_projections(monkeypatch):
    monkeypatch.setattr("nanobot.webui.outbound_wire.time.time", lambda: 100.0)
    fixtures = json.loads((ROOT / "packages/client-events/fixtures.json").read_text())
    for expected in fixtures:
        if expected["event"] == "turn_end":
            # The turn_end contract is the frame encoder, not a notification
            # projection: metadata carries the wire turn id, exactly as the
            # websocket channel hands it to ``encode_turn_end``.
            fields = {
                key: value
                for key, value in expected.items()
                if key not in {"event", "chat_id", "turn_id"}
            }
            metadata = (
                {"webui_turn_id": expected["turn_id"]}
                if "turn_id" in expected
                else None
            )
            assert encode_turn_end(expected["chat_id"], TurnEndEvent(**fields), metadata) == expected
            continue
        fields = {key: value for key, value in expected.items() if key not in {"event", "chat_id"}}
        event_type = {
            "context_compaction": ContextCompactionEvent,
            "recovery_state": RecoveryStateEvent,
            "retry_status": RetryStatusEvent,
        }[expected["event"]]
        if "retry_after_s" in fields:
            fields["next_retry_at"] = 100.0 + fields.pop("retry_after_s")
        projection = project_notification(expected["chat_id"], event_type(**fields))
        assert projection is not None
        assert projection.payload == expected


def test_turn_end_fixtures_cover_the_used_line_contracts():
    fixtures = json.loads((ROOT / "packages/client-events/fixtures.json").read_text())
    turn_end = [fixture for fixture in fixtures if fixture["event"] == "turn_end"]
    assert len(turn_end) == 3
    with_used = [fixture for fixture in turn_end if "used" in fixture]
    # Gmail plus web search, a no-tools turn, and an old runtime without used.
    assert len(with_used) == 1
    assert [entry["family"] for entry in with_used[0]["used"]] == ["gmail", "web_search"]
    assert all("used" not in fixture for fixture in turn_end if fixture is not with_used[0])


def test_tui_source_archive_preserves_shared_module_import_path():
    spec = importlib.util.spec_from_file_location("package_release", ROOT / "tui/scripts/package-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tarfile.open(fileobj=io.BytesIO(module._source_archive(ROOT / "tui"))) as archive:
        names = set(archive.getnames())
        assert "nanobot-tui-source/tui/src/protocol.ts" in names
        assert "nanobot-tui-source/packages/client-events/notifications.ts" in names
        assert "nanobot-tui-source/tui/bun.lock" in names
        assert "nanobot-tui-source/LICENSE" in names
