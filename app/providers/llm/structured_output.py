"""Structured-output request modes for OpenAI-compatible LLM gateways.

The model-role configuration in Zelig distinguishes between a provider's
request shape and the business-level JSON validation. Keep the same
separation here so a model such as Gemma can use prompt-only JSON output
without making the pipeline depend on provider-specific SDKs.
"""

from __future__ import annotations

from typing import Any, Literal

StructuredOutputMode = Literal[
    "json_schema",
    "compat_json_schema",
    "json_object",
    "prompt_only",
]

SUPPORTED_STRUCTURED_OUTPUT_MODES = frozenset(
    {"json_schema", "compat_json_schema", "json_object", "prompt_only"}
)

# Gemini/Gemma-compatible gateways commonly map OpenAI JSON Schema requests
# onto the legacy generation_config.response_schema proto. These JSON Schema
# keywords are not representable in that proto subset.
_INCOMPATIBLE_KEYWORDS = frozenset(
    {
        "uniqueItems",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
        "pattern",
        "patternProperties",
        "propertyNames",
        "contains",
    }
)
_SCHEMA_MAP_KEYS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SCHEMA_LIST_KEYS = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})
_SCHEMA_NODE_KEYS = frozenset({"items", "additionalProperties", "not", "contains"})
_LITERAL_KEYS = frozenset(
    {"enum", "required", "propertyOrdering", "const", "default", "example", "examples"}
)
_SCALAR_VALUE_TYPES = ("string", "number", "boolean", "object")


def normalize_structured_output_mode(
    value: str | None,
    *,
    default: StructuredOutputMode = "prompt_only",
) -> StructuredOutputMode:
    """Validate and normalize one configured mode.

    An empty value means the caller's default. The default is deliberately
    ``prompt_only`` because it is the least provider-specific mode and is the
    mode used by the known-good Gemma response configuration in Zelig.
    """

    normalized = (value or "").strip().lower() or default
    if normalized not in SUPPORTED_STRUCTURED_OUTPUT_MODES:
        supported = ", ".join(sorted(SUPPORTED_STRUCTURED_OUTPUT_MODES))
        raise ValueError(
            f"unsupported structured output mode {normalized!r}; "
            f"expected one of: {supported}"
        )
    return normalized  # type: ignore[return-value]


def to_provider_compatible_schema(value: Any) -> Any:
    """Rewrite JSON Schema into the legacy Gemini-compatible subset.

    This mirrors the safe conversion used by Zelig: schema maps are traversed
    as maps, nullable type arrays become ``nullable``, and unsupported
    validation keywords are removed. Pydantic validation remains the final
    authority after the model response is parsed.
    """

    if not isinstance(value, dict):
        return value

    target: dict[str, Any] = {}
    nullable = False

    for key, child in value.items():
        if key in _INCOMPATIBLE_KEYWORDS:
            continue

        if key == "type" and isinstance(child, list):
            concrete = [item for item in child if item != "null"]
            nullable = len(concrete) != len(child)
            if len(concrete) == 1:
                target["type"] = concrete[0]
            elif concrete:
                target["anyOf"] = [{"type": item} for item in concrete]
            continue

        if key == "enum" and isinstance(child, list):
            concrete = [item for item in child if item is not None]
            nullable = nullable or len(concrete) != len(child)
            target[key] = concrete
            continue

        if key in _LITERAL_KEYS:
            target[key] = child
        elif key in _SCHEMA_MAP_KEYS and isinstance(child, dict):
            target[key] = {
                name: to_provider_compatible_schema(schema)
                for name, schema in child.items()
            }
        elif key in _SCHEMA_LIST_KEYS and isinstance(child, list):
            target[key] = [to_provider_compatible_schema(schema) for schema in child]
        elif key in _SCHEMA_NODE_KEYS:
            target[key] = (
                child
                if isinstance(child, bool)
                else to_provider_compatible_schema(child)
            )
        else:
            target[key] = child

    if nullable:
        target["nullable"] = True

    if not any(key in target for key in ("type", "anyOf", "$ref")):
        if "properties" in target or "additionalProperties" in target:
            target["type"] = "object"
        elif "items" in target:
            target["type"] = "array"
        else:
            target["anyOf"] = [
                *({"type": item} for item in _SCALAR_VALUE_TYPES),
                {
                    "type": "array",
                    "items": {
                        "anyOf": [{"type": item} for item in _SCALAR_VALUE_TYPES]
                    },
                },
            ]

    return target


def response_format_for_mode(
    mode: StructuredOutputMode,
    *,
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the OpenAI-compatible ``response_format`` body fragment."""

    if mode == "prompt_only":
        return None
    if mode == "json_object":
        return {"type": "json_object"}

    schema_body = schema if mode == "json_schema" else to_provider_compatible_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": schema_body,
        },
    }


def schema_instruction(schema: dict[str, Any]) -> str:
    """Return the standalone user message used by prompt/json-object modes."""

    import json

    return (
        "Return ONLY one valid JSON object matching this JSON Schema. "
        "Do not use Markdown fences or commentary.\n"
        + json.dumps(schema, ensure_ascii=False)
    )
