import asyncio
from pathlib import Path

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.channels.chat_inbox import ChatInboxStore


def _message(
    content: str,
    *,
    chat_id: str = "chat-1",
    client_message_id: str = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306",
) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="client-1",
        chat_id=chat_id,
        content=content,
        metadata={"client_message_id": client_message_id},
    )


@pytest.mark.asyncio
async def test_accept_is_durable_and_idempotent(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    writer = ChatInboxStore(tmp_path)

    first_disposition, first = await writer.accept(
        _message("hello"),
        client_message_id,
    )
    second_disposition, second = await ChatInboxStore(tmp_path).accept(
        _message("hello"),
        client_message_id,
    )

    assert first_disposition == "inserted"
    assert second_disposition == "existing"
    assert second.sequence == first.sequence
    assert second.message.content == "hello"


@pytest.mark.asyncio
async def test_reused_id_with_different_payload_is_rejected(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    store = ChatInboxStore(tmp_path)
    await store.accept(_message("first"), client_message_id)

    disposition, record = await store.accept(
        _message("different"),
        client_message_id,
    )

    assert disposition == "conflict"
    assert record.message.content == "first"


@pytest.mark.asyncio
async def test_retry_ignores_ephemeral_remote_address(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    store = ChatInboxStore(tmp_path)
    original = _message("hello")
    original.metadata["remote"] = ("192.0.2.1", 1000)
    retry = _message("hello")
    retry.metadata["remote"] = ("192.0.2.1", 2000)

    await store.accept(original, client_message_id)
    disposition, _ = await store.accept(retry, client_message_id)

    assert disposition == "existing"


@pytest.mark.asyncio
async def test_recovery_is_ordered_and_processed_receipts_are_retained(
    tmp_path: Path,
) -> None:
    first_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    second_id = "06eab632-f2ec-48f2-901b-0aa0a7f2a4c7"
    store = ChatInboxStore(tmp_path)
    await store.accept(_message("first", client_message_id=first_id), first_id)
    await store.accept(_message("second", client_message_id=second_id), second_id)
    await store.mark_enqueued("chat-1", first_id)

    recovered = await ChatInboxStore(tmp_path).recoverable()
    assert [record.message.content for record in recovered] == ["first", "second"]

    await store.mark_processed("chat-1", first_id)
    remaining = await ChatInboxStore(tmp_path).recoverable()
    assert [record.client_message_id for record in remaining] == [second_id]

    disposition, receipt = await store.accept(
        _message("first", client_message_id=first_id),
        first_id,
    )
    assert disposition == "existing"
    assert receipt.state == "processed"


@pytest.mark.asyncio
async def test_processed_state_cannot_regress_to_enqueued(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    store = ChatInboxStore(tmp_path)
    await store.accept(_message("hello"), client_message_id)
    await store.mark_processed("chat-1", client_message_id)

    await store.mark_enqueued("chat-1", client_message_id)

    disposition, receipt = await store.accept(
        _message("hello"),
        client_message_id,
    )
    assert disposition == "existing"
    assert receipt.state == "processed"


@pytest.mark.asyncio
async def test_only_one_concurrent_enqueue_claim_succeeds(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    store = ChatInboxStore(tmp_path)
    await store.accept(_message("hello"), client_message_id)

    claims = await asyncio.gather(
        *(
            store.claim_for_enqueue("chat-1", client_message_id)
            for _ in range(8)
        )
    )

    assert claims.count(True) == 1
    assert claims.count(False) == 7


@pytest.mark.asyncio
async def test_retry_state_and_attempt_count_survive_restart(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    store = ChatInboxStore(tmp_path)
    await store.accept(_message("hello"), client_message_id)
    assert await store.claim_for_enqueue("chat-1", client_message_id)

    retry = await store.prepare_retry("chat-1", client_message_id)

    assert retry.state == "retry_wait"
    assert retry.retry_count == 1
    recovered = await ChatInboxStore(tmp_path).recoverable()
    assert [(item.state, item.retry_count) for item in recovered] == [
        ("retry_wait", 1)
    ]
    assert await store.claim_retry_for_enqueue("chat-1", client_message_id)
    assert not await store.claim_retry_for_enqueue("chat-1", client_message_id)


@pytest.mark.asyncio
async def test_started_command_is_excluded_from_automatic_replay(
    tmp_path: Path,
) -> None:
    store = ChatInboxStore(tmp_path)
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    await store.accept(_message("/danger"), client_message_id)
    await store.mark_enqueued("chat-1", client_message_id)

    assert await store.mark_command_started("chat-1", client_message_id)
    assert not await store.mark_command_started("chat-1", client_message_id)
    assert await store.recoverable() == []
    interrupted = await store.interrupted_commands()
    assert [record.client_message_id for record in interrupted] == [
        client_message_id
    ]


@pytest.mark.asyncio
async def test_receipts_are_isolated_by_tenant_workspace(tmp_path: Path) -> None:
    client_message_id = "7fbf82b5-37de-4df0-b2bb-749bb6fd2306"
    first = ChatInboxStore(tmp_path / "tenant-a")
    second = ChatInboxStore(tmp_path / "tenant-b")

    first_disposition, _ = await first.accept(_message("first"), client_message_id)
    second_disposition, second_record = await second.accept(
        _message("second"),
        client_message_id,
    )

    assert first_disposition == "inserted"
    assert second_disposition == "inserted"
    assert second_record.message.content == "second"
