"""Execute tool calls and turn their outcomes into model observations."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from contextlib import nullcontext
from functools import cache
from typing import Any, cast

from loguru import logger

from nanobot.agent import turn_provenance
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.ask import AskUserInterrupt
from nanobot.agent.tools.browser_budget import browser_budget_error
from nanobot.agent.tools.file_state import file_read_context
from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result

# Ziggy-local (fork, MIT-202/MIT-211): Langfuse tool span (no-op without Langfuse).
from nanobot.observability import observe_tool
from nanobot.providers.base import ToolCallRequest
from nanobot.utils.runtime import (
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)

_RETRY_HINT = "\n\n[Analyze the error above and try a different approach.]"
# SSRF is a hard security block at the tool boundary, but the agent turn
# should recover conversationally instead of aborting the runtime.
_SSRF_MARKERS: tuple[str, ...] = (
    "internal/private url detected",
    "private/internal address",
    "private address",
)
_SSRF_BOUNDARY_NOTE = (
    "This is a non-bypassable security boundary. Stop trying to access "
    "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
    "alternate DNS, redirects, proxies, or another tool. Ask the user for "
    "local files, logs, screenshots, or an explicit safe public URL instead. "
    "If the user explicitly trusts this private URL, ask them to whitelist "
    "the exact IP/CIDR via tools.ssrfWhitelist."
)
# A hostname that does not resolve is blocked by the same guard, but it is a
# correctable mistake rather than a private-network target -- see
# tests/security/test_internal_url_reporting.py.
_UNRESOLVABLE_MARKER = "(unresolvable hostname)"
_UNRESOLVABLE_HOST_RE = re.compile(r"cannot resolve hostname:\s*([^\s,;)]+)", re.IGNORECASE)
_MAX_REPEAT_UNRESOLVABLE_HOSTS = 2
# Aggregate budget across *distinct* dead hostnames in one turn. Without it the
# per-host cap is near-useless: a model that guesses URLs guesses a new one each
# time, so every attempt starts a fresh counter.
_MAX_TOTAL_UNRESOLVABLE_HOSTS = 3
_UNRESOLVABLE_TOTAL_KEY = "unresolvable:*"
# Per-turn budget for SSRF blocks. The guard never yields, so this only bounds
# how many times an (possibly injected) model may probe before being told to stop.
_MAX_TOTAL_SSRF_BLOCKS = 3
_SSRF_TOTAL_KEY = "ssrf:*"
# Non-SSRF boundary markers returned to the model as recoverable tool errors.
_WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
    "outside the configured workspace",
    "outside allowed directory",
    "working_dir is outside",
    "working_dir could not be resolved",
    "path outside working dir",
    "path traversal detected",
)
# Ziggy-local (MIT-1849 / TP-08, design turn-provenance §6): prepare_call
# failures that are policy gates, not call failures. Matched against the
# exact message shapes produced by registry.prepare_call (shared-room,
# read-only-turn and ask_user denials); the tester Activity endpoint already
# maps ``refused`` rows (webui/ws_http.py).
_POLICY_DENIAL_MARKERS: tuple[str, ...] = (
    "is unavailable in a shared conversation",  # room_denial_message
    '"code":"shared_room_denied"',              # typed room denial envelope
    "is unavailable in a read-only turn",       # read_only_denial_message
    "is unavailable in a scheduled run",        # ask_user_unavailable_message
)


def _is_policy_denial(text: str) -> bool:
    return any(marker in text for marker in _POLICY_DENIAL_MARKERS)


def _record_tool_call(
    tools: ToolRegistry,
    tool_call: ToolCallRequest,
    params: Any,
    status: str,
    started_at: float,
    *,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """MIT-1849 (TP-08): write the one audit row + Prometheus sample per
    live tool call. Record-only — the result the model sees is untouched —
    and registries without the recorder are skipped."""
    record = getattr(tools, "record_call", None)
    if not callable(record):
        return
    record(
        tool_call.name,
        params if isinstance(params, dict) else {},
        status,
        (time.monotonic() - started_at) * 1000,
        error=error,
        extra=extra,
    )


def _with_retry_hint(payload: str) -> str:
    """Append the recovery hint exactly once."""
    if payload.endswith(_RETRY_HINT):
        return payload
    return payload + _RETRY_HINT


# Failure-envelope ``error.next`` values that instruct the model to stop
# trying (FR-FAIL-004, D5-agent-tools.md §3.4.2). Appending the generic
# "try a different approach" hint to these contradicts the envelope and
# invites workarounds, so the hint is suppressed for them.
_NO_RETRY_NEXTS: frozenset[str] = frozenset({"ask_user", "hand_off", "conclude"})


def _envelope_forbids_retry(text: str) -> bool:
    """Return whether a tool error is a failure envelope that says do not retry.

    True iff ``text`` parses as a JSON dict with ``ok`` exactly ``False`` and
    ``error.next`` in :data:`_NO_RETRY_NEXTS`. Anything else (plain text,
    malformed JSON, a retry envelope, an envelope without a dict ``error``)
    is False, so the generic retry hint is still appended.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return error.get("next") in _NO_RETRY_NEXTS


def _repaired_call_extra(repairs: list[str]) -> dict[str, Any] | None:
    """Audit-extra for a call whose arguments were silently repaired (TP-07)."""
    if not repairs:
        return None
    return {"args_repaired": True, "args_repair_kinds": list(repairs)}


def _annotate_repaired_event(event: dict[str, Any], repairs: list[str]) -> dict[str, Any]:
    """Tag a tool event with the repair kinds, never the repaired values."""
    extra = _repaired_call_extra(repairs)
    if extra is not None:
        event.update(extra)
    return event


def _note_args_repairs(repairs: list[str]) -> None:
    """Increment the turn provenance record's per-kind repair counters (TP-07).

    ``current_turn_provenance`` is owned by the TP-02 record; it is optional
    here so a registry running without provenance plumbing pays nothing.
    """
    if not repairs:
        return
    try:
        get_current = getattr(turn_provenance, "current_turn_provenance", None)
        if not callable(get_current):
            return
        record = get_current()
        if record is None:
            return
        counters = getattr(record, "args_repaired", None)
        if not isinstance(counters, dict):
            counters = {}
            try:
                setattr(record, "args_repaired", counters)
            except Exception:
                return
        for kind in repairs:
            counters[kind] = counters.get(kind, 0) + 1
    except Exception as exc:
        logger.debug("turn provenance args_repaired update failed: {}", exc)


async def execute_tool_calls(
    tools: ToolRegistry,
    tool_calls: list[ToolCallRequest],
    *,
    concurrent: bool,
    external_lookup_counts: dict[str, int],
    workspace_violation_counts: dict[str, int],
    hook: AgentHook,
    context: AgentHookContext,
    model_messages: list[dict[str, Any]] | None = None,
    compacted_tool_results: set[str] | None = None,
) -> tuple[list[Any], list[dict[str, Any]], BaseException | None]:
    """Execute one model response's tool calls in stable result order.

    Returns ``(results, events, fatal_error)``. A fatal error is the first
    turn-aborting signal seen (today only :class:`AskUserInterrupt`); later
    batches are skipped once one is observed so nothing runs past the pause.
    """
    @cache
    def read_results() -> dict[str, str]:
        """Index once, on the first read-dedup check in this batch."""
        return {
            message["tool_call_id"]: message["content"]
            for message in model_messages or []
            if message.get("role") == "tool"
            and isinstance(message.get("tool_call_id"), str)
            and isinstance(message.get("content"), str)
            and message["tool_call_id"] not in (compacted_tool_results or ())
        }
    tool_results: list[tuple[Any, dict[str, Any], BaseException | None]] = []
    for batch in _partition_tool_batches(tools, tool_calls, concurrent=concurrent):
        if concurrent and len(batch) > 1:
            batch_results = await asyncio.gather(*(
                _execute_tool_call(
                    tools,
                    tool_call,
                    external_lookup_counts,
                    workspace_violation_counts,
                    hook,
                    context,
                    read_results,
                )
                for tool_call in batch
            ))
            tool_results.extend(batch_results)
        else:
            batch_results: list[tuple[Any, dict[str, Any], BaseException | None]] = []
            for tool_call in batch:
                result = await _execute_tool_call(
                    tools,
                    tool_call,
                    external_lookup_counts,
                    workspace_violation_counts,
                    hook,
                    context,
                    read_results,
                )
                tool_results.append(result)
                batch_results.append(result)
                if isinstance(result[2], AskUserInterrupt):
                    break
        if any(isinstance(error, AskUserInterrupt) for _, _, error in batch_results):
            break

    results = [result for result, _event, _error in tool_results]
    events = [event for _result, event, _error in tool_results]
    fatal_error: BaseException | None = None
    for _result, _event, error in tool_results:
        if error is not None and fatal_error is None:
            fatal_error = error
    return results, events, fatal_error


async def _execute_tool_call(
    tools: ToolRegistry,
    tool_call: ToolCallRequest,
    external_lookup_counts: dict[str, int],
    workspace_violation_counts: dict[str, int],
    hook: AgentHook,
    context: AgentHookContext,
    read_results: Callable[[], dict[str, str]],
) -> tuple[Any, dict[str, Any], BaseException | None]:
    lookup_error = repeated_external_lookup_error(
        tool_call.name,
        tool_call.arguments,
        external_lookup_counts,
    )
    if lookup_error:
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": "repeated external lookup blocked",
        }
        return _with_retry_hint(lookup_error), event, None

    budget = browser_budget_error(tool_call.name, external_lookup_counts)
    if budget:
        event = {
            "name": tool_call.name,
            "status": "error",
            "detail": "browser turn budget exhausted",
        }
        return budget, event, None

    # TP-07: prefer prepare_call_ex, which also reports the silent argument
    # repairs it applied; registries without it keep the three-tuple contract.
    tool, params, prep_error, repairs = None, tool_call.arguments, None, []
    prepared_consumed = False
    prepare_call_ex = cast(
        Callable[[str, Any], object] | None,
        getattr(tools, "prepare_call_ex", None),
    )
    started_at = time.monotonic()
    if callable(prepare_call_ex):
        prepared_ex = prepare_call_ex(tool_call.name, tool_call.arguments)
        if isinstance(prepared_ex, tuple):
            ex_tuple = cast(tuple[object, ...], prepared_ex)
            if len(ex_tuple) == 4:
                tool, params, prep_error, raw_repairs = cast(
                    tuple[Any, Any, str | None, list[str]], ex_tuple
                )
                repairs = sorted({r for r in raw_repairs if isinstance(r, str)})
                prepared_consumed = True
    if not prepared_consumed:
        prepare_call = cast(
            Callable[[str, Any], object] | None,
            getattr(tools, "prepare_call", None),
        )
        if callable(prepare_call):
            prepared = prepare_call(tool_call.name, tool_call.arguments)
            if isinstance(prepared, tuple):
                prepared_tuple = cast(tuple[object, ...], prepared)
                if len(prepared_tuple) == 3:
                    tool, params, prep_error = cast(tuple[Any, Any, str | None], prepared_tuple)
    if repairs:
        _note_args_repairs(repairs)
    if prep_error:
        # Ziggy-local (MIT-1849): a call the gates refused still gets its
        # exactly-one audit row — ``refused`` for the policy gates, ``error``
        # for schema/not-found style rejections.
        _record_tool_call(
            tools,
            tool_call,
            params,
            "refused" if _is_policy_denial(str(prep_error)) else "error",
            started_at,
            error=str(prep_error)[:200],
            extra=_repaired_call_extra(repairs),
        )
        payload = _with_retry_hint(prep_error)
        event = _annotate_repaired_event(
            {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            },
            repairs,
        )
        handled = _classify_violation(
            raw_text=prep_error,
            soft_payload=payload,
            event=event,
            tool_call=tool_call,
            workspace_violation_counts=workspace_violation_counts,
        )
        if handled is not None:
            return handled + (None,)
        return payload, event, None

    await hook.before_execute_tool(context, tool_call, tool, params)
    # When the registry's own execute dispatches the call it records it
    # itself (MIT-1849); recording here would double-write that path.
    dispatched_here = tool is not None
    # TP-07: surface silent argument repairs on the Langfuse tool span too.
    tool_span_metadata: dict[str, Any] | None = None
    if repairs:
        tool_span_metadata = {
            "ziggy.args_repaired": True,
            "ziggy.args_repair_kinds": ",".join(repairs),
        }
    try:
        # Ziggy-local (fork, MIT-202/MIT-211): nest tool dispatch under the
        # active llm-iteration span so the Langfuse trace shows
        # "llm-iteration -> tool:<name>". observe_tool redacts and truncates
        # the arguments before export (MIT-211) and is a no-op when Langfuse
        # is not configured.
        with (
            observe_tool(
                tool_name=tool_call.name, arguments=params, metadata=tool_span_metadata,
            ),
            file_read_context(tool_call.id, read_results)
            if tool_call.name == "read_file" else nullcontext(),
        ):
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await tools.execute(tool_call.name, params)
    except asyncio.CancelledError:
        raise
    except AskUserInterrupt as interrupt:
        if dispatched_here:
            _record_tool_call(
                tools, tool_call, params, "waiting", started_at,
                extra=_repaired_call_extra(repairs),
            )
        event = _annotate_repaired_event(
            {
                "name": tool_call.name,
                "status": "waiting",
                "detail": interrupt.question.replace("\n", " ").strip()[:120],
            },
            repairs,
        )
        return "", event, interrupt
    except Exception as exc:
        if dispatched_here:
            _record_tool_call(
                tools, tool_call, params, "error", started_at, error=str(exc)[:200],
                extra=_repaired_call_extra(repairs),
            )
        await hook.on_execute_tool_error(context, tool_call, tool, params, exc)
        event = _annotate_repaired_event(
            {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            },
            repairs,
        )
        payload = _with_retry_hint(f"Error: {type(exc).__name__}: {exc}")
        handled = _classify_violation(
            raw_text=str(exc),
            soft_payload=payload,
            event=event,
            tool_call=tool_call,
            workspace_violation_counts=workspace_violation_counts,
        )
        if handled is not None:
            return handled + (None,)
        return payload, event, None

    if dispatched_here:
        _record_tool_call(
            tools,
            tool_call,
            params,
            "error" if is_tool_error_result(result) else "ok",
            started_at,
            error=str(result)[:200] if is_tool_error_result(result) else None,
            extra=_repaired_call_extra(repairs),
        )

    if is_tool_error_result(result):
        await hook.on_execute_tool_error(context, tool_call, tool, params, result)
        payload = str(result) if _envelope_forbids_retry(result) else _with_retry_hint(result)
        event = _annotate_repaired_event(
            {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            },
            repairs,
        )
        handled = _classify_violation(
            raw_text=result,
            soft_payload=payload,
            event=event,
            tool_call=tool_call,
            workspace_violation_counts=workspace_violation_counts,
        )
        if handled is not None:
            return handled + (None,)
        return payload, event, None

    await hook.after_execute_tool(context, tool_call, tool, params, result)

    detail = "" if result is None else str(result)
    detail = detail.replace("\n", " ").strip()
    if not detail:
        detail = "(empty)"
    elif len(detail) > 120:
        detail = detail[:120] + "..."
    return result, _annotate_repaired_event(
        {"name": tool_call.name, "status": "ok", "detail": detail},
        repairs,
    ), None


def is_unresolvable_host(text: str) -> bool:
    """Return whether a tool error describes a hostname that does not resolve.

    Checked *before* the SSRF classification: an unreachable name is refused
    by the same guard but is a correctable mistake, not an attempt to reach a
    private network, and must not inherit the non-bypassable SSRF advice.
    """
    if not text:
        return False
    return _UNRESOLVABLE_MARKER in text.lower()


def _unresolvable_host_key(text: str) -> str:
    """Throttle key for a repeated unresolvable hostname."""
    match = _UNRESOLVABLE_HOST_RE.search(text)
    host = match.group(1).lower() if match else "unknown"
    return f"unresolvable:{host}"


def is_ssrf_violation(text: str) -> bool:
    """Return whether a tool error describes a blocked private-network request."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SSRF_MARKERS)


def _is_workspace_violation(text: str) -> bool:
    """Return whether text describes any workspace or network boundary rejection."""
    if not text:
        return False
    lowered = text.lower()
    if is_ssrf_violation(lowered):
        return True
    return any(marker in lowered for marker in _WORKSPACE_VIOLATION_MARKERS)


def _classify_violation(
    *,
    raw_text: str,
    soft_payload: str,
    event: dict[str, Any],
    tool_call: ToolCallRequest,
    workspace_violation_counts: dict[str, int],
) -> tuple[Any, dict[str, Any]] | None:
    if is_unresolvable_host(raw_text):
        # Recoverable: the name does not exist, so the model should fix the URL.
        # Capped two ways. A per-host counter catches a model retrying the same
        # dead name; an aggregate counter catches the far more common case of a
        # model *inventing a fresh* hostname each time, which a per-host key
        # alone never trips -- every new guess would start again at one.
        host = _unresolvable_host_key(raw_text)
        count = workspace_violation_counts.get(host, 0) + 1
        workspace_violation_counts[host] = count
        total = workspace_violation_counts.get(_UNRESOLVABLE_TOTAL_KEY, 0) + 1
        workspace_violation_counts[_UNRESOLVABLE_TOTAL_KEY] = total
        event["detail"] = _event_detail("unresolvable_host: ", raw_text)
        if count > _MAX_REPEAT_UNRESOLVABLE_HOSTS or total > _MAX_TOTAL_UNRESOLVABLE_HOSTS:
            logger.warning(
                "Tool {} retried an unresolvable hostname {} times; escalating",
                tool_call.name,
                count,
            )
            event["detail"] = _event_detail("unresolvable_host_escalated: ", raw_text)
            return (
                "Error: repeated lookups of hostnames that do not resolve.\n"
                f"{raw_text.strip()}\n\n"
                "Stop guessing URLs. Use a documented endpoint, a configured "
                "search tool, or tell the user you could not reach the service "
                "and ask them for the correct address."
            ), event
        return soft_payload, event

    if is_ssrf_violation(raw_text):
        # The turn deliberately does not abort here (#3599/#3605), but the
        # attempts must still be bounded: without a cap an injected model gets
        # a fresh probe every iteration. The block itself never yields -- this
        # only decides how many times we restate it before saying "stop".
        total = workspace_violation_counts.get(_SSRF_TOTAL_KEY, 0) + 1
        workspace_violation_counts[_SSRF_TOTAL_KEY] = total
        logger.warning(
            "Tool {} blocked by SSRF guard ({} this turn); returning non-retryable tool error: {}",
            tool_call.name,
            total,
            raw_text.replace("\n", " ").strip()[:200],
        )
        event["detail"] = _event_detail("ssrf_violation: ", raw_text)
        if total > _MAX_TOTAL_SSRF_BLOCKS:
            event["detail"] = _event_detail("ssrf_violation_escalated: ", raw_text)
            return (
                "Error: refusing repeated attempts to reach private/internal addresses.\n"
                f"{raw_text.strip()}\n\n"
                f"You have been blocked {total} times in this turn. Stop. Trying "
                "another host, encoding, port, redirect, or tool will not change "
                "the answer. Tell the user what you could not reach and ask how "
                "they want to proceed."
            ), event
        return _ssrf_soft_payload(raw_text), event

    if _is_workspace_violation(raw_text):
        escalation = repeated_workspace_violation_error(
            tool_call.name,
            tool_call.arguments,
            workspace_violation_counts,
        )
        event["detail"] = _event_detail("workspace_violation: ", raw_text)
        if escalation is not None:
            logger.warning(
                "Tool {} hit workspace boundary repeatedly; escalating hint",
                tool_call.name,
            )
            event["detail"] = _event_detail(
                "workspace_violation_escalated: ",
                raw_text,
            )
            return escalation, event
        return soft_payload, event

    return None


def _ssrf_soft_payload(raw_text: str) -> str:
    text = raw_text.strip() or "Error: request blocked by SSRF guard"
    return f"{text}\n\n{_SSRF_BOUNDARY_NOTE}"


def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
    return (prefix + text.replace("\n", " ").strip())[:limit]


def _partition_tool_batches(
    tools: ToolRegistry,
    tool_calls: list[ToolCallRequest],
    *,
    concurrent: bool,
) -> list[list[ToolCallRequest]]:
    if not concurrent:
        return [[tool_call] for tool_call in tool_calls]

    batches: list[list[ToolCallRequest]] = []
    current: list[ToolCallRequest] = []
    for tool_call in tool_calls:
        get_tool = cast(Callable[[str], Any] | None, getattr(tools, "get", None))
        tool = get_tool(tool_call.name) if callable(get_tool) else None
        can_batch = bool(tool and tool.concurrency_safe)
        if can_batch:
            current.append(tool_call)
            continue
        if current:
            batches.append(current)
            current = []
        batches.append([tool_call])
    if current:
        batches.append(current)
    return batches
