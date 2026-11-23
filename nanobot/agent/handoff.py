"""Hand-off of long private websocket chat turns to Work (MIT-1855).

Design doc ``docs/design/onboarding-and-approval-tuning.md`` §4 (product
repo): when a private websocket chat turn has run for
``agents.defaults.backgroundHandoffSeconds`` (default 45 s) and is about to
execute another tool batch, the rest of the work moves to a Work task and the
chat is freed. OA-13 adds a second trigger with no time threshold: an
iteration whose browser tool result is a sign-in the tester must fix outside
chat (:func:`signin_needs_tester`) hands off at once, from
``after_iteration``, so it fires even when the model would otherwise answer
in text.

These constraints shape the mechanism:

* A hook cannot cancel its own turn task: ``CompositeHook._for_each_hook_safe``
  swallows hook exceptions unless the hook sets ``_reraise = True``, and
  ``AgentRunner.run`` turns any other exception into ``stop_reason="error"``
  plus ``on_error``. So the hand-off is a ``HandoffRequested`` exception the
  runner recognises as a stop, raised by a ``_reraise`` hook.
* Writes must not be repeated. The gate is the runner's own write evidence
  (SR-09, ``turn_had_write`` on the runtime checkpoint): a turn that has
  started any non-read-only tool call never hands off. That covers Gmail and
  browser mutations and approval proposals alike, and fails closed for any
  tool that does not declare itself read-only.
* The Work task keys its writes by its own task id (SR-11). Its creation is
  idempotent on the chat turn's idempotency scope, so a replayed chat turn
  links the existing task instead of starting a second run.
* A draining runtime (MIT-1811) never hands off: its Work inbound would be
  dropped, so the turn keeps running in chat and finishes inside the grace.
* The transcript must stay valid. The time raise fires in
  ``before_execute_tools``, so the pending batch never runs; the loop keeps
  history only up to the last completed tool result and drops the assistant
  message whose tool calls never executed. The sign-in raise fires in
  ``after_iteration``, after that batch's results are already complete, so
  the kept history includes them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
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


# Mutations that need a tap answer with a structured
# ``{"status": "approval_required", ...}`` result. An outstanding approval in
# a sign-in iteration vetoes the sign-in hand-off: the chat-origin call could
# still run once the tester taps.
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
    failure.

    *turn_had_write* reads the runner's sticky write evidence (the
    ``turn_had_write`` flag on the latest runtime checkpoint, fail closed when
    it is missing). The runner checkpoints a batch -- with its pending calls
    counted -- before ``before_execute_tools`` runs, so the evidence is
    sampled after each executed batch in ``after_iteration``: the batch being
    dropped by a time hand-off never ran and must not count, every batch that
    did run must. A sign-in hand-off keeps the batch that produced the
    sign-in, so that batch's own calls count too: only the sign-in calls
    whose result needs the tester may be non-read-only (*is_read_only* is the
    same registry lookup SR-09 uses). *may_hand_off* is consulted before
    either raise (``False`` while the runtime drains).
    """

    def __init__(
        self,
        *,
        threshold: float,
        turn_had_write: Callable[[], bool],
        is_read_only: Callable[[str], bool] = lambda name: False,
        may_hand_off: Callable[[], bool] = lambda: True,
        started_at: float | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self.threshold = threshold
        self.started_at = monotonic() if started_at is None else started_at
        self._turn_had_write = turn_had_write
        self._is_read_only = is_read_only
        self._may_hand_off = may_hand_off
        self.saw_write = False
        # The live runner transcript, captured at the last tool boundary so
        # the loop can build the kept history after the raise.
        self.transcript: list[dict[str, Any]] | None = None

    async def after_iteration(self, context: AgentHookContext) -> None:
        if not context.tool_calls:
            return
        wrote_before = self.saw_write
        if self._turn_had_write():
            self.saw_write = True
        # OA-13: a sign-in the tester must fix outside chat ends the turn
        # here, even when the model would otherwise answer in text -- unless
        # an earlier batch wrote, or this batch ran a write other than the
        # sign-in itself.
        if wrote_before or not signin_needs_tester(context.tool_results):
            return
        for call, result in zip(context.tool_calls, context.tool_results):
            if not self._is_read_only(call.name) and not signin_needs_tester([result]):
                return
        if not self._may_hand_off():
            return
        raise HandoffRequested("signin")

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self.transcript = context.messages
        if self.saw_write or self.threshold <= 0:
            return
        if monotonic() - self.started_at < self.threshold:
            return
        if not self._may_hand_off():
            return
        raise HandoffRequested("time")
