from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry


class _FakeTool(Tool):
    def __init__(self, name: str, schema: dict[str, Any] | None = None):
        self._name = name
        self._schema = schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema or {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return kwargs

def _tool_names(definitions: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for definition in definitions:
        fn = definition.get("function", {})
        names.append(fn.get("name", ""))
    return names


def _registry_with_names(names: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_FakeTool(name))
    return registry


def test_get_definitions_orders_builtins_then_mcp_tools() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("mcp_git_status"))
    registry.register(_FakeTool("write_file"))
    registry.register(_FakeTool("mcp_fs_list"))
    registry.register(_FakeTool("read_file"))

    assert _tool_names(registry.get_definitions()) == [
        "read_file",
        "write_file",
        "mcp_fs_list",
        "mcp_git_status",
    ]


def test_prepare_call_rejects_near_miss_tool_name_with_suggestion() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))

    tool, params, error = registry.prepare_call("readFile", {"path": "foo.txt"})

    assert tool is None
    assert params == {"path": "foo.txt"}
    assert error is not None
    assert "Tool 'readFile' not found" in error
    assert "Did you mean 'read_file'?" in error
    assert "must match exactly" in error


def test_suggest_name_handles_canonical_tool_name_variants() -> None:
    registry = _registry_with_names(["read_file"])
    expected = {
        "readFile": "read_file",
        "read-file": "read_file",
        "READ_FILE": "read_file",
        "read file": "read_file",
        "readfile": "read_file",
    }

    assert {name: registry._suggest_name(name) for name in expected} == expected


def test_suggest_name_suppresses_low_confidence_and_non_unique_matches() -> None:
    registry = _registry_with_names(["read_file", "write_file"])

    for name in ["", "foo", "read", "file", "readfil", "read_file_tool"]:
        assert registry._suggest_name(name) is None

    ambiguous = _registry_with_names(["read_file", "readFile"])
    assert ambiguous._suggest_name("readfile") is None


def test_suggest_name_updates_after_register_and_unregister() -> None:
    registry = _registry_with_names(["read_file"])

    assert registry._suggest_name("readFile") == "read_file"

    registry.register(_FakeTool("readFile"))
    assert registry._suggest_name("read-file") is None

    registry.unregister("read_file")
    assert registry._suggest_name("read-file") == "readFile"


def test_prepare_call_read_file_rejects_non_object_params_with_actionable_hint() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))

    tool, params, error = registry.prepare_call("read_file", ["foo.txt"])

    assert tool is not None
    assert params == ["foo.txt"]
    assert error is not None
    assert "must be a JSON object" in error
    assert 'tool_name(param1="value1", param2="value2")' in error
    assert "matching the tool schema" in error


def test_prepare_call_parses_json_string_arguments() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))

    tool, params, error = registry.prepare_call("read_file", '{"path":"foo.txt"}')

    assert tool is not None
    assert params == {"path": "foo.txt"}
    assert error is None


def test_prepare_call_rejects_malformed_json_string_arguments() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))

    tool, params, error = registry.prepare_call("read_file", '{path:"foo.txt"}')

    assert tool is not None
    assert params == '{path:"foo.txt"}'
    assert error is not None
    assert "parameters must be a JSON object" in error


def test_prepare_call_rejects_scalar_for_single_required_parameter() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool(
        "web_fetch",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    ))

    tool, params, error = registry.prepare_call("web_fetch", "https://example.com")

    assert tool is not None
    assert params == "https://example.com"
    assert error is not None
    assert "parameters must be a JSON object" in error


def test_prepare_call_rejects_unquoted_scalar_strings_before_schema_cast() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool(
        "message",
        {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    ))

    tool, params, error = registry.prepare_call("message", "true")

    assert tool is not None
    assert params == "true"
    assert error is not None
    assert "parameters must be a JSON object" in error


def test_prepare_call_unwraps_arguments_payload() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool(
        "read_file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ))

    tool, params, error = registry.prepare_call(
        "read_file",
        {"arguments": '{"path":"foo.txt"}'},
    )

    assert tool is not None
    assert params == {"path": "foo.txt"}
    assert error is None


def test_prepare_call_treats_none_arguments_as_empty_object() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("list_exec_sessions"))

    tool, params, error = registry.prepare_call("list_exec_sessions", None)

    assert tool is not None
    assert params == {}
    assert error is None

    tool, params, error = registry.prepare_call("list_exec_sessions", "null")

    assert tool is not None
    assert params == "null"
    assert error is not None
    assert "parameters must be a JSON object" in error


def test_prepare_call_other_tools_keep_generic_object_validation() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("grep"))

    tool, params, error = registry.prepare_call("grep", ["TODO"])

    assert tool is not None
    assert params == ["TODO"]
    assert error == (
        "Error: Tool 'grep' parameters must be a JSON object, got list. "
        'Use named parameters like tool_name(param1="value1", param2="value2") '
        "matching the tool schema."
    )


async def test_registry_rejects_unknown_builtin_tool_parameters(tmp_path) -> None:
    (tmp_path / "sample.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(
        ReadFileTool(
            workspace=tmp_path,
            allowed_dir=tmp_path,
            restrict_to_workspace=True,
        )
    )

    result = await registry.execute(
        "read_file",
        {"path": "sample.txt", "line_limit": 1},
    )

    assert "Invalid parameters" in result
    assert "unexpected parameter line_limit" in result
    assert "one" not in result


async def test_registry_preserves_successful_exec_output_that_starts_with_error() -> None:
    registry = ToolRegistry()
    output = "Error: generated report successfully\n\nExit code: 0"
    tool = _FakeTool("exec")
    tool.execute = AsyncMock(return_value=output)
    registry.register(tool)

    result = await registry.execute("exec", {})

    assert result == output


async def test_registry_uses_structured_tool_result_for_errors() -> None:
    registry = ToolRegistry()
    output = "Error: plain tool output, not a structured failure"
    raw_tool = _FakeTool("raw_output")
    raw_tool.execute = AsyncMock(return_value=output)
    registry.register(raw_tool)

    raw_result = await registry.execute("raw_output", {})

    assert raw_result == output

    failing_tool = _FakeTool("failing_tool")
    failing_tool.execute = AsyncMock(return_value=ToolResult.error("Error: real failure"))
    registry.register(failing_tool)

    error_result = await registry.execute("failing_tool", {})

    assert isinstance(error_result, ToolResult)
    assert error_result.is_error
    assert error_result.startswith("Error: real failure")
    assert "[Analyze the error above" in error_result


def test_get_definitions_returns_cached_result() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    first = registry.get_definitions()
    assert registry._cached_definitions is not None
    second = registry.get_definitions()
    assert first is second


def test_register_invalidates_cache() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    first = registry.get_definitions()
    registry.register(_FakeTool("write_file"))
    second = registry.get_definitions()
    assert first is not second
    assert len(second) == 2


def test_unregister_invalidates_cache() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool("read_file"))
    registry.register(_FakeTool("write_file"))
    first = registry.get_definitions()
    registry.unregister("write_file")
    second = registry.get_definitions()
    assert first is not second
    assert len(second) == 1


# ---------------------------------------------------------------------------
# Secret redaction (MIT-122)
#
# ToolRegistry.execute() must scrub embedded secrets from string results so
# that, even if a misbehaving or adversarial tool pulls a private key or API
# token into its output, the model never gets to see it. Non-string results
# (e.g. ReadFileTool returning a list of multimodal image blocks) must pass
# through untouched.
# ---------------------------------------------------------------------------


class _StringReturningTool(Tool):
    """Fake tool that returns a caller-supplied string verbatim."""

    def __init__(self, name: str, payload: str):
        self._name = name
        self._payload = payload

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return self._payload


class _ListReturningTool(Tool):
    """Fake tool that returns a list result (mimics ReadFileTool for images)."""

    def __init__(self, name: str, payload: list[dict[str, Any]]):
        self._name = name
        self._payload = payload

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return self._payload


class _RaisingTool(Tool):
    """Fake tool that raises a caller-supplied exception when executed.

    Used to exercise the `except Exception` branch of `ToolRegistry.execute()`.
    """

    def __init__(self, name: str, exc: BaseException):
        self._name = name
        self._exc = exc

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        raise self._exc


@pytest.mark.asyncio
async def test_execute_redacts_private_key_in_output() -> None:
    payload = (
        "Here are some notes:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAy3... (truncated)\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    registry = ToolRegistry()
    registry.register(_StringReturningTool("leaky", payload))

    result = await registry.execute("leaky", {})

    assert "BEGIN RSA PRIVATE KEY" not in result
    assert "REDACTED" in result
    assert "security policy" in result.lower()


@pytest.mark.asyncio
async def test_execute_redacts_aws_access_key() -> None:
    payload = "export AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP\n"
    registry = ToolRegistry()
    registry.register(_StringReturningTool("leaky", payload))

    result = await registry.execute("leaky", {})

    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "REDACTED" in result


@pytest.mark.asyncio
async def test_execute_redacts_github_token() -> None:
    payload = "Authorization: token ghp_0123456789abcdef0123456789abcdef0123\n"
    registry = ToolRegistry()
    registry.register(_StringReturningTool("leaky", payload))

    result = await registry.execute("leaky", {})

    assert "ghp_0123456789abcdef0123456789abcdef0123" not in result
    assert "REDACTED" in result


@pytest.mark.asyncio
async def test_execute_passes_clean_output_unchanged() -> None:
    payload = "hello, world — no secrets here"
    registry = ToolRegistry()
    registry.register(_StringReturningTool("clean", payload))

    result = await registry.execute("clean", {})

    assert result == payload


@pytest.mark.asyncio
async def test_execute_does_not_touch_list_results() -> None:
    """Non-string results (image blocks from ReadFileTool) must pass through."""
    image_blocks: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "_meta": {"path": "/tmp/pixel.png"},
        },
        {"type": "text", "text": "(Image file: /tmp/pixel.png)"},
    ]
    registry = ToolRegistry()
    registry.register(_ListReturningTool("read_file", image_blocks))

    result = await registry.execute("read_file", {})

    # Identity — the list must flow through untouched, not stringified.
    assert result is image_blocks
    assert isinstance(result, list)
    assert result[0]["image_url"]["url"] == "data:image/png;base64,AAAA"


# ---------------------------------------------------------------------------
# Error-path redaction (MIT-147)
#
# Before MIT-147, `ToolRegistry.execute()` short-circuited on
# `isinstance(result, str) and result.startswith("Error")` and returned
# `result + _HINT` without running the secret scrubber. A tool that produced
# an error string carrying a secret — e.g. an auth module that echoes the
# rejected AKIA key back in its error message, or a PEM parser that dumps
# the offending blob — would therefore leak that secret straight to the
# model.  MIT-147 routes both success and error branches through the same
# `redact_if_sensitive()` call; the `_HINT` suffix is appended *after*
# redaction on the error branch so the "try another approach" nudge still
# reaches the model.
#
# Downstream contract: `AgentRunner._run_tool()` (and other consumers) use
# `result.startswith("Error")` to detect failed tool calls. The bare
# redaction notice does NOT begin with "Error", so the registry re-wraps
# the redacted body in an "Error ..." shell on the error branches to
# preserve that contract.
# ---------------------------------------------------------------------------


class _ErrorReturningTool(_StringReturningTool):
    """Fake tool whose output is a *structured* failure (ToolResult.error).

    Post-upstream-merge: a bare string that merely starts with "Error" is no
    longer a failure — upstream made failure detection structural via
    ``ToolResult.is_error``. Tests that exercise the error branch must produce
    a real ToolResult failure.
    """

    async def execute(self, **kwargs: Any) -> Any:
        return ToolResult.error(await super().execute(**kwargs))


@pytest.mark.asyncio
async def test_execute_redacts_aws_key_in_error_output() -> None:
    """MIT-147: AKIA-style secrets embedded in Error: strings must be scrubbed."""
    payload = "Error: invalid AWS credentials: AKIAABCDEFGHIJKLMNOP is not valid"
    registry = ToolRegistry()
    registry.register(_ErrorReturningTool("boom", payload))

    result = await registry.execute("boom", {})

    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "REDACTED" in result
    # The error-path hint still attaches so the model knows to try again.
    assert "Analyze the error above" in result
    # Downstream-contract: failure detection is structural post-merge.
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_redacts_pem_material_in_error_output() -> None:
    """MIT-147: a tool that dumps a PEM blob into its error message must have it scrubbed."""
    payload = (
        "Error parsing key file:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAy3SECRETSECRETSECRETSECRETSECRETSECRETSECRETSECRET==\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    registry = ToolRegistry()
    registry.register(_ErrorReturningTool("parser", payload))

    result = await registry.execute("parser", {})

    assert "BEGIN RSA PRIVATE KEY" not in result
    assert "MIIEowIBAAKCAQEAy3SECRETSECRETSECRET" not in result
    assert "REDACTED" in result
    assert "Analyze the error above" in result
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_redacts_github_token_in_error_output() -> None:
    """MIT-147: a GitHub token exfiltration attempt via Error: string is scrubbed."""
    payload = (
        "Error: GitHub API rejected token "
        "ghp_0123456789abcdef0123456789abcdef0123 (403 Forbidden)"
    )
    registry = ToolRegistry()
    registry.register(_ErrorReturningTool("gh", payload))

    result = await registry.execute("gh", {})

    assert "ghp_0123456789abcdef0123456789abcdef0123" not in result
    assert "REDACTED" in result
    assert "Analyze the error above" in result
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_clean_error_passes_through_with_hint() -> None:
    """MIT-147: error strings with no secrets stay intact and still gain the HINT suffix.

    This is the regression guard for the previous test_execute_does_not_redact_error_output
    behavior — the hint is still there, the body is still readable; the only change is
    that secret-bearing error bodies now get scrubbed.
    """
    registry = ToolRegistry()
    registry.register(_ErrorReturningTool("boom", "Error: something bad happened"))

    result = await registry.execute("boom", {})

    assert result.startswith("Error: something bad happened")
    assert "Analyze the error above" in result
    # No REDACTED substitution for clean errors.
    assert "REDACTED" not in result


@pytest.mark.asyncio
async def test_execute_redacts_secret_in_exception_message() -> None:
    """MIT-147: secrets embedded in raised exceptions must also be scrubbed.

    A tool that raises `ValueError(f"could not parse {pem_content}")` would previously
    surface the PEM blob verbatim in the `Error executing {name}: {str(e)}` line.
    """
    pem_in_exc = (
        "could not parse: -----BEGIN OPENSSH PRIVATE KEY----- "
        "MIIEowIBAAKCAQEAy3 (truncated) -----END OPENSSH PRIVATE KEY-----"
    )
    registry = ToolRegistry()
    registry.register(_RaisingTool("parser", ValueError(pem_in_exc)))

    result = await registry.execute("parser", {})

    assert "BEGIN OPENSSH PRIVATE KEY" not in result
    assert "REDACTED" in result
    assert "Analyze the error above" in result
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_redacts_akia_in_exception_message() -> None:
    """MIT-147: AKIA keys in exception messages are scrubbed on the except branch."""
    registry = ToolRegistry()
    registry.register(
        _RaisingTool("aws", RuntimeError("connection failed for AKIAABCDEFGHIJKLMNOP"))
    )

    result = await registry.execute("aws", {})

    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "REDACTED" in result
    assert "Analyze the error above" in result
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_clean_exception_passes_through_with_hint() -> None:
    """Regression: clean exception messages surface intact with the HINT suffix."""
    registry = ToolRegistry()
    registry.register(_RaisingTool("boom", RuntimeError("disk full")))

    result = await registry.execute("boom", {})

    assert "Error executing boom" in result
    assert "disk full" in result
    assert "Analyze the error above" in result
    assert "REDACTED" not in result
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_preserves_error_prefix_when_whole_body_is_redacted() -> None:
    """MIT-147 (codex review): `AgentRunner._run_tool()` relies on
    `result.startswith("Error")` to classify tool calls as failed.

    Before the codex fix, when a tool returned an error string consisting ONLY
    of secret material (worst case), `redact_if_sensitive()` would replace the
    whole body with `[REDACTED — ...]` — which does NOT start with "Error".
    The runner would then mark the call as `ok` and skip `fail_on_tool_error`
    handling. This test pins the fix: the registry re-wraps a scrubbed error
    body in an `Error (...)` shell so the downstream detector keeps working.
    """
    # Pathological case: the entire error body is the secret. The redactor
    # replaces the whole string, leaving no "Error:" prefix in the raw match.
    secret_only = "AKIAABCDEFGHIJKLMNOP"
    registry = ToolRegistry()
    # The tool returns a pure-error string starting with "Error" (so the
    # registry classifies status=error), but the payload after that is just
    # the secret — redaction swallows everything downstream of the first match.
    registry.register(
        _ErrorReturningTool("pure_error", f"Error: {secret_only}")
    )

    result = await registry.execute("pure_error", {})

    # Secret must be gone.
    assert secret_only not in result
    # Redaction notice must be present.
    assert "REDACTED" in result
    # CRITICAL: the runner keys off ToolResult.is_error to detect failures.
    # If this regresses, `fail_on_tool_error` paths stop firing.
    assert result.is_error
    # Hint is still appended.
    assert "Analyze the error above" in result


@pytest.mark.asyncio
async def test_execute_preserves_error_prefix_when_exception_body_is_redacted() -> None:
    """Same downstream-contract guarantee on the exception branch."""
    secret_only_exc = RuntimeError("AKIAABCDEFGHIJKLMNOP")
    registry = ToolRegistry()
    registry.register(_RaisingTool("bad_exc", secret_only_exc))

    result = await registry.execute("bad_exc", {})

    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "REDACTED" in result
    assert result.is_error  # downstream failure detection contract
    assert "Analyze the error above" in result


class _AnyReturningTool(Tool):
    """Fake tool that returns an arbitrary non-string payload verbatim."""

    def __init__(self, name: str, payload: Any):
        self._name = name
        self._payload = payload

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return self._payload


@pytest.mark.asyncio
async def test_execute_passes_structured_payload_containing_secret_string_unchanged() -> None:
    """Document string-only redaction scope: nested structured payloads pass through.

    Current behavior is string-only redaction; `ToolRegistry.execute()` guards the
    scrubber with `isinstance(result, str)`, so dict/list results flow through
    untouched even when they embed secret-looking strings in nested fields. This
    is intentional today (image blocks from ReadFileTool are list[dict]), but it
    is a known scope limit: if a future tool returns user-facing text in a nested
    field, consider recursive scanning (tracked separately, unticketed).

    This test is a regression guard + scope-documentation test, not a fix.
    """
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAy3... (truncated)\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    # Nested dict (simulating a future tool that returns structured output)
    dict_payload: dict[str, Any] = {"content": pem, "meta": {"path": "/tmp/key.pem"}}
    registry = ToolRegistry()
    registry.register(_AnyReturningTool("structured_dict", dict_payload))

    result = await registry.execute("structured_dict", {})

    # Pass-through: the dict flows through untouched, secret string still present.
    assert result is dict_payload
    assert isinstance(result, dict)
    assert "BEGIN RSA PRIVATE KEY" in result["content"]
    assert "REDACTED" not in result["content"]

    # Same contract for a list of dicts with a nested secret string.
    list_payload: list[dict[str, Any]] = [
        {"type": "text", "text": "Authorization: token ghp_0123456789abcdef0123456789abcdef0123"},
    ]
    registry2 = ToolRegistry()
    registry2.register(_AnyReturningTool("structured_list", list_payload))

    result2 = await registry2.execute("structured_list", {})

    assert result2 is list_payload
    assert isinstance(result2, list)
    assert "ghp_0123456789abcdef0123456789abcdef0123" in result2[0]["text"]
