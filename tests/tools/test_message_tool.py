from pathlib import Path

import pytest

from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "not a list",
        [["ok"], "row-not-a-list"],
        [["ok", 42]],
        [[None]],
    ],
)
async def test_message_tool_rejects_malformed_buttons(bad) -> None:
    """``buttons`` must be ``list[list[str]]``; the tool validates the shape
    up front so a malformed LLM payload errors visibly instead of slipping
    into the channel layer where Telegram would silently reject the frame."""
    tool = MessageTool()
    result = await tool.execute(
        content="hi", channel="telegram", chat_id="1", buttons=bad,
    )
    assert result == "Error: buttons must be a list of list of strings"


@pytest.mark.asyncio
async def test_message_tool_marks_channel_delivery_only_when_enabled() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    await tool.execute(content="normal", channel="telegram", chat_id="1")
    token = tool.set_record_channel_delivery(True)
    try:
        await tool.execute(content="cron", channel="telegram", chat_id="1")
    finally:
        tool.reset_record_channel_delivery(token)

    assert sent[0].metadata == {}
    assert sent[1].metadata == {"_record_channel_delivery": True}


@pytest.mark.asyncio
async def test_message_tool_inherits_metadata_for_same_target() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    slack_meta = {"slack": {"thread_ts": "111.222", "channel_type": "channel"}}
    tool.set_context("slack", "C123", metadata=slack_meta)

    await tool.execute(content="thread reply")

    assert sent[0].metadata == slack_meta


@pytest.mark.asyncio
async def test_message_tool_does_not_inherit_metadata_for_cross_target() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)
    tool.set_context(
        "slack",
        "C123",
        metadata={"slack": {"thread_ts": "111.222", "channel_type": "channel"}},
    )

    await tool.execute(content="channel reply", channel="slack", chat_id="C999")

    assert sent[0].metadata == {}


@pytest.mark.asyncio
async def test_message_tool_resolves_relative_media_paths(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    workspace = tmp_path / "workspace"
    attachment = workspace / "output" / "image.png"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"image")
    tool = MessageTool(send_callback=_send, workspace=workspace)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=["output/image.png"],
    )

    expected = str(attachment.resolve())
    assert sent[0].media == [expected]


@pytest.mark.asyncio
async def test_message_tool_resolves_relative_media_paths_from_active_workspace(tmp_path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    workspace = tmp_path / "workspace"
    attachment = workspace / "output" / "image.png"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"image")
    tool = MessageTool(send_callback=_send, workspace=workspace)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=["output/image.png"],
    )

    assert sent[0].media == [str(attachment.resolve())]


@pytest.mark.asyncio
async def test_message_tool_rejects_absolute_media_paths_outside_workspace(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send, workspace=tmp_path / "workspace")

    abs_path = tmp_path / "outside.png"
    abs_path.write_bytes(b"not allowed")

    result = await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[str(abs_path)],
    )

    assert result.startswith("Error: attachment must be")
    assert sent == []


@pytest.mark.asyncio
async def test_message_tool_passes_through_url_media_paths() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send)

    url = "https://example.com/image.png"

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[url],
    )

    assert sent[0].media == [url]


@pytest.mark.asyncio
async def test_message_tool_resolves_mixed_allowed_media_paths(tmp_path: Path, monkeypatch) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    workspace = tmp_path / "workspace"
    workspace_attachment = workspace / "output" / "relative.png"
    workspace_attachment.parent.mkdir(parents=True)
    workspace_attachment.write_bytes(b"workspace")
    media_root = tmp_path / "media"
    media_root.mkdir()
    media_attachment = media_root / "media.png"
    media_attachment.write_bytes(b"media")
    monkeypatch.setattr("nanobot.agent.tools.message.get_media_dir", lambda: media_root)
    tool = MessageTool(send_callback=_send, workspace=workspace)

    await tool.execute(
        content="see attached",
        channel="telegram",
        chat_id="1",
        media=[
            "output/relative.png",
            str(media_attachment),
            "https://example.com/url.png",
            "http://example.com/http.png",
        ],
    )

    expected_relative = str(workspace_attachment.resolve())
    assert sent[0].media == [
        expected_relative,
        str(media_attachment.resolve()),
        "https://example.com/url.png",
        "http://example.com/http.png",
    ]


@pytest.mark.asyncio
async def test_message_tool_rejects_proc_environ(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send, workspace=tmp_path / "workspace")
    result = await tool.execute(
        content="do not attach host secrets",
        channel="telegram",
        chat_id="1",
        media=["/proc/self/environ"],
    )

    assert result.startswith("Error: attachment must be")
    assert sent == []


@pytest.mark.asyncio
async def test_message_tool_rejects_traversal_and_symlink_media(tmp_path: Path) -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    link = workspace / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are unavailable")

    tool = MessageTool(send_callback=_send, workspace=workspace)
    for path in ("../secret.txt", str(link)):
        result = await tool.execute(
            content="blocked",
            channel="telegram",
            chat_id="1",
            media=[path],
        )
        assert result.startswith("Error: attachment must be")
    assert sent == []
