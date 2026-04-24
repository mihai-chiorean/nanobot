"""Unit tests for the Langfuse observability layer.

These tests exercise the fail-safe paths (env vars absent, SDK import
failure, client init failure) and the trace-context-propagation shape
WITHOUT hitting a live Langfuse server.  The live-ingest path is covered
by the Chunk 2 deploy verification on Spark.
"""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock, patch


@contextmanager
def _clean_langfuse_env() -> Iterator[None]:
    """Temporarily remove every LANGFUSE_* env var and cached import."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("LANGFUSE_")}
    for k in list(os.environ):
        if k.startswith("LANGFUSE_"):
            del os.environ[k]
    # Purge cached module so module-level LANGFUSE_ENABLED re-evaluates.
    for mod in ("nanobot.observability", "nanobot.observability.langfuse"):
        sys.modules.pop(mod, None)
    try:
        yield
    finally:
        os.environ.update(saved)
        for mod in ("nanobot.observability", "nanobot.observability.langfuse"):
            sys.modules.pop(mod, None)


def test_langfuse_disabled_when_env_var_absent() -> None:
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import LANGFUSE_ENABLED
        assert LANGFUSE_ENABLED is False


def test_observe_turn_is_no_op_when_disabled() -> None:
    """Every helper MUST be safe when Langfuse is off — agent must keep running."""
    with _clean_langfuse_env():
        from nanobot.observability import (
            capture_trace_context,
            observe_llm_iteration,
            observe_subagent,
            observe_tool,
            observe_turn,
        )
        # None of these should raise or block.
        with observe_turn(
            name="turn:cli", session_id="s1", user_id="u1",
            channel="cli", chat_id="direct", input_preview="hi",
        ) as span:
            assert span is None
            with observe_llm_iteration(iteration=0, model="test") as span2:
                assert span2 is None
                with observe_tool(tool_name="read_file", arguments={"path": "x"}):
                    pass
        assert capture_trace_context() is None
        with observe_subagent(
            task_id="t1", label="x", trace_context=None, input_preview="go",
        ):
            pass


def test_capture_trace_context_returns_none_when_disabled() -> None:
    with _clean_langfuse_env():
        from nanobot.observability import capture_trace_context
        assert capture_trace_context() is None


def test_is_langfuse_ready_false_when_disabled() -> None:
    with _clean_langfuse_env():
        from nanobot.observability import is_langfuse_ready
        assert is_langfuse_ready() is False


def test_observe_turn_swallows_sdk_exceptions() -> None:
    """If the Langfuse client raises during start_as_current_observation,
    observe_turn must still yield control — not crash the agent loop."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        # Force LANGFUSE_ENABLED True for this test
        with patch.object(obs_mod, "LANGFUSE_ENABLED", True):
            broken_client = MagicMock()
            broken_client.start_as_current_observation.side_effect = RuntimeError("boom")
            with patch.object(obs_mod, "_safe_get_client", return_value=broken_client):
                # Must not raise.
                with obs_mod.observe_turn(name="turn:cli", session_id="s1") as span:
                    assert span is None


def test_observe_tool_swallows_sdk_exceptions() -> None:
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        broken_client = MagicMock()
        broken_client.start_as_current_observation.side_effect = RuntimeError("boom")
        with patch.object(obs_mod, "_safe_get_client", return_value=broken_client):
            with obs_mod.observe_tool(tool_name="read_file", arguments={"p": 1}) as span:
                assert span is None


def test_observe_subagent_passes_trace_context_to_sdk() -> None:
    """Key MIT-186 assertion: trace_context kwarg reaches start_as_current_observation."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        mock_client = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="span"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_client.start_as_current_observation.return_value = mock_cm
        tc = {"trace_id": "abc123", "parent_span_id": "deadbeef"}
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with obs_mod.observe_subagent(
                task_id="t1",
                label="backend-architect",
                trace_context=tc,
                input_preview="plan things",
            ):
                pass
        args, kwargs = mock_client.start_as_current_observation.call_args
        assert kwargs["trace_context"] == tc, (
            "subagent span must attach to parent trace via trace_context kwarg"
        )
        assert kwargs["as_type"] == "agent"
        assert kwargs["name"] == "subagent:backend-architect"


def test_observe_subagent_omits_trace_context_when_none() -> None:
    """Top-level subagent with no parent: do NOT pass trace_context kwarg
    (letting the SDK open a fresh trace)."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        mock_client = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="span"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_client.start_as_current_observation.return_value = mock_cm
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with obs_mod.observe_subagent(
                task_id="t1",
                label="backend-architect",
                trace_context=None,
                input_preview="plan things",
            ):
                pass
        _, kwargs = mock_client.start_as_current_observation.call_args
        assert "trace_context" not in kwargs


def test_capture_trace_context_shape_when_enabled() -> None:
    """When Langfuse + an active OTel span are both present, capture_trace_context
    returns the {trace_id, parent_span_id} shape v4 accepts."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        mock_client = MagicMock()
        mock_client.get_current_trace_id.return_value = "abc123"
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            # Mock the OTel span layer too.
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = MagicMock(span_id=0xdeadbeef)
            with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
                ctx = obs_mod.capture_trace_context()
        assert ctx is not None
        assert ctx["trace_id"] == "abc123"
        assert ctx["parent_span_id"] == format(0xdeadbeef, "016x")


def test_sanitize_attr_truncates_long_values() -> None:
    """propagate_attributes limits to 200 chars — we pre-truncate silently."""
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _sanitize_attr
        assert _sanitize_attr(None) is None
        long_val = "x" * 500
        assert len(_sanitize_attr(long_val)) == 200


def test_truncate_helper_handles_string_and_other_types() -> None:
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _truncate
        assert _truncate("short", 100) == "short"
        assert _truncate("x" * 1000, 50).startswith("x" * 50)
        assert "truncated" in _truncate("x" * 1000, 50)
        # Structured data passes through untouched.
        assert _truncate({"a": 1}, 10) == {"a": 1}


# ---------------------------------------------------------------------------
# MIT-210: application exceptions raised inside observed spans must propagate
# ---------------------------------------------------------------------------


def _mock_active_client() -> MagicMock:
    """Return a Langfuse client mock whose observation CM enters/exits cleanly."""
    mock_client = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="span"))
    mock_cm.__exit__ = MagicMock(return_value=False)
    mock_client.start_as_current_observation.return_value = mock_cm
    return mock_client


def test_observe_turn_propagates_application_exceptions() -> None:
    """MIT-210: RuntimeError raised inside `with observe_turn(...)` must propagate."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        import pytest as _pytest
        mock_client = _mock_active_client()
        # Stub propagate_attributes to a trivial context manager so the
        # import-inside-observe_turn works without a real Langfuse install.
        stub_propagate = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=None),
            __exit__=MagicMock(return_value=False),
        ))
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            # Patch `langfuse.propagate_attributes` import target.
            fake_module = MagicMock()
            fake_module.propagate_attributes = stub_propagate
            with patch.dict(sys.modules, {"langfuse": fake_module}):
                with _pytest.raises(RuntimeError, match="boom"):
                    with obs_mod.observe_turn(name="turn:cli", session_id="s1"):
                        raise RuntimeError("boom")


def test_observe_llm_iteration_propagates_application_exceptions() -> None:
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        import pytest as _pytest
        mock_client = _mock_active_client()
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with _pytest.raises(RuntimeError, match="boom"):
                with obs_mod.observe_llm_iteration(iteration=0, model="test"):
                    raise RuntimeError("boom")


def test_observe_tool_propagates_application_exceptions() -> None:
    """MIT-210 canonical test — tool-path suppression was the worst case
    because it could leave `result` unset and produce UnboundLocalError."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        import pytest as _pytest
        mock_client = _mock_active_client()
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with _pytest.raises(RuntimeError, match="boom"):
                with obs_mod.observe_tool(tool_name="exec", arguments={"command": "ls"}):
                    raise RuntimeError("boom")


def test_observe_subagent_propagates_application_exceptions() -> None:
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        import pytest as _pytest
        mock_client = _mock_active_client()
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with _pytest.raises(RuntimeError, match="boom"):
                with obs_mod.observe_subagent(
                    task_id="t1",
                    label="x",
                    trace_context=None,
                    input_preview="plan",
                ):
                    raise RuntimeError("boom")


def test_observe_tool_swallows_sdk_init_exception() -> None:
    """MIT-210 counterpart: SDK errors at span CREATION time must still be
    swallowed + fall back to yield None.  Regression guard so fixing the
    propagation bug doesn't accidentally re-expose every SDK hiccup."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        broken_client = MagicMock()
        broken_client.start_as_current_observation.side_effect = RuntimeError("sdk boom")
        with patch.object(obs_mod, "_safe_get_client", return_value=broken_client):
            # No exception must escape.
            with obs_mod.observe_tool(tool_name="exec", arguments={"command": "ls"}) as span:
                assert span is None


# ---------------------------------------------------------------------------
# MIT-211: redact + truncate tool args / user input before export to Langfuse
# ---------------------------------------------------------------------------


def test_prepare_input_redacts_bearer_token_in_string() -> None:
    """The redactor must replace a bearer-token-shaped value before export."""
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _prepare_input
        raw = "Authorization: Bearer abcdef0123456789ABCDEFghijklmn0123456"
        out = _prepare_input(raw)
        assert isinstance(out, str)
        assert "abcdef0123456789" not in out
        assert "REDACTED" in out


def test_prepare_input_redacts_aws_access_key() -> None:
    """AKIA-prefixed AWS access keys must be redacted from the input payload."""
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _prepare_input
        raw = "hello AKIAIOSFODNN7EXAMPLE world"
        out = _prepare_input(raw)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "REDACTED" in out


def test_prepare_input_redacts_secret_in_dict_arguments() -> None:
    """Dict arguments get JSON-serialized, then redacted, before truncation.

    Mirrors the observe_tool call shape: `arguments={"command": "..."}`.
    """
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _prepare_input
        args = {
            "command": (
                "export SK_TEST=sk-live-abc123 && "
                "curl -H 'Authorization: Bearer abcdef0123456789ABCDEFghijklmn0123456'"
            ),
        }
        out = _prepare_input(args)
        assert isinstance(out, str)
        # Both the bearer token and the surrounding secret-bearing string
        # must be gone.
        assert "abcdef0123456789" not in out
        assert "REDACTED" in out


def test_prepare_input_passes_through_clean_string() -> None:
    """Non-sensitive input must pass through unchanged (beyond truncation)."""
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _prepare_input
        assert _prepare_input("hello world") == "hello world"
        assert _prepare_input({"path": "/tmp/x"}) == '{"path": "/tmp/x"}'


def test_prepare_input_truncates_long_input_to_512_chars() -> None:
    """Budget is 512 (not 4096); suffix marks the truncation."""
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _prepare_input
        big = "x" * 2000
        out = _prepare_input(big)
        assert isinstance(out, str)
        # 512 body + "…[truncated]" suffix.
        assert out.startswith("x" * 512)
        assert "truncated" in out
        # Never exceed the body size by more than the fixed suffix.
        assert len(out) < 512 + 64


def test_prepare_input_redaction_runs_before_truncation() -> None:
    """Regression guard: the redactor must see the FULL string, not a
    pre-truncated prefix.  Otherwise a 512-char truncation landing inside
    an `AKIA…` token would let the prefix leak verbatim."""
    with _clean_langfuse_env():
        from nanobot.observability.langfuse import _prepare_input
        # Pad past the 512-char truncation boundary, then plant the secret
        # well past position 512 so it would be sliced away if truncation
        # ran first.
        padding = "x" * 1000
        raw = padding + " AKIAIOSFODNN7EXAMPLE " + padding
        out = _prepare_input(raw)
        # If truncation ran BEFORE redaction, the AKIA token would be
        # out of bounds and the 512-char prefix would leak verbatim (no
        # "REDACTED" marker).  Correct order yields the redaction notice.
        assert "REDACTED" in out


def test_observe_tool_redacts_secrets_in_arguments() -> None:
    """End-to-end MIT-211: tool arguments with secrets must not reach
    `start_as_current_observation(input=...)` in the clear."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        mock_client = _mock_active_client()
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with obs_mod.observe_tool(
                tool_name="exec",
                arguments={
                    "command": (
                        "export SK_TEST=sk-live-abc123 && "
                        "curl -H 'Authorization: Bearer abcdef0123456789ABCDEFghijklmn0123456'"
                    ),
                },
            ):
                pass
        _, kwargs = mock_client.start_as_current_observation.call_args
        exported = kwargs["input"]
        assert isinstance(exported, str)
        assert "abcdef0123456789" not in exported
        assert "REDACTED" in exported


def test_observe_turn_redacts_user_input() -> None:
    """End-to-end MIT-211: user-message previews containing an AWS key
    must be redacted before reaching the Langfuse input field."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        mock_client = _mock_active_client()
        stub_propagate = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=None),
            __exit__=MagicMock(return_value=False),
        ))
        fake_module = MagicMock()
        fake_module.propagate_attributes = stub_propagate
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with patch.dict(sys.modules, {"langfuse": fake_module}):
                with obs_mod.observe_turn(
                    name="turn:cli",
                    session_id="s1",
                    input_preview="please deploy with AKIAIOSFODNN7EXAMPLE",
                ):
                    pass
        _, kwargs = mock_client.start_as_current_observation.call_args
        exported = kwargs["input"]
        assert isinstance(exported, str)
        assert "AKIAIOSFODNN7EXAMPLE" not in exported
        assert "REDACTED" in exported


def test_observe_tool_truncation_after_redaction_e2e() -> None:
    """End-to-end regression guard: even with a huge payload, the redactor
    sees the full string first and the truncation cap is 512."""
    with _clean_langfuse_env():
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-test"
        from nanobot.observability import langfuse as obs_mod
        mock_client = _mock_active_client()
        with patch.object(obs_mod, "_safe_get_client", return_value=mock_client):
            with obs_mod.observe_tool(
                tool_name="write_file",
                arguments={"content": "y" * 5000},
            ):
                pass
        _, kwargs = mock_client.start_as_current_observation.call_args
        exported = kwargs["input"]
        assert isinstance(exported, str)
        # Clean payload → redactor passes through → truncated at 512.
        assert len(exported) < 512 + 64
        assert "truncated" in exported
