"""Tool for pausing a turn until the user answers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema

STRUCTURED_BUTTON_CHANNELS = frozenset({"telegram", "websocket"})

# A parked ``ask_user`` is only answerable while it is the live tail of the
# conversation. MIT-1029 asks "what happens when the user never answers": an
# abandoned question must not be silently resolved by whichever unrelated
# message happens to arrive days later -- that message is a fresh request, and
# the stale question is left for the history/compaction path to expire. A
# question parked longer than this is treated as abandoned and the next plain
# message starts a new turn instead of becoming the tool result.
ASK_USER_ANSWER_MAX_AGE_S = 24 * 60 * 60


class AskUserInterrupt(BaseException):
    """Internal signal: the runner should stop and wait for user input."""

    def __init__(self, question: str, options: list[str] | None = None) -> None:
        self.question = question
        self.options = [str(option) for option in (options or []) if str(option)]
        super().__init__(question)


@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "The question to ask before continuing. Use this only when the task needs the user's answer."
        ),
        options=ArraySchema(
            StringSchema("A possible answer label"),
            description="Optional choices. The user may still reply with free text.",
        ),
        required=["question"],
    )
)
class AskUserTool(Tool):
    """Ask the user a blocking question."""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Pause and ask the user a question when their answer is required to continue. "
            "Use options for likely answers; the user's reply, typed or selected, is returned as the tool result. "
            "For non-blocking notifications or buttons, use the message tool instead."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        question: str,
        options: list[str] | None = None,
        **_: Any,
    ) -> Any:
        raise AskUserInterrupt(question=question, options=options)


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, dict):
        function_data = cast(dict[str, Any], function)
        name = function_data.get("name")
        if isinstance(name, str):
            return name
    name_value = tool_call.get("name")
    return name_value if isinstance(name_value, str) else ""


def _tool_call_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    function_value = tool_call.get("function")
    raw: Any
    if isinstance(function_value, dict):
        raw = cast(dict[str, Any], function_value).get("arguments")
    else:
        raw = tool_call.get("arguments")
    if isinstance(raw, dict):
        return cast(dict[str, Any], raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}
    return {}


def _assistant_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[object], raw) if isinstance(item, dict)]


def pending_ask_user_id(history: list[dict[str, Any]]) -> str | None:
    """Return the tool-call id of the newest unanswered ``ask_user`` call."""
    pending: dict[str, str] = {}
    for message in history:
        role = message.get("role")
        if role == "assistant":
            for tool_call in _assistant_tool_calls(message):
                tool_call_id = tool_call.get("id")
                if isinstance(tool_call_id, str):
                    pending[tool_call_id] = _tool_call_name(tool_call)
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if isinstance(tool_call_id, str):
                pending.pop(tool_call_id, None)
    for tool_call_id, name in reversed(list(pending.items())):
        if name == "ask_user":
            return tool_call_id
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Session rows are written with naive local time (datetime.now().isoformat()),
    # so a naive value is local time, not UTC. Reading it as UTC shrinks or grows
    # the answer window by the host's UTC offset (about 7h on the PDT Spark).
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def ask_user_call_is_expired(
    history: list[dict[str, Any]],
    tool_call_id: str,
    *,
    max_age_s: float = ASK_USER_ANSWER_MAX_AGE_S,
    now: datetime | None = None,
) -> bool:
    """Whether the assistant row parking ``tool_call_id`` is older than ``max_age_s``.

    Reads the persisted ``timestamp`` the session writer stamps on every row
    (``Session.add_message``). A missing or unparseable timestamp returns
    ``False`` so a data glitch never orphans a resumable question; the caller
    still gates on :func:`pending_ask_user_id`, which is what makes the call
    resumable in the first place.
    """
    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        for tool_call in _assistant_tool_calls(message):
            if tool_call.get("id") != tool_call_id or _tool_call_name(tool_call) != "ask_user":
                continue
            timestamp = _parse_timestamp(message.get("timestamp"))
            if timestamp is None:
                return False
            base = now if now is not None else datetime.now(timezone.utc)
            return (base - timestamp).total_seconds() > max_age_s
    return False


def ask_user_tool_result_messages(
    system_prompt: str,
    history: list[dict[str, Any]],
    tool_call_id: str,
    content: str,
) -> list[dict[str, Any]]:
    """Resume a parked turn: the user's next message becomes the tool result."""
    return [
        {"role": "system", "content": system_prompt},
        *history,
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": "ask_user",
            "content": content,
        },
    ]


def ask_user_options_from_messages(messages: list[dict[str, Any]]) -> list[str]:
    """Recover the option labels from the trailing ``ask_user`` tool call."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        for tool_call in reversed(_assistant_tool_calls(message)):
            if _tool_call_name(tool_call) != "ask_user":
                continue
            options = _tool_call_arguments(tool_call).get("options")
            if isinstance(options, list):
                return [
                    option
                    for option in cast(list[object], options)
                    if isinstance(option, str)
                ]
    return []


def ask_user_outbound(
    content: str | None,
    options: list[str],
    channel: str,
) -> tuple[str | None, list[list[str]]]:
    """Render the question for ``channel``: buttons where supported, text otherwise."""
    if not options:
        return content, []
    if channel in STRUCTURED_BUTTON_CHANNELS:
        return content, [options]
    option_text = "\n".join(f"{index}. {option}" for index, option in enumerate(options, 1))
    return f"{content}\n\n{option_text}" if content else option_text, []
