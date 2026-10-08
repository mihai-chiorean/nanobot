"""Hand-off of long private websocket chat turns to Work (MIT-1855, MIT-1860).

Design doc ``docs/design/onboarding-and-approval-tuning.md`` §4 (product
repo): when a private websocket chat turn has run for
``agents.defaults.backgroundHandoffSeconds`` (default 45 s) and is about to
execute another tool batch, the rest of the work moves to a Work task and the
chat is freed. OA-13 adds a second trigger with no time threshold: an
iteration whose browser tool result is a sign-in the tester must fix outside
chat (:func:`signin_needs_tester`) hands off at once, from
``after_iteration``, so it fires even when the model would otherwise answer
in text.

Three constraints shape the mechanism:

* A hook cannot cancel its own turn task: ``CompositeHook._for_each_hook_safe``
  swallows hook exceptions unless the hook sets ``_reraise = True``, and
  ``AgentRunner.run`` turns any other exception into ``stop_reason="error"``
  plus ``on_error``. So the hand-off is a ``HandoffRequested`` exception the
  runner recognises as a stop, raised by a ``_reraise`` hook.
* Writes must not be repeated. A Gmail re-proposal is keyed by the model's own
  ``request_key`` and its argument hash includes the model's reason, so a
  re-run would create a second approval. A turn whose executed tool names
  contain any write tool -- or whose tool results carry an
  ``approval_required`` status -- never hands off.
* The transcript must stay valid. The time raise fires in ``before_execute_tools``,
  so the pending batch never runs; the loop keeps history only up to the last
  completed tool result and drops the assistant message whose tool calls never
  executed. The sign-in raise fires in ``after_iteration``, after that batch's
  results are already complete, so the kept history includes them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from time import monotonic
from typing import Any, Literal, cast

from nanobot.agent.hook import AgentHook, AgentHookContext

HandoffReason = Literal["time", "signin"]

# The fixed chat replies. ``handoff_task_id`` rides the outbound metadata when
# the task was created, so the apps can link the chat to the Work task.
HANDOFF_REPLY = (
    "This is taking a while. I'll keep going in Work and let you know when it's done."
)
HANDOFF_UNAVAILABLE_REPLY = "I couldn't move this to Work; please ask again."

# Every Gmail mutation tool (exact MCP names from the connectors'
# ``mcp_gmail_write.go``) plus the browser tools that act on the user's
# signed-in session: ``browser_act`` clicks/sends/saves/posts/deletes/buys
# (and follows download links) and ``browser_fill_form`` can submit a form.
# Gmail reads (``gmail_search``, ``gmail_get_message``, ``gmail_list_labels``,
# ``gmail_connection_status``) and browser reads (``browser_open``,
# ``browser_find``, ``browser_read_page``) deliberately stay out of this set:
# re-running them in Work is harmless.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "gmail_modify_labels",
        "gmail_archive_message",
        "gmail_trash_message",
        "gmail_trash_messages",
        "gmail_create_draft",
        "gmail_send_message",
        "browser_act",
        "browser_fill_form",
    }
)

# MCP tools reach the agent as ``mcp_<server>_<tool>`` (``mcp.py``), and the
# connector server key is tenant-settable (``ziggy``, legacy ``ziggy_gmail``),
# so names match on their trailing tool segment rather than a fixed prefix.
def is_write_tool_name(name: str | None) -> bool:
    """Whether a tool name (as seen by the agent) is a write that must not re-run."""
    if not name:
        return False
    return any(name == tool or name.endswith(f"_{tool}") for tool in WRITE_TOOL_NAMES)


# Gmail/browser mutations that need a tap answer with a structured
# ``{"status": "approval_required", ...}`` result; the approval is already
# outstanding, so a Work re-run would propose it a second time.
_APPROVAL_REQUIRED_RE = re.compile(r'"status"\s*:\s*"approval_required"')


def result_requests_approval(result: Any) -> bool:
    """Whether a tool result reports an outstanding ``approval_required``."""
    text = result if isinstance(result, str) else str(result or "")
    if "approval_required" not in text:
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        return _APPROVAL_REQUIRED_RE.search(text) is not None
    return isinstance(parsed, dict) and parsed.get("status") == "approval_required"


# The sign-in states that leave nothing outstanding and need the tester
# outside chat (design doc §4 "Sign-ins"). ``needs_credential`` is D4's login
# outcome/reason wording; ``no_credential`` is the contract code D5 puts in
# the tool result for that same state (connectors' ``LoginHalt``), so
# matching only D4's word would never fire on a real result.
_SIGNIN_NEEDS_TESTER: frozenset[str] = frozenset(
    {"needs_credential", "no_credential", "session_expired"}
)

# MIT-1349's in-call code wait ends the chat-origin call when nobody relays a
# code; the sign-in then comes back as a stopped result carrying this code
# (connectors maps "code/approval not answered in time" to
# ``interrupt_required``; the issue's wording reports the same wait failure
# as ``blocked``). Either stop value paired with it means the wait ended
# without a code and the tester is needed.
_CODE_WAIT_STOP_CODES: frozenset[str] = frozenset({"interrupt_required"})
_WAIT_ENDED_STOPS: frozenset[str] = frozenset({"blocked", "needs_user"})

# Fields a browser result carries the login outcome/reason in: the envelope's
# ``outcome``, ``status`` and ``reason`` (connectors repeats D4's LoginResult
# status verbatim) and the failure envelope's ``error.code``/``error.reason``.
_LOGIN_OUTCOME_KEYS: tuple[str, ...] = ("outcome", "status", "reason")
_LOGIN_ERROR_KEYS: tuple[str, ...] = ("code", "reason")


def _login_tokens(result: Any) -> set[str]:
    """Login outcome/reason tokens of one tool result's JSON envelope.

    Parses like ``_envelope_forbids_retry`` in ``tools/execution.py``: a JSON
    dict with an optional dict ``error``. Plain-text or non-dict results
    carry no tokens and never match.
    """
    text = result if isinstance(result, str) else str(result or "")
    try:
        parsed: object = json.loads(text)
    except (ValueError, TypeError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    payload = cast("dict[str, Any]", parsed)
    tokens: set[str] = set()
    for key in _LOGIN_OUTCOME_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            tokens.add(value)
    error = payload.get("error")
    if isinstance(error, dict):
        details = cast("dict[str, Any]", error)
        for key in _LOGIN_ERROR_KEYS:
            value = details.get(key)
            if isinstance(value, str):
                tokens.add(value)
    return tokens


def signin_needs_tester(tool_results: Iterable[Any]) -> bool:
    """Whether an iteration's tool results are a sign-in the tester must fix.

    True only for results that leave nothing outstanding and need the tester
    outside chat (design doc §4): a login outcome/reason of
    ``needs_credential``/``session_expired`` (or the wire code D5 emits for
    the first), or a stopped sign-in carrying ``interrupt_required`` --
    MIT-1349's in-call code wait ended without a code. A sign-in waiting on
    a ``browser.credential_fill`` approval tap must not hand off, because the
    chat-origin fill could still run, so any ``approval_required`` result
    (Gmail/connector style, :func:`result_requests_approval`) or outstanding
    D5 ``needs_approval`` card in the iteration answers False. Rule-based
    blocks (``policy_denied``, ``login_blocked``) and successful sign-ins
    match nothing.
    """
    token_sets: list[set[str]] = []
    for result in tool_results:
        if result_requests_approval(result):
            return False
        tokens = _login_tokens(result)
        if "needs_approval" in tokens:
            return False
        token_sets.append(tokens)
    for tokens in token_sets:
        if tokens & _SIGNIN_NEEDS_TESTER:
            return True
    return any(
        tokens & _WAIT_ENDED_STOPS and tokens & _CODE_WAIT_STOP_CODES for tokens in token_sets
    )


class HandoffRequested(Exception):  # noqa: N818
    """Recognised stop: the runner passes it up without error bookkeeping.

    Not an ``Error`` by design (the issue names it ``HandoffRequested``): it
    reports work moving to the background, not a failure.
    """

    def __init__(self, reason: HandoffReason) -> None:
        super().__init__(reason)
        self.reason: HandoffReason = reason


class HandoffHook(AgentHook):
    """Raise ``HandoffRequested`` at a safe tool boundary on a trigger.

    Two triggers: elapsed time at the next tool boundary (reason ``time``)
    and a sign-in the tester must fix in the finished iteration's results
    (reason ``signin``, no threshold). ``_reraise`` keeps ``CompositeHook``
    from swallowing the exception; the runner treats it as a stop, not a
    failure. The hook also records whether the turn has done any write (or
    has an approval outstanding), which disqualifies the turn from hand-off
    permanently.
    """

    def __init__(self, *, threshold: float, started_at: float | None = None) -> None:
        super().__init__(reraise=True)
        self.threshold = threshold
        self.started_at = monotonic() if started_at is None else started_at
        self.saw_write = False
        # The live runner transcript, captured at the last tool boundary so
        # the loop can build the kept history after the raise.
        self.transcript: list[dict[str, Any]] | None = None

    async def after_iteration(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            if is_write_tool_name(call.name):
                self.saw_write = True
                return
        for result in context.tool_results:
            if result_requests_approval(result):
                self.saw_write = True
                return
        # OA-13: a sign-in the tester must fix outside chat ends the turn
        # here, even when the model would otherwise answer in text. The
        # write guard above still applies: a turn that wrote earlier or has
        # an approval outstanding never hands off.
        if not self.saw_write and signin_needs_tester(context.tool_results):
            raise HandoffRequested("signin")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self.transcript = context.messages
        if self.saw_write or self.threshold <= 0:
            return
        if monotonic() - self.started_at >= self.threshold:
            raise HandoffRequested("time")
