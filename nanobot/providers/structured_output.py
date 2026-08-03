"""Request-scoped JSON Schema output for internal LLM calls.

Use :func:`generate_structured` for single-turn internal decisions such as a
reasoning classifier, Work plan, connector approval preview, or mini-app spec.
The schema is immutable after construction and is never stored on the provider.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import FormatChecker, ValidationError, validators
from jsonschema.exceptions import SchemaError

from nanobot.providers.base import LLMProvider, LLMResponse

JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None

_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StructuredOutputError(ValueError):
    """Base class for structured-generation failures."""


class StructuredOutputSchemaError(StructuredOutputError):
    """The supplied JSON Schema or schema metadata is invalid."""


class MalformedStructuredOutputError(StructuredOutputError):
    """The model returned empty content or content that is not valid JSON."""


class StructuredOutputValidationError(StructuredOutputError):
    """The model returned JSON that does not satisfy the requested schema."""

    def __init__(self, errors: tuple[str, ...]):
        self.errors = errors
        super().__init__("Structured output failed schema validation: " + "; ".join(errors))


class StructuredOutputProviderError(StructuredOutputError):
    """The provider failed before valid structured output was returned."""

    def __init__(self, response: LLMResponse):
        self.response = response
        detail = (response.content or "unknown provider error").strip()
        super().__init__(f"Structured output provider request failed: {detail}")


class UnsupportedStructuredOutputProviderError(StructuredOutputError):
    """The selected provider cannot enforce request-scoped JSON Schema output."""


class UnexpectedStructuredToolCallError(StructuredOutputError):
    """A schema-only generation unexpectedly returned a tool call."""


@dataclass(frozen=True, slots=True, init=False)
class JSONSchemaOutput:
    """Immutable description of one JSON Schema-constrained response.

    The schema is serialized at construction time. Every request receives a
    newly decoded copy, preventing caller or provider mutations from crossing
    concurrent requests.
    """

    name: str
    description: str | None
    strict: bool
    _schema_json: str = field(repr=False)

    def __init__(
        self,
        *,
        name: str,
        schema: Mapping[str, Any],
        description: str | None = None,
        strict: bool = True,
    ) -> None:
        if not _SCHEMA_NAME.fullmatch(name):
            raise StructuredOutputSchemaError(
                "Schema name must be 1-64 letters, numbers, underscores, or hyphens"
            )
        try:
            schema_json = json.dumps(schema, ensure_ascii=False, allow_nan=False)
            normalized = json.loads(schema_json)
        except (TypeError, ValueError) as exc:
            raise StructuredOutputSchemaError(f"JSON Schema is not JSON-serializable: {exc}") from exc
        if not isinstance(normalized, dict):
            raise StructuredOutputSchemaError("JSON Schema must be an object")
        try:
            validators.validator_for(normalized).check_schema(normalized)
        except SchemaError as exc:
            raise StructuredOutputSchemaError(f"Invalid JSON Schema: {exc.message}") from exc

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "strict", strict)
        object.__setattr__(self, "_schema_json", schema_json)

    @property
    def schema(self) -> dict[str, Any]:
        """Return an independent mutable copy for one provider request."""
        return json.loads(self._schema_json)

    def chat_completions_format(self) -> dict[str, Any]:
        """Build the standard OpenAI-compatible ``response_format`` value."""
        definition: dict[str, Any] = {
            "name": self.name,
            "schema": self.schema,
            "strict": self.strict,
        }
        if self.description:
            definition["description"] = self.description
        return {"type": "json_schema", "json_schema": definition}

    def responses_text_format(self) -> dict[str, Any]:
        """Build the equivalent OpenAI Responses API ``text.format`` value."""
        output = self.chat_completions_format()["json_schema"]
        return {"type": "json_schema", **output}

    def validate(self, value: JSONValue) -> None:
        """Raise :class:`StructuredOutputValidationError` for schema violations."""
        schema = self.schema
        validator_cls = validators.validator_for(schema)
        validator = validator_cls(schema, format_checker=FormatChecker())
        failures = sorted(validator.iter_errors(value), key=_validation_error_sort_key)
        if failures:
            raise StructuredOutputValidationError(tuple(_format_validation_error(e) for e in failures))


@dataclass(frozen=True, slots=True)
class LLMRequestOptions:
    """Optional behavior that applies to exactly one provider request."""

    structured_output: JSONSchemaOutput | None = None


@dataclass(frozen=True, slots=True)
class StructuredOutputResult:
    """Validated JSON plus the provider response and usage metadata."""

    value: JSONValue
    response: LLMResponse


async def generate_structured(
    provider: LLMProvider,
    *,
    messages: list[dict[str, Any]],
    output: JSONSchemaOutput,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    retry_mode: str = "standard",
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
) -> StructuredOutputResult:
    """Generate and validate one internal JSON response.

    This deliberately performs a schema-only call without tools. Product flows
    should construct their own schema and consume ``result.value`` rather than
    parsing prose or mutating a provider's configured ``extra_body``.
    """
    if not provider.supports_structured_output:
        raise UnsupportedStructuredOutputProviderError(
            f"{type(provider).__name__} does not support request-scoped structured output"
        )

    kwargs: dict[str, Any] = {
        "messages": messages,
        "tools": None,
        "model": model,
        "retry_mode": retry_mode,
        "on_retry_wait": on_retry_wait,
        "request_options": LLMRequestOptions(structured_output=output),
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    response = await provider.chat_with_retry(**kwargs)
    if response.finish_reason == "error":
        raise StructuredOutputProviderError(response)
    if response.tool_calls:
        raise UnexpectedStructuredToolCallError(
            "Structured output request unexpectedly returned tool calls"
        )
    content = response.content
    if not content or not content.strip():
        raise MalformedStructuredOutputError("Structured output response was empty")
    try:
        value: JSONValue = json.loads(content)
    except json.JSONDecodeError as exc:
        raise MalformedStructuredOutputError(
            f"Structured output was not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc

    output.validate(value)
    return StructuredOutputResult(value=value, response=response)


def _validation_error_sort_key(error: ValidationError) -> tuple[str, str]:
    return (_json_path(error), error.message)


def _format_validation_error(error: ValidationError) -> str:
    return f"{_json_path(error)}: {error.message}"


def _json_path(error: ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path
