"""``GET /api/activity/audit`` -- owner-facing tester activity feed (MIT-1450).

Every workspace already records each tool and LLM call in ``<workspace>/audit.jsonl``
(the ``AuditLogger`` the agent loop attaches at its workspace root, MIT-1401).
Until now only the host owner could read those files; Go.AI shows testers every
action -- including refusals -- in a readable feed, and this is the server half
of giving the workspace owner the same view of what Ziggy did on their behalf.

The route serves the workspace's own log, newest-first and paginated, with raw
arguments and results reduced to redacted summaries. Auth is the owner API
token only: the trusted-proxy shortcut must not admit bare proxied requests
(repo rule for owner routes) and room credentials are a distinct audience and
are refused -- a room guest must never read the owner's audit trail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.channels.websocket.rooms import SharedRoomStore
from nanobot.session.manager import SessionManager

_ROOM_CHAT = "11111111-2222-3333-4444-555555555555"
_ROOM_ID = "room_" + "a" * 32
_PARTICIPANT_ID = "participant_" + "b" * 32
# A secret-shaped value (OpenAI-style key) planted in a tool argument to prove
# the read side never echoes raw arguments -- mirrors the redaction fixtures.
# It is caught by the shared ``redact_if_sensitive`` screen (label-form match
# on ``api_key=...``); a bare unlabelled blob would NOT be caught, so the
# fixture keeps the realistic ``api_key=<secret>`` shape rather than a raw one.
_PLANTED_SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0" * 2
_CORRUPT_MARKER = "CORRUPT-MARKER-should-never-appear"
_RESULT_MARKER = "RESULT-MARKER-should-never-appear"


def _audit_line(**fields: Any) -> str:
    return json.dumps({"pid": 4242, "session_id": "websocket:tester", "channel": "websocket", **fields})


def _write_log(workspace: Path, lines: list[str]) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workspace


def _seed_audit_log(workspace: Path) -> Path:
    """Write a chronological fixture audit log (as the agent loop appends it).

    Returns the workspace path; the log lives at ``<workspace>/audit.jsonl``.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    lines = [
        # 0: plain tool success -- arguments must never be echoed; the stored
        # result payload must never be echoed either (the feed projects only
        # the five whitelisted keys, so this rides nowhere).
        _audit_line(
            timestamp="2026-09-25T10:00:00+00:00",
            event_type="tool_call",
            tool_name="exec",
            arguments={"command": "ls -la /tmp"},
            result_status="ok",
            output=_RESULT_MARKER + " raw tool result payload",
        ),
        # 1: LLM usage row (no tool_name, no result_status).
        _audit_line(
            timestamp="2026-09-25T10:00:01+00:00",
            event_type="llm_call",
            model="gpt-test-1",
            tokens_in=120,
            tokens_out=45,
            latency_ms=880.5,
        ),
        # 2: failing tool call -> outcome error, with error text.
        _audit_line(
            timestamp="2026-09-25T10:00:02+00:00",
            event_type="tool_call",
            tool_name="web_fetch",
            arguments={"url": "https://example.com/a"},
            result_status="error",
            error="Error: HTTP 500 from upstream",
        ),
        # 3: guard-blocked tool call -> the real ToolRegistry writer records
        # result_status "error" plus error_type "prescreen" for every
        # prepare_call rejection; the feed surfaces that writer shape as
        # outcome "refused".
        _audit_line(
            timestamp="2026-09-25T10:00:03+00:00",
            event_type="tool_call",
            tool_name="exec",
            arguments={"command": "printenv | grep TOKEN"},
            result_status="error",
            error_type="prescreen",
            error="Error: refused by safety guard",
        ),
        # 4: success whose arguments carry an api_key= secret -- the canonical
        # never-echo case. Args are normally redacted at WRITE time by the
        # tool's own screen; this row proves the read side also refuses to
        # reproduce raw arguments when the log already contains them.
        _audit_line(
            timestamp="2026-09-25T10:00:04+00:00",
            event_type="tool_call",
            tool_name="web_fetch",
            arguments={
                "url": "https://api.example.com/v1",
                "headers": "api_key=" + _PLANTED_SECRET,
            },
            result_status="ok",
        ),
        # 5+6: corrupt physical lines that must be skipped, not fatal.
        f"{_CORRUPT_MARKER} {{{not json}",
        '{"timestamp": "2026-09-25T10:00:05+00:00", "tool_name": "truncated"',
        # A JSON array line is valid JSON but not an entry object -> skipped.
        json.dumps(["not", "an", "entry"]),
        # 7: approval-gated tool call whose approval window lapsed -> the
        # issue's "approval_expired" outcome.
        _audit_line(
            timestamp="2026-09-25T10:00:06+00:00",
            event_type="tool_call",
            tool_name="exec",
            arguments={"command": "rm -rf /scratch/build"},
            result_status="approval_expired",
            error="Error: approval expired",
        ),
    ]
    (workspace / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workspace


def _channel(sessions: SessionManager, port: int, workspace: Path) -> Any:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _ch

    return _ch(MagicMock(), session_manager=sessions, port=port, workspace_path=workspace)


async def _get(channel: Any, port: int, path: str, *, token: str | None = None) -> Any:
    from nanobot.channels.websocket.tests.ws_test_client import http_get

    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    return await http_get(f"http://127.0.0.1:{port}{path}", headers=headers)


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[reportUnknownParameter]
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


def _free_port_and_channel(tmp_path: Path) -> tuple[Any, int, SessionManager]:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    workspace = _seed_audit_log(tmp_path / "ws")
    sessions = SessionManager(workspace)
    port = _free_port()
    return _channel(sessions, port, workspace), port, sessions


@pytest.mark.asyncio
async def test_owner_token_gets_newest_first_with_pagination(tmp_path: Path) -> None:
    channel, port, _sessions = _free_port_and_channel(tmp_path)
    try:
        token = channel.gateway.tokens.issue_api_token(300)

        # Page 1: newest two entries (the approval-expired exec, then the
        # secret-bearing web_fetch -- whose args must be redacted in the summary).
        first = await _get(channel, port, "/api/activity/audit?limit=2", token=token)
        assert first.status_code == 200, first.content
        page1 = first.json()
        assert [row["ts"] for row in page1["entries"]] == [
            "2026-09-25T10:00:06+00:00",
            "2026-09-25T10:00:04+00:00",
        ]
        assert page1["has_more"] is True
        assert page1["next_cursor"] == "4"
        assert page1["entries"][0]["kind"] == "tool"
        assert page1["entries"][0]["tool"] == "exec"
        assert page1["entries"][0]["outcome"] == "approval_expired"

        # Page 2 via the returned cursor: entries 3 and 2.
        second = await _get(
            channel, port, f"/api/activity/audit?limit=2&before={page1['next_cursor']}", token=token
        )
        assert second.status_code == 200, second.content
        page2 = second.json()
        assert [row["ts"] for row in page2["entries"]] == [
            "2026-09-25T10:00:03+00:00",
            "2026-09-25T10:00:02+00:00",
        ]
        assert page2["entries"][0]["outcome"] == "refused"  # writer shape: error/prescreen
        assert page2["entries"][1]["outcome"] == "error"

        # Page 3: the LLM row and the first tool row, then the feed is exhausted.
        third = await _get(
            channel, port, f"/api/activity/audit?limit=2&before={page2['next_cursor']}", token=token
        )
        assert third.status_code == 200, third.content
        page3 = third.json()
        assert [row["ts"] for row in page3["entries"]] == [
            "2026-09-25T10:00:01+00:00",
            "2026-09-25T10:00:00+00:00",
        ]
        assert page3["has_more"] is False
        assert page3["next_cursor"] is None

        # Concatenating pages = the full parsed feed in chronological order.
        forwards = [row for page in (page3, page2, page1) for row in reversed(page["entries"])]
        assert [row["ts"] for row in forwards] == [
            "2026-09-25T10:00:00+00:00",
            "2026-09-25T10:00:01+00:00",
            "2026-09-25T10:00:02+00:00",
            "2026-09-25T10:00:03+00:00",
            "2026-09-25T10:00:04+00:00",
            "2026-09-25T10:00:06+00:00",
        ]
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_llm_entry_projects_kind_and_summary(tmp_path: Path) -> None:
    channel, port, _sessions = _free_port_and_channel(tmp_path)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        # Default path: the first page of a feed client is a bare GET -- the
        # route's own default limit must cover the small log without params.
        response = await _get(channel, port, "/api/activity/audit", token=token)
        assert response.status_code == 200, response.content
        payload = response.json()
        assert payload["has_more"] is False and payload["next_cursor"] is None
        entries = payload["entries"]
        assert len(entries) == 6, "the three corrupt/foreign lines must be skipped"
        llm = next(row for row in entries if row["kind"] == "llm")
        assert llm["outcome"] == "ok"
        assert llm["tool"] is None
        assert llm["ts"] == "2026-09-25T10:00:01+00:00"
        assert "gpt-test-1" in llm["summary"]
        assert "120 in / 45 out" in llm["summary"]
        tools = [row for row in entries if row["kind"] == "tool"]
        assert {row["tool"] for row in tools} == {"exec", "web_fetch"}
        assert {row["outcome"] for row in tools} == {"ok", "error", "refused", "approval_expired"}
        for row in entries:
            assert set(row) == {"ts", "kind", "tool", "outcome", "summary"}
            assert isinstance(row["summary"], str) and row["summary"]
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_writer_prescreen_rejection_is_refused(tmp_path: Path) -> None:
    """The real ``ToolRegistry`` writer records refusals as ``error/prescreen``.

    The owner's live log has those rows and no ``blocked`` rows, so the feed
    must classify that exact writer shape as a refusal rather than a failure.
    """
    from nanobot.agent.tools.audit import AuditLogger
    from nanobot.agent.tools.base import Tool, ToolResult
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    class _PrescreenTool(Tool):
        @property
        def name(self) -> str:
            return "exec"

        @property
        def description(self) -> str:
            return "test tool rejected by the safety guard"

        @property
        def parameters(self) -> dict:
            return tool_parameters_schema(command=StringSchema("command"), required=["command"])

        async def execute(self, **kwargs: Any) -> ToolResult:
            return ToolResult.error("Error: Command blocked by safety guard (dangerous pattern detected)")

    workspace = tmp_path / "prescreen-ws"
    workspace.mkdir()
    registry = ToolRegistry()
    registry.register(_PrescreenTool())
    registry.set_audit_logger(AuditLogger(workspace / "audit.jsonl"))
    await registry.execute(
        "exec",
        {"command": "rm -rf /scratch"},
        session_id="websocket:tester",
        channel="websocket",
    )

    written = json.loads((workspace / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert written["result_status"] == "error"
    assert written["error_type"] == "prescreen"

    sessions = SessionManager(workspace)
    port = _free_port()
    channel = _channel(sessions, port, workspace)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/activity/audit", token=token)
        assert response.status_code == 200, response.content
        entries = response.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["tool"] == "exec"
        assert entries[0]["outcome"] == "refused"
        assert "dangerous pattern detected" in entries[0]["summary"]
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_limit_capped_at_200_and_bad_params_rejected(tmp_path: Path) -> None:
    channel, port, _sessions = _free_port_and_channel(tmp_path)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        # 250 well-formed entries: a request over the cap returns exactly 200.
        log_path = tmp_path / "ws" / "audit.jsonl"
        with open(log_path, "a", encoding="utf-8") as handle:
            for index in range(250):
                handle.write(
                    _audit_line(
                        timestamp=f"2026-09-26T00:00:{index:02d}+00:00",
                        event_type="tool_call",
                        tool_name="exec",
                        arguments={"command": f"echo {index}"},
                        result_status="ok",
                    )
                    + "\n"
                )
        capped = await _get(channel, port, "/api/activity/audit?limit=1000", token=token)
        assert capped.status_code == 200, capped.content
        assert len(capped.json()["entries"]) == 200
        assert capped.json()["has_more"] is True
        assert capped.json()["next_cursor"] == "56"

        for bad in ("limit=0", "limit=-1", "limit=abc", "before=abc", "before=-3"):
            response = await _get(channel, port, f"/api/activity/audit?{bad}", token=token)
            assert response.status_code == 400, bad
        # A cursor of 0 is the start of the feed: an empty page, not an error.
        empty = await _get(channel, port, "/api/activity/audit?before=0", token=token)
        assert empty.status_code == 200, empty.content
        assert empty.json() == {"entries": [], "next_cursor": None, "has_more": False}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_missing_log_is_empty_not_fatal(tmp_path: Path) -> None:
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    workspace = tmp_path / "empty-ws"
    workspace.mkdir()
    sessions = SessionManager(workspace)
    port = _free_port()
    channel = _channel(sessions, port, workspace)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/activity/audit", token=token)
        assert response.status_code == 200, response.content
        assert response.json() == {"entries": [], "next_cursor": None, "has_more": False}
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_no_token_unauthorized_room_credential_refused(tmp_path: Path) -> None:
    channel, port, sessions = _free_port_and_channel(tmp_path)
    try:
        anonymous = await _get(channel, port, "/api/activity/audit")
        assert anonymous.status_code == 401, anonymous.content
        wrong = await _get(channel, port, "/api/activity/audit", token="not-a-real-token")
        assert wrong.status_code == 401, wrong.content

        # Mint a REAL room credential and authenticate it against the route:
        # a live guest credential still must not read the owner's audit trail.
        store = SharedRoomStore(sessions, token_ttl_s=300)
        room_token, credential = store.mint(
            room_id=_ROOM_ID,
            chat_id=_ROOM_CHAT,
            participant_id=_PARTICIPANT_ID,
            display_name="Guest",
            role="contributor",
        )
        assert room_token.startswith("nbrt_")
        assert store.api_credential(room_token) is credential  # live, not revoked
        refused = await _get(channel, port, "/api/activity/audit", token=room_token)
        assert refused.status_code == 401, refused.content
        # Sanity: the very same credential is recognised by the room store, so
        # the 401 is the audience gate, not an invalid-token accident.
        assert sessions is not None
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_trusted_proxy_shortcut_cannot_be_abused_to_read_the_log(tmp_path: Path) -> None:
    """A proxied request with no owner token must not be admitted by the trusted-proxy
    shortcut; the route must demand the owner API token itself (repo rule for owner
    routes: ``tokens.check_api_token``, never the ``check_api_token`` shortcut)."""
    from nanobot.channels.websocket.transport import TransportRequest

    channel, _port, _sessions = _free_port_and_channel(tmp_path)
    try:
        handler = channel.gateway.http

        proxied = TransportRequest(
            method="GET",
            path="/api/activity/audit",
            headers=_Headers(),
            body=b"",
            raw_path="/api/activity/audit",
        )
        setattr(proxied, "_nanobot_trusted_proxy_authenticated", True)
        assert (await handler._handle_activity_audit(proxied)).status_code == 401

        # Non-vacuity: the very same request shape with a valid owner token
        # present does reach the log (this is what the 401 above is denying).
        owner = TransportRequest(
            method="GET",
            path="/api/activity/audit?limit=1",
            headers=_Headers({"Authorization": f"Bearer {channel.gateway.tokens.issue_api_token(60)}"}),
            body=b"",
            raw_path="/api/activity/audit?limit=1",
        )
        response = await handler._handle_activity_audit(owner)
        assert response.status_code == 200
        payload = json.loads(bytes(response.body).decode())
        assert payload["entries"][0]["ts"] == "2026-09-25T10:00:06+00:00"
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_post_not_allowed(tmp_path: Path) -> None:
    from nanobot.channels.websocket.transport import TransportRequest

    channel, _port, _sessions = _free_port_and_channel(tmp_path)
    try:
        handler = channel.gateway.http
        token = channel.gateway.tokens.issue_api_token(300)
        request = TransportRequest(
            method="POST",
            path="/api/activity/audit",
            headers=_Headers({"Authorization": f"Bearer {token}"}),
            body=b"{}",
            raw_path="/api/activity/audit",
        )
        response = await handler._dispatch_misc_routes(MagicMock(), request, "/api/activity/audit")
        assert response is not None
        assert response.status_code == 405
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_never_echoes_secrets_or_raw_arguments(tmp_path: Path) -> None:
    channel, port, _sessions = _free_port_and_channel(tmp_path)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        # The request itself carries a secret-shaped value in an unrelated
        # query parameter; must not be reflected either.
        response = await _get(
            channel,
            port,
            f"/api/activity/audit?limit=100&note={_PLANTED_SECRET}",
            token=token,
        )
        assert response.status_code == 200, response.content
        body = response.content.decode("utf-8", "replace")
        assert _PLANTED_SECRET not in body
        assert _PLANTED_SECRET[:11] + "A1b2" not in body
        # The planted secret is in the log -- prove the fixture would have
        # exposed it end to end had redaction failed (the value itself, and
        # even its first 12 chars, must not appear).
        assert "A1b2C3d4E5f6" not in body
        raw_log = (tmp_path / "ws" / "audit.jsonl").read_text(encoding="utf-8")
        assert _PLANTED_SECRET in raw_log  # sanity: the secret really is in the log
        assert _RESULT_MARKER in raw_log   # sanity: the raw result really is in the log
        assert _CORRUPT_MARKER not in body
        assert _RESULT_MARKER not in body
        # No raw argument mappings or result payloads leak: the response
        # carries the five projection keys only -- the args dict never rides
        # along, and the summary is the redacted, truncated rendering the
        # issue prescribes (never a verbatim dump of the recorded arguments).
        for row in response.json()["entries"]:
            assert "arguments" not in row
            assert "result" not in row
            assert "stderr_tail" not in row
            # The summary is a bounded human-readable line, not an argument dump:
            # a per-entry budget of 400 characters keeps even the redacted
            # rows short of the raw 2048-char error fields the writer stores.
            assert len(row["summary"]) <= 400
            assert row["summary"].count("\n") == 0
            # The secret-bearing row specifically: redaction must fire on the
            # planted value, not merely truncate it.
            if "web_fetch" in (row["tool"] or "") and row["outcome"] == "ok":
                assert "REDACTED" in row["summary"]
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_malformed_rows_degrade_without_leaking_raw_text(tmp_path: Path) -> None:
    """Negative controls (rule 7): rows the projection was NOT designed
    around -- unknown status strings, non-dict arguments, missing fields,
    top-level JSON scalars, non-string ts and adversarial tool names --
    must not crash the route, leak raw text, or surface as outcome 'ok'
    (an unrecognised status never becomes a success; only explicit
    ok/success do)."""
    from nanobot.channels.websocket.tests.test_websocket_http_routes import _free_port

    workspace = tmp_path / "negws"
    _write_log(
        workspace,
        [
            json.dumps(
                {
                    "timestamp": "2026-09-26T09:00:00+00:00",
                    "tool_name": "weird_tool",
                    "arguments": {"url": "http://x"},
                    "result_status": "wholly-unknown",
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-09-26T09:00:01+00:00",
                    "tool_name": "exec",
                    "arguments": "not-a-dict",
                    "result_status": "ok",
                }
            ),
            json.dumps({"tool_name": "exec", "result_status": "ok"}),  # missing ts
            json.dumps({"timestamp": 1735600000, "tool_name": "exec", "result_status": "ok"}),  # non-str ts
            json.dumps({"event_type": "llm_call", "timestamp": "2026-09-26T09:00:02+00:00"}),  # no model
            json.dumps(
                {
                    "timestamp": "2026-09-26T09:00:03+00:00",
                    "event_type": "llm_call",
                    "model": "m",
                    "tokens_in": "many",  # non-int
                    "tokens_out": None,
                }
            ),
            json.dumps(42),  # bare-number line
            json.dumps(None),  # null literal line
            '"just a string"',  # bare-string line
            "[]",  # empty array line
            json.dumps({"tool_name": "x" * 300, "result_status": "ok"}),  # huge tool name
            json.dumps({"tool_name": "ex\tec\x1b[31mescape", "result_status": "ok"}),  # control chars
        ],
    )
    sessions = SessionManager(workspace)
    port = _free_port()
    channel = _channel(sessions, port, workspace)
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        response = await _get(channel, port, "/api/activity/audit", token=token)
        assert response.status_code == 200, response.content
        payload = response.json()
        assert payload["has_more"] is False
        entries = payload["entries"]
        assert len(entries) == 8  # five entry objects + huge + control + int-ts
        body = response.content.decode("utf-8", "replace")
        assert "not-a-dict" not in body  # non-dict arguments dropped wholesale
        assert "1735600000" not in body  # non-string ts never leaks
        assert "many" not in body  # invalid token counts are dropped, not rendered
        assert all(row["kind"] in {"tool", "llm"} for row in entries)
        # Unknown status strings render as error, never as a fabricated success.
        weird = next(row for row in entries if row["tool"] == "weird_tool")
        assert weird["outcome"] == "error" and weird["kind"] == "tool"
        # Non-dict arguments render as bare "Tool exec" -- never a dict dump.
        row = next(r for r in entries if r["ts"] == "2026-09-26T09:00:01+00:00")
        assert row["summary"] == "Tool exec" and "{" not in row["summary"]
        # Missing / non-string ts renders as empty string, never a crash or repr.
        assert any(row["ts"] == "" for row in entries)
        # model-null llm row keeps a fallback label; token garbage stays dropped.
        lls = [row for row in entries if row["kind"] == "llm"]
        assert any(r["summary"] == "LLM call: model call" for r in lls)
        assert any(r["summary"] == "LLM call: m" for r in lls)
        assert all("None" not in r["summary"] for r in lls)
        # Adversarial tool names: huge names are capped, control characters
        # are stripped -- neither raw value can round-trip into the response.
        huge = next(row for row in entries if (row["tool"] or "").startswith("xxx"))
        assert len(huge["tool"]) <= 80 and len(huge["summary"]) <= 400
        assert "x" * 300 not in body
        ctrl = next(row for row in entries if "escape" in (row["tool"] or ""))
        assert "\x1b" not in ctrl["tool"] and "\t" not in ctrl["tool"] and "\n" not in ctrl["tool"]
        assert all(ch == " " or ch.isprintable() for row in entries for ch in (row["tool"] or "") + row["summary"] + row["ts"])
    finally:
        await channel.stop()
