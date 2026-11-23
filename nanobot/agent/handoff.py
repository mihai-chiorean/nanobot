"""Hand-off of long private websocket chat turns to Work (MIT-1855).

Design doc ``docs/design/onboarding-and-approval-tuning.md`` §4 (product
repo): when a private websocket chat turn has run for
``agents.defaults.backgroundHandoffSeconds`` (default 45 s) and is about to
execute another tool batch, the rest of the work moves to a Work task and the
chat is freed.

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
* The transcript must stay valid. The raise fires in ``before_execute_tools``,
  so the pending batch never runs; the loop keeps history only up to the last
  completed tool result and drops the assistant message whose tool calls never
  executed.
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any, Literal

from nanobot.agent.hook import AgentHook, AgentHookContext

HandoffReason = Literal["time", "signin"]

# The fixed chat replies. ``handoff_task_id`` rides the outbound metadata when
# the task was created, so the apps can link the chat to the Work task.
HANDOFF_REPLY = (
    "This is taking a while. I'll keep going in Work and let you know when it's done."
)
HANDOFF_UNAVAILABLE_REPLY = "I couldn't move this to Work; please ask again."


class HandoffRequested(Exception):  # noqa: N818
    """Recognised stop: the runner passes it up without error bookkeeping.

    Not an ``Error`` by design (the issue names it ``HandoffRequested``): it
    reports work moving to the background, not a failure.
    """

    def __init__(self, reason: HandoffReason) -> None:
        super().__init__(reason)
        self.reason: HandoffReason = reason


class HandoffHook(AgentHook):
    """Raise ``HandoffRequested`` at a safe tool boundary once the turn is long.

    ``_reraise`` keeps ``CompositeHook`` from swallowing the exception; the
    runner treats it as a stop, not a failure.

    *turn_had_write* reads the runner's sticky write evidence (the
    ``turn_had_write`` flag on the latest runtime checkpoint, fail closed when
    it is missing). The runner checkpoints a batch -- with its pending calls
    counted -- before ``before_execute_tools`` runs, so the evidence is
    sampled after each executed batch in ``after_iteration``: the batch being
    dropped by the hand-off never ran and must not count, every batch that did
    run must. *may_hand_off* is consulted at the boundary itself (``False``
    while the runtime drains).
    """

    def __init__(
        self,
        *,
        threshold: float,
        turn_had_write: Callable[[], bool],
        may_hand_off: Callable[[], bool] = lambda: True,
        started_at: float | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self.threshold = threshold
        self.started_at = monotonic() if started_at is None else started_at
        self._turn_had_write = turn_had_write
        self._may_hand_off = may_hand_off
        self.saw_write = False
        # The live runner transcript, captured at the last tool boundary so
        # the loop can build the kept history after the raise.
        self.transcript: list[dict[str, Any]] | None = None

    async def after_iteration(self, context: AgentHookContext) -> None:
        if context.tool_calls and self._turn_had_write():
            self.saw_write = True

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self.transcript = context.messages
        if self.saw_write or self.threshold <= 0:
            return
        if monotonic() - self.started_at < self.threshold:
            return
        if not self._may_hand_off():
            return
        raise HandoffRequested("time")
