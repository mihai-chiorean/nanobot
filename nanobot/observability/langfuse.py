"""Langfuse integration (v4 SDK, OTel-based) for the Ziggy agent loop.

Design goals
------------
* **Fail-safe.** Missing env vars, missing package, or SDK errors must never
  crash or slow the agent loop.  Every helper returns a no-op context
  manager when Langfuse is not available.
* **Shallow dependency surface.** The rest of the codebase imports
  ``observe_turn`` / ``observe_llm_iteration`` / ``observe_tool`` /
  ``observe_subagent`` / ``capture_trace_context`` and never touches the
  SDK directly.  If we swap to manual generation emission later (per the
  MIT-202 spike memo, task 2 — "keep ``langfuse.openai`` auto-trace for
  week 1"), only this module changes.
* **Single trace per turn.** Subagents share the parent turn's
  ``trace_id`` via ``capture_trace_context`` + the ``trace_context=``
  argument to ``start_as_current_observation`` — NOT a fresh trace per
  subagent (MIT-186 readiness).

Environment gate
----------------
``LANGFUSE_ENABLED`` is ``True`` iff the Langfuse client is importable AND
env vars are configured (``LANGFUSE_SECRET_KEY`` is the canonical gate,
matching the gate already present in ``providers/openai_compat_provider.py``).
The provider-level gate emits one ``generation`` per LLM call via the
``langfuse.openai`` drop-in wrapper; this module adds the span hierarchy
(turn → llm-iteration → tool / subagent) that gives those generations
parentage.
"""

from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Any, Iterator

from loguru import logger

if TYPE_CHECKING:
    from langfuse import Langfuse as _LangfuseClient  # pragma: no cover


def _detect_enabled() -> bool:
    """Check gate conditions at module import time.

    Mirrors the ``openai_compat_provider.py`` gate so both layers agree on
    whether Langfuse is wired.  ``LANGFUSE_SECRET_KEY`` is treated as the
    canonical switch because it is the minimum required credential; the
    default ``LANGFUSE_HOST`` points at the Langfuse Cloud SaaS endpoint
    when unset, which is still a valid target.
    """
    if not os.environ.get("LANGFUSE_SECRET_KEY"):
        return False
    if importlib.util.find_spec("langfuse") is None:
        logger.warning(
            "LANGFUSE_SECRET_KEY is set but langfuse package is not "
            "installed; tracing disabled. Install with "
            "`pip install langfuse` to enable observability."
        )
        return False
    return True


LANGFUSE_ENABLED: bool = _detect_enabled()


def _safe_get_client() -> "_LangfuseClient | None":
    """Return a Langfuse client, or ``None`` if unavailable.

    Never raises: SDK import, constructor, and ingest failures are all
    caught so the agent loop cannot be poisoned by observability.
    """
    if not LANGFUSE_ENABLED:
        return None
    try:
        from langfuse import get_client
        return get_client()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Langfuse client init failed, disabling tracing: {}", exc)
        return None


def is_langfuse_ready() -> bool:
    """Check ingest readiness (env vars set *and* client can be built).

    Cheap enough to call per-turn; the SDK caches the client after first
    call.  Returns ``False`` on any failure, which forces the no-op path.
    """
    return _safe_get_client() is not None


@contextmanager
def observe_turn(
    *,
    name: str,
    session_id: str | None = None,
    user_id: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    input_preview: str | None = None,
    tags: list[str] | None = None,
) -> Iterator[Any]:
    """Open the **root span** of a conversation turn.

    Wraps ``AgentLoop._process_message`` so every user turn is one trace
    with ``session_id``/``user_id`` propagated to every child span.
    ``propagate_attributes`` must fire inside this context before any
    downstream work (see MIT-202 task 3 — propagation is not
    retroactive).

    Yields the underlying ``LangfuseSpan`` when tracing is active, or
    ``None`` when the helper is a no-op.
    """
    client = _safe_get_client()
    if client is None:
        yield None
        return

    try:
        from langfuse import propagate_attributes
    except Exception as exc:  # pragma: no cover
        logger.debug("Langfuse import failed mid-call: {}", exc)
        yield None
        return

    metadata: dict[str, str] = {}
    if channel:
        metadata["channel"] = str(channel)[:200]
    if chat_id:
        metadata["chat_id"] = str(chat_id)[:200]

    try:
        # Open the root turn span first.  propagate_attributes runs
        # inside so session/user/tags propagate to all child spans —
        # critical because Langfuse only aggregates observations that
        # carry the attribute at *creation* time.
        span_cm = client.start_as_current_observation(
            name=name,
            as_type="agent",
            input=_truncate(input_preview, 4096),
        )
    except Exception as exc:
        logger.debug("Langfuse start_as_current_observation failed: {}", exc)
        yield None
        return

    try:
        with span_cm as span:
            try:
                attr_cm = propagate_attributes(
                    session_id=_sanitize_attr(session_id),
                    user_id=_sanitize_attr(user_id),
                    tags=tags,
                    metadata=metadata or None,
                )
            except Exception as exc:
                logger.debug("Langfuse propagate_attributes failed: {}", exc)
                attr_cm = nullcontext()

            with attr_cm:
                yield span
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Langfuse turn span context errored: {}", exc)


@contextmanager
def observe_llm_iteration(
    *,
    iteration: int,
    model: str | None = None,
) -> Iterator[Any]:
    """Wrap one LLM iteration so tool calls + the auto-traced generation
    nest under it.

    The ``langfuse.openai`` auto-instrumentation emits its ``generation``
    observation as a child of whichever span is currently active.  If
    no span is open the generation orphans directly under the trace
    root, losing tool-hierarchy context.  Opening this span *before*
    the provider call and keeping it open across tool dispatch is what
    produces the MIT-186 nested trace shape.
    """
    client = _safe_get_client()
    if client is None:
        yield None
        return

    metadata: dict[str, Any] = {"iteration": iteration}
    if model:
        metadata["model"] = model

    try:
        cm = client.start_as_current_observation(
            name=f"llm-iteration-{iteration}",
            as_type="span",
            metadata=metadata,
        )
    except Exception as exc:
        logger.debug("Langfuse llm-iteration span failed: {}", exc)
        yield None
        return

    try:
        with cm as span:
            yield span
    except Exception as exc:  # pragma: no cover
        logger.debug("Langfuse llm-iteration context errored: {}", exc)


@contextmanager
def observe_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Wrap a tool invocation as a child span.

    Meant to be opened inside ``observe_llm_iteration`` so tool spans
    nest under their owning generation — not as siblings under the
    turn root.
    """
    client = _safe_get_client()
    if client is None:
        yield None
        return

    try:
        cm = client.start_as_current_observation(
            name=f"tool:{tool_name}",
            as_type="tool",
            input=_truncate(arguments, 4096),
        )
    except Exception as exc:
        logger.debug("Langfuse tool span failed for {}: {}", tool_name, exc)
        yield None
        return

    try:
        with cm as span:
            yield span
    except Exception as exc:  # pragma: no cover
        logger.debug("Langfuse tool span context errored: {}", exc)


def capture_trace_context() -> dict[str, str] | None:
    """Capture the current trace + span ids so a subagent can attach as a
    child observation of the parent turn's trace.

    Returns ``None`` when Langfuse is disabled or no span is active.
    The returned dict matches the shape accepted by
    ``start_as_current_observation(trace_context=...)``:
    ``{"trace_id": ..., "parent_span_id": ...}``.

    Used at ``SubagentManager.spawn`` time — the caller captures the
    context synchronously in the main agent's OTel scope, then passes
    it into the async subagent task which opens its work under the
    captured parent.  This is the MIT-186 readiness hook.
    """
    client = _safe_get_client()
    if client is None:
        return None

    try:
        trace_id = client.get_current_trace_id()
        # Span id is read off the OTel active span.  The Langfuse Python
        # SDK does not expose a public getter in v4, so we reach into
        # OTel directly — still fail-safe if unavailable.
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        span_id_int = getattr(ctx, "span_id", 0)
        if not trace_id or not span_id_int:
            return None
        parent_span_id = format(span_id_int, "016x")
        return {"trace_id": trace_id, "parent_span_id": parent_span_id}
    except Exception as exc:
        logger.debug("Langfuse capture_trace_context failed: {}", exc)
        return None


@contextmanager
def observe_subagent(
    *,
    task_id: str,
    label: str,
    trace_context: dict[str, str] | None,
    input_preview: str | None = None,
) -> Iterator[Any]:
    """Open a span for a subagent task, nested inside the parent trace.

    When ``trace_context`` is provided, the subagent's observations
    share the parent turn's ``trace_id`` and hang off the parent span
    — NOT a separate trace.  When it is ``None`` the subagent opens a
    new trace, which is the pre-MIT-186 behaviour.
    """
    client = _safe_get_client()
    if client is None:
        yield None
        return

    kwargs: dict[str, Any] = {
        "name": f"subagent:{label}",
        "as_type": "agent",
        "metadata": {"task_id": task_id, "label": label},
        "input": _truncate(input_preview, 4096),
    }
    if trace_context:
        kwargs["trace_context"] = trace_context

    try:
        cm = client.start_as_current_observation(**kwargs)
    except Exception as exc:
        logger.debug("Langfuse subagent span failed for {}: {}", task_id, exc)
        yield None
        return

    try:
        with cm as span:
            yield span
    except Exception as exc:  # pragma: no cover
        logger.debug("Langfuse subagent context errored: {}", exc)


def _sanitize_attr(value: str | None) -> str | None:
    """Normalize propagate_attributes values (US-ASCII, ≤200 chars).

    ``propagate_attributes`` drops non-compliant values with a warning;
    we pre-truncate to avoid log noise on normal Discord/Slack ids.
    """
    if value is None:
        return None
    text = str(value)
    if len(text) > 200:
        text = text[:200]
    return text


def _truncate(value: Any, limit: int) -> Any:
    """Truncate string payloads; leave structured data intact.

    Langfuse accepts arbitrary JSON-serializable input; the limit only
    guards against multi-megabyte tool arg blobs.
    """
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value
