# Internal structured output

Use request-scoped JSON Schema output when an internal LLM decision must be
machine-readable. Do not set `providers.*.extra_body.guided_json` for these
calls: that configuration applies to every request made by the provider.

```python
from nanobot.providers.structured_output import JSONSchemaOutput, generate_structured

decision = JSONSchemaOutput(
    name="reasoning_decision",
    schema={
        "type": "object",
        "properties": {
            "profile": {"enum": ["fast", "think", "think-code"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["profile", "confidence"],
        "additionalProperties": False,
    },
)

result = await generate_structured(
    provider,
    messages=[{"role": "user", "content": classifier_prompt}],
    output=decision,
    max_tokens=128,
    temperature=0,
)
profile = result.value["profile"]
```

The helper uses the OpenAI-compatible `response_format: json_schema` request
shape, parses the returned JSON without repair, and validates it locally. It
raises distinct errors for invalid schemas, malformed JSON, schema violations,
unsupported providers, provider failures, and unexpected tool calls.

Create separate schemas for each contract. Expected near-term consumers are:

- reasoning classifier decisions
- Work plans and task decomposition
- connector mutation approval previews
- generated mini-app specifications

`JSONSchemaOutput` snapshots its schema during construction. A per-run schema
is never saved on the provider and cannot constrain a later chat request.
Configured legacy `extra_body.guided_json` remains supported for deployments
that intentionally constrain every provider call.
