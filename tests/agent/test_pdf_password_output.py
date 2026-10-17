import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.progress_hook import AgentProgressHook
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
