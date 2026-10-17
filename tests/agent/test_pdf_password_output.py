import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import TranscriptInput
from nanobot.agent.hook import AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.progress_hook import AgentProgressHook, PDFPasswordStore
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.unlock_attachment import UnlockAttachmentTool
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.progress_events import output_events


@pytest.mark.asyncio
async def test_known_pdf_password_is_redacted_from_final_and_split_stream_output():
    chunks = []

    async def stream(content):
        chunks.append(content)

    hook = AgentProgressHook(output_events(on_stream=stream), streaming=True)
    context = AgentHookContext(iteration=1, messages=[], tool_calls=[
        ToolCallRequest(id="unlock", name="unlock_attachment",
                        arguments={"attachment_id": "fixture", "password": "test-secret"})
    ], response=LLMResponse(content="Using test-secret", tool_calls=[]))
    await hook.before_execute_tools(context)
    assert context.response.content == "Using [REDACTED]"
    assert hook.finalize_content(context, "Opened with test-secret") == "Opened with [REDACTED]"
    for chunk in ["Opened with test-", "sec", "ret", "."]:
        await hook.on_stream(context, chunk)
    await hook.on_stream_end(context, resuming=False)
    assert "".join(chunks) == "Opened with [REDACTED]."
    assert all("test-secret" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_password_at_end_of_stream_is_redacted_before_flush():
    chunks = []

    async def stream(content):
        chunks.append(content)

    hook = AgentProgressHook(output_events(on_stream=stream), streaming=True)
    context = AgentHookContext(iteration=1, messages=[], tool_calls=[
        ToolCallRequest(id="unlock", name="unlock_attachment",
                        arguments={"password": "test-secret"})
    ])
    await hook.before_execute_tools(context)
    await hook.on_stream(context, "Password: test-secret")
    await hook.on_stream_end(context, resuming=False)
    assert "".join(chunks) == "Password: [REDACTED]"


@pytest.mark.parametrize(("password", "text", "expected"), [
    ("a", "a cat has a hat", "a cat has a hat"),
    ("2024", "In 2024 and 2025", "In 2024 and 2025"),
    ("secret", "secretive mysecret secret2 2secret ésecret secreté",
     "secretive mysecret secret2 2secret ésecret secreté"),
    ("secret", "Use (secret), then _secret_.", "Use ([REDACTED]), then _[REDACTED]_."),
    ("a+b[0]", "Use a+b[0].", "Use [REDACTED]."),
    ("secret!", "secret!suffix secret!", "secret!suffix [REDACTED]"),
])
async def test_password_boundaries_in_final_and_streamed_text(password, text, expected):
    chunks = []

    async def stream(content):
        chunks.append(content)

    hook = AgentProgressHook(output_events(on_stream=stream), streaming=True)
    context = AgentHookContext(iteration=1, messages=[], tool_calls=[
        ToolCallRequest(id="unlock", name="unlock_attachment", arguments={"password": password})
    ], response=LLMResponse(content=text))
    await hook.before_execute_tools(context)
    assert context.response.content == expected
    assert hook.finalize_content(context, text) == expected
    # Every split, including a full password followed later by an alphanumeric.
    for split in range(len(text) + 1):
        chunks.clear()
        await hook.on_stream(context, text[:split])
        await hook.on_stream(context, text[split:])
        await hook.on_stream_end(context, resuming=False)
        assert "".join(chunks) == expected


def test_password_store_tenant_session_isolation_ttl_and_size(tmp_path, monkeypatch):
    now = 0
    monkeypatch.setattr("nanobot.agent.progress_hook.time.monotonic", lambda: now)
    store = PDFPasswordStore(max_sessions=2)
    tenant = tmp_path / "tenant-a"
    store.for_session(tenant, "chat").add("test-secret")
    assert store.for_session(tenant, "chat") == {"test-secret"}
    assert store.for_session(tmp_path / "tenant-b", "chat") == set()
    assert store.for_session(tenant, "other-chat") == set()
    assert store.for_session(tenant, "chat") == set()  # Oldest session evicted.
    store.for_session(tenant, "chat").add("test-secret")
    now = 3601
    assert store.for_session(tenant, "chat") == set()
    store.for_session(tenant, None).add("unscoped-secret")
    assert store.for_session(tenant, None) == set()


async def test_passwords_per_session_are_bounded():
    passwords = set()
    hook = AgentProgressHook(pdf_passwords=passwords)
    context = AgentHookContext(iteration=1, messages=[], tool_calls=[
        ToolCallRequest(id=str(i), name="unlock_attachment", arguments={"password": f"secret-{i}"})
        for i in range(33)
    ])
    await hook.before_execute_tools(context)
    assert len(passwords) == 32
    assert "secret-32" in passwords


def _loop(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


async def test_password_survives_turns_without_cross_session_leakage(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch)
    vault = MagicMock()
    vault.unlock = AsyncMock(return_value="Unlocked text")
    loop.tools.register(UnlockAttachmentTool(vault))
    call = ToolCallRequest(id="unlock", name="unlock_attachment",
                           arguments={"attachment_id": "fixture", "password": "test-secret"})
    responses = iter([
        LLMResponse(content="", tool_calls=[call]),
        LLMResponse(content="Opened with test-secret"),
        LLMResponse(content="Remember test-secret"),
        LLMResponse(content="Remember test-secret"),
    ])
    loop.provider.chat_stream_with_retry = AsyncMock(side_effect=lambda **kw: next(responses))

    async def turn(key):
        return await loop._run_agent_loop(
            TranscriptInput(history=[], current_message="Read the attachment"),
            runtime=loop.llm_runtime(),
            session=loop.sessions.get_or_create(key),
            request_context=RequestContext(channel="websocket", chat_id=key, session_key=key),
        )

    first = await turn("websocket:chat")
    assert first.final_content == "Opened with [REDACTED]"
    vault.unlock.assert_awaited_once_with(
        "websocket:chat", "fixture", "test-secret", session="websocket:chat",
    )
    assert call.arguments["password"] == "test-secret"
    assert "test-secret" not in json.dumps(first.messages)
    assert (await turn("websocket:chat")).final_content == "Remember [REDACTED]"
    assert (await turn("websocket:other")).final_content == "Remember test-secret"


@pytest.mark.parametrize("encoded", [False, True])
async def test_persisted_unlock_history_and_archiver_input_have_no_password(
    tmp_path, monkeypatch, encoded,
):
    loop = _loop(tmp_path, monkeypatch)
    session = loop.sessions.get_or_create("websocket:chat")
    arguments = {"attachment_id": "fixture", "password": "test-secret"}
    # Raw/resumed transcript bypassing ToolCallRequest serialization.
    raw = [{"role": "user", "content": "Read the attachment"}, {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "unlock", "type": "function", "function": {
                "name": "unlock_attachment",
                "arguments": json.dumps(arguments) if encoded else arguments,
            },
        }],
    }, {"role": "tool", "tool_call_id": "unlock", "name": "unlock_attachment",
        "content": "Unlocked text"}]
    loop._save_turn(session, raw, 0)
    loop.sessions.save(session)
    assert "test-secret" not in json.dumps(session.messages)
    assert "test-secret" not in loop.sessions._get_session_path(session.key).read_text()
    assert "test-secret" not in json.dumps(session.get_history())
    saved_arguments = json.loads(session.messages[1]["tool_calls"][0]["function"]["arguments"])
    assert saved_arguments == {"attachment_id": "fixture", "password": "[REDACTED]"}
    assert "test-secret" in json.dumps(raw)  # No mutation of the execution transcript.

    loop.provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(content="Summary"))
    await loop.consolidator.archiver.archive(
        session.messages,
        runtime=loop.llm_runtime(),
        session_key=session.key,
        history=session.get_history(),
        request_tools=[],
    )
    archive_input = loop.provider.chat_stream_with_retry.call_args.kwargs["messages"]
    assert "test-secret" not in json.dumps(archive_input)
    assert "[REDACTED]" in json.dumps(archive_input)
