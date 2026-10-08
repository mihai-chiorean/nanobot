"""Tests for skill draft overlays: check, diff, accept, reject and the offer message."""

from pathlib import Path
from typing import Any

import pytest

from nanobot.agent import skill_drafts
from nanobot.bus.events import INBOUND_META_ROOM_SCOPE, OutboundMessage


@pytest.fixture(autouse=True)
def _no_shared_history():
    skill_drafts._HISTORY = None
    yield
    skill_drafts._HISTORY = None


def _skill_md(name: str, description: str = "Does a helpful thing") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nSteps.\n"


def _write_files(base: Path, files: dict[str, bytes | str]) -> None:
    for rel, data in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)


def _write_live(ws: Path, name: str, files: dict[str, bytes | str]) -> None:
    _write_files(ws / "skills" / name, files)


def _write_draft(ws: Path, name: str, files: dict[str, bytes | str]) -> None:
    _write_files(ws / "skills" / skill_drafts.PROPOSED_DIR / name, files)


def _read(ws: Path, rel: str) -> str:
    return (ws / rel).read_text(encoding="utf-8")


class FakeHistory:
    """Mimics the SM-02 WorkspaceHistory contract: on_commit list + commit_if_changed."""

    def __init__(self) -> None:
        self.on_commit: list[Any] = []
        self.commits: list[dict[str, Any]] = []

    def commit_if_changed(self, subject: str, *, session_key: str | None = None,
                          channel: str | None = None) -> str:
        return self._commit(subject, session_key=session_key, channel=channel, changed_paths=[])

    # Turn/Dream commits in production carry the changed paths computed by git;
    # tests supply them explicitly the way the SM-02 commit path would.
    def record_turn_commit(self, subject: str, *, session_key: str | None = None,
                           channel: str | None = None, changed_paths: list[str]) -> str:
        return self._commit(subject, session_key=session_key, channel=channel,
                            changed_paths=changed_paths)

    def _commit(self, subject: str, *, session_key: str | None, channel: str | None,
                changed_paths: list[str]) -> str:
        sha = f"{len(self.commits):040x}"
        lines = [subject, ""]
        if session_key:
            lines.append(f"session: {session_key}")
        if channel:
            lines.append(f"channel: {channel}")
        if changed_paths:
            lines.append(f"files: {','.join(changed_paths)}")
        message = "\n".join(lines)
        self.commits.append({"sha": sha, "subject": subject, "message": message})
        for callback in list(self.on_commit):
            callback(sha, changed_paths)
        return sha

    @property
    def latest_subject(self) -> str | None:
        return self.commits[-1]["subject"] if self.commits else None


class FakeSessions:
    """SessionManager stand-in: list_sessions() + read_session_file()."""

    def __init__(self, sessions: dict[str, dict[str, Any]]) -> None:
        # sessions: key -> {"updated_at": str, "metadata": dict}
        self._sessions = sessions

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {"key": key, "created_at": info["updated_at"], "updated_at": info["updated_at"],
             "title": "", "preview": "", "path": ""}
            for key, info in self._sessions.items()
        ]

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        info = self._sessions.get(key)
        if info is None:
            return None
        return {"key": key, "metadata": dict(info.get("metadata", {}))}


# ---------------------------------------------------------------------------
# accept / reject
# ---------------------------------------------------------------------------


def test_accept_overlays_and_keeps_untouched_live_files(tmp_path: Path) -> None:
    _write_live(
        tmp_path,
        "demo",
        {
            "SKILL.md": _skill_md("demo"),
            "references/notes.md": "keep me\n",
            "assets/x.png": b"\x89PNG\r\n\x1a\nbinary",
        },
    )
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo", "Updated description")})

    problems = skill_drafts.accept(tmp_path, "demo")

    assert problems == []
    assert "Updated description" in _read(tmp_path, "skills/demo/SKILL.md")
    assert _read(tmp_path, "skills/demo/references/notes.md") == "keep me\n"
    assert (tmp_path / "skills/demo/assets/x.png").read_bytes() == b"\x89PNG\r\n\x1a\nbinary"
    assert not (tmp_path / "skills/_proposed/demo").exists()


def test_accept_new_skill_creates_dir(tmp_path: Path) -> None:
    _write_draft(tmp_path, "new-skill", {"SKILL.md": _skill_md("new-skill")})

    assert skill_drafts.accept(tmp_path, "new-skill") == []
    assert _read(tmp_path, "skills/new-skill/SKILL.md").startswith("---")
    assert skill_drafts.list_drafts(tmp_path) == []


def test_accept_refuses_invalid_frontmatter(tmp_path: Path) -> None:
    _write_draft(tmp_path, "broken", {"SKILL.md": "# no frontmatter at all\n"})

    problems = skill_drafts.accept(tmp_path, "broken")

    assert problems
    assert any("frontmatter" in problem.lower() for problem in problems)
    assert not (tmp_path / "skills/broken").exists()
    assert (tmp_path / "skills/_proposed/broken/SKILL.md").exists()


def test_accept_refuses_frontmatter_name_mismatch(tmp_path: Path) -> None:
    _write_draft(tmp_path, "right-name", {"SKILL.md": _skill_md("wrong-name")})

    problems = skill_drafts.accept(tmp_path, "right-name")

    assert any("wrong-name" in problem for problem in problems)
    assert not (tmp_path / "skills/right-name").exists()


def test_check_flags_illegal_root_file_in_live_skill(tmp_path: Path) -> None:
    """quick_validate's root-entry rule runs on the MERGED view: a live-only
    state.json still blocks the draft (accept refuses)."""
    _write_live(tmp_path, "demo", {"SKILL.md": _skill_md("demo"), "state.json": "{}"})
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo", "Another description")})

    problems = skill_drafts.check(tmp_path, "demo")

    assert any("state.json" in problem for problem in problems)
    assert skill_drafts.accept(tmp_path, "demo") != []


def test_check_passes_valid_draft_with_resource_dirs(tmp_path: Path) -> None:
    """Negative control: a legitimate draft must produce no problems."""
    _write_live(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    _write_draft(tmp_path, "demo", {"scripts/run.py": "print('hi')\n"})

    assert skill_drafts.check(tmp_path, "demo") == []


def test_reject_deletes_draft_only(tmp_path: Path) -> None:
    _write_live(tmp_path, "demo", {"SKILL.md": _skill_md("demo"), "scripts/keep.py": "x = 1\n"})
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo", "Changed")})

    assert skill_drafts.reject(tmp_path, "demo") is True
    assert _read(tmp_path, "skills/demo/SKILL.md") == _skill_md("demo")
    assert _read(tmp_path, "skills/demo/scripts/keep.py") == "x = 1\n"
    assert not (tmp_path / "skills/_proposed/demo").exists()
    assert skill_drafts.reject(tmp_path, "demo") is False


def test_accept_rejects_path_traversal_names(tmp_path: Path) -> None:
    assert skill_drafts.accept(tmp_path, "../evil") != []
    assert skill_drafts.accept(tmp_path, "") != []


# ---------------------------------------------------------------------------
# list / merged / diff
# ---------------------------------------------------------------------------


def test_list_drafts_sorted(tmp_path: Path) -> None:
    _write_draft(tmp_path, "zeta", {"SKILL.md": _skill_md("zeta")})
    _write_draft(tmp_path, "alpha", {"SKILL.md": _skill_md("alpha")})

    assert skill_drafts.list_drafts(tmp_path) == ["alpha", "zeta"]


def test_merged_view_overlays_draft_on_live(tmp_path: Path) -> None:
    _write_live(tmp_path, "demo", {"SKILL.md": "live", "references/keep.md": "keep"})
    _write_draft(tmp_path, "demo", {"SKILL.md": "draft"})

    merged = skill_drafts.merged_view(tmp_path, "demo")

    assert merged == {"SKILL.md": b"draft", "references/keep.md": b"keep"}


def test_diff_shows_overlay_and_caps_at_6000(tmp_path: Path) -> None:
    _write_live(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo") + "\n".join("big line" for _ in range(2000))})

    text = skill_drafts.diff(tmp_path, "demo")

    assert len(text) <= 6000
    assert "diff truncated" in text

    _write_draft(tmp_path, "tiny", {"SKILL.md": _skill_md("tiny")})
    small = skill_drafts.diff(tmp_path, "tiny")
    assert "+---" in small or "+name: tiny" in small


def test_diff_binary_file_line(tmp_path: Path) -> None:
    _write_draft(tmp_path, "binny", {"SKILL.md": _skill_md("binny"), "assets/x.bin": b"\xff\xfe\x80\x00"})

    text = skill_drafts.diff(tmp_path, "binny")

    assert "Binary file assets/x.bin added" in text


def test_draft_names_touched_only_counts_proposed():
    assert skill_drafts.draft_names_touched(
        ["memory/MEMORY.md", "skills/demo/SKILL.md", "skills/_proposed/demo/SKILL.md",
         "skills/_proposed/other/scripts/x.py", "skills/_proposed"]
    ) == ["demo", "other"]


# ---------------------------------------------------------------------------
# offers (SM-02 on_commit callback)
# ---------------------------------------------------------------------------


def _collect_publish() -> tuple[list[OutboundMessage], Any]:
    published: list[OutboundMessage] = []
    return published, published.append


def test_turn_draft_offer_goes_to_origin_session(tmp_path: Path) -> None:
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    published, publish = _collect_publish()
    history = FakeHistory()
    skill_drafts.wire(
        history,
        workspace=tmp_path,
        publish=publish,
        sessions=FakeSessions({}),
        commit_message=lambda sha: history.commits[-1]["message"],
    )

    history.record_turn_commit(
        "turn t-77",
        session_key="telegram:4242",
        channel="telegram",
        changed_paths=["skills/_proposed/demo/SKILL.md"],
    )

    assert len(published) == 1
    message = published[0]
    assert (message.channel, message.chat_id) == ("telegram", "4242")
    assert "demo" in message.content
    assert "Does a helpful thing" in message.content
    assert "new skill" in message.content
    assert "/skill accept demo" in message.content
    assert "/skill reject demo" in message.content
    assert "/skill diff demo" in message.content


def test_dream_draft_offer_never_goes_to_room_session(tmp_path: Path) -> None:
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    published, publish = _collect_publish()
    history = FakeHistory()
    room_key = "websocket:room-chat-1"
    sessions = FakeSessions(
        {room_key: {"updated_at": "2026-10-01T00:00:00+00:00", "metadata": {"shared_room": True,
                                                                            INBOUND_META_ROOM_SCOPE: {"room_id": "room_" + "0" * 32, "chat_id": "room-chat-1", "participant_id": "participant_" + "0" * 32, "role": "guest"}}}}
    )
    skill_drafts.wire(
        history,
        workspace=tmp_path,
        publish=publish,
        sessions=sessions,
        commit_message=lambda sha: history.commits[-1]["message"],
    )

    # Dream commit: no session line in the message body.
    history.record_turn_commit(
        "dream: consolidate", changed_paths=["skills/_proposed/demo/SKILL.md"]
    )

    assert published == []


def test_dream_draft_offer_prefers_recent_non_room_session(tmp_path: Path) -> None:
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    published, publish = _collect_publish()
    history = FakeHistory()
    sessions = FakeSessions(
        {
            "websocket:room-chat-1": {"updated_at": "2026-10-05T00:00:00+00:00",
                                      "metadata": {"shared_room": True}},
            "telegram:9": {"updated_at": "2026-10-01T00:00:00+00:00", "metadata": {}},
            "telegram:10": {"updated_at": "2026-10-03T00:00:00+00:00", "metadata": {"last_channel": "telegram:10"}},
        }
    )
    skill_drafts.wire(
        history,
        workspace=tmp_path,
        publish=publish,
        sessions=sessions,
        commit_message=lambda sha: history.commits[-1]["message"],
    )

    history.record_turn_commit(
        "dream: consolidate", changed_paths=["skills/_proposed/demo/SKILL.md"]
    )

    assert len(published) == 1
    assert (published[0].channel, published[0].chat_id) == ("telegram", "10")


def test_offer_lists_check_problems(tmp_path: Path) -> None:
    _write_draft(tmp_path, "demo", {"SKILL.md": "# no frontmatter\n"})
    published, publish = _collect_publish()
    history = FakeHistory()
    skill_drafts.wire(
        history,
        workspace=tmp_path,
        publish=publish,
        sessions=FakeSessions({}),
        commit_message=lambda sha: history.commits[-1]["message"],
    )

    history.record_turn_commit(
        "turn t-1", session_key="cli:direct", channel="cli",
        changed_paths=["skills/_proposed/demo/SKILL.md"],
    )

    assert len(published) == 1
    assert "Problems:" in published[0].content


def test_accept_commit_fires_no_offer_for_deleted_draft(tmp_path: Path) -> None:
    """The accept commit's changed paths touch _proposed/ but the draft is gone:
    exactly one offer (from the original turn commit), none from the accept."""
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    published, publish = _collect_publish()
    history = FakeHistory()
    skill_drafts.wire(
        history,
        workspace=tmp_path,
        publish=publish,
        sessions=FakeSessions({"cli:direct": {"updated_at": "2026-10-01T00:00:00+00:00", "metadata": {}}}),
        commit_message=lambda sha: history.commits[-1]["message"],
    )

    skill_drafts.accept(tmp_path, "demo")

    assert published == []
    assert history.latest_subject == "skill: accept demo"

    # Even if SM-02 hands the callback the accept commit's real changed paths,
    # the draft directory is gone: no offer.
    history.record_turn_commit(
        "skill: accept demo", session_key="cli:direct", channel="cli",
        changed_paths=["skills/_proposed/demo/SKILL.md"],
    )
    assert published == []


def test_accept_commits(tmp_path: Path) -> None:
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    history = FakeHistory()
    skill_drafts.wire(history, workspace=tmp_path, publish=lambda m: None)

    assert skill_drafts.accept(tmp_path, "demo") == []
    assert history.latest_subject == "skill: accept demo"


def test_reject_commits(tmp_path: Path) -> None:
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})
    history = FakeHistory()
    skill_drafts.wire(history, workspace=tmp_path, publish=lambda m: None)

    assert skill_drafts.reject(tmp_path, "demo") is True
    assert history.latest_subject == "skill: reject demo"


def test_commits_are_skipped_without_history(tmp_path: Path) -> None:
    """Pre-SM-02 degradation: accept/reject still work, commit is skipped."""
    _write_draft(tmp_path, "demo", {"SKILL.md": _skill_md("demo")})

    assert skill_drafts.accept(tmp_path, "demo") == []
