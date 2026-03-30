"""
app/ml/json_repair.py

Purpose:
- Provide a single reliable "call -> parse JSON -> repair once" flow.
- This is the standard entrypoint all ML endpoints should use.

Why:
- LLM output must be machine-readable.
- We want high reliability without infinite retries.
- Exactly ONE repair attempt keeps latency predictable.
"""

from __future__ import annotations
from typing import Optional, Union

from app.ml.ollama_client import ollama_chat
from app.ml.json_guard import JsonType, parse_json_strict


def _default_from_schema(schema: object) -> object:
    """
    Build a deep default value from schema exemplar JSON.
    """
    if isinstance(schema, dict):
        return {k: _default_from_schema(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_default_from_schema(v) for v in schema]
    return schema


def _coerce_scalar(value: object, default: object) -> object:
    """
    Best-effort scalar coercion using schema exemplar type.
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"true", "1", "yes"}:
                return True
            if low in {"false", "0", "no"}:
                return False
        return default

    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value.strip()))
            except ValueError:
                return default
        return default

    if isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return default
        return default

    if isinstance(default, str):
        if isinstance(value, str):
            return value
        if value is None:
            return default
        return str(value)

    return value if value is not None else default


def _apply_schema_shape(value: object, schema: object) -> object:
    """
    Normalize a value to the structure described by schema exemplar JSON.
    """
    if isinstance(schema, dict):
        src = value if isinstance(value, dict) else {}
        out: dict[str, object] = {}

        for key, schema_value in schema.items():
            if key in src:
                out[key] = _apply_schema_shape(src[key], schema_value)
            else:
                out[key] = _default_from_schema(schema_value)

        # Preserve extras so callers do not lose useful data.
        for key, src_val in src.items():
            if key not in out:
                out[key] = src_val
        return out

    if isinstance(schema, list):
        if not isinstance(value, list):
            return _default_from_schema(schema)
        if not schema:
            return value

        item_schema = schema[0]
        return [_apply_schema_shape(item, item_schema) for item in value]

    return _coerce_scalar(value, schema)


def _normalize_with_schema(parsed: JsonType, schema_obj: Optional[JsonType]) -> JsonType:
    """
    If schema JSON is available, fill missing keys/type-mismatches from schema.
    """
    if schema_obj is None:
        return parsed

    normalized = _apply_schema_shape(parsed, schema_obj)
    if isinstance(normalized, (dict, list)):
        return normalized
    return parsed


async def repair_once(bad_output: str, schema_hint: str) -> Optional[JsonType]:
    """
    Ask the model to convert its previous output into valid JSON.

    Inputs:
    - bad_output: the raw invalid response from the model
    - schema_hint: a short schema description the model must follow

    Returns:
    - parsed JSON dict/list if successful
    - None if still invalid
    """
    system = "Return STRICT valid JSON only. No markdown. No extra text."
    user = (
        "Fix the following output into valid JSON ONLY.\n\n"
        f"Schema hint:\n{schema_hint}\n\n"
        "Bad output:\n"
        f"{bad_output}\n\n"
        "Return ONLY valid JSON."
    )

    fixed = await ollama_chat(system, user)
    return parse_json_strict(fixed)


async def call_and_parse_json(system: str, user: str, schema_hint: str) -> Union[dict, list]:
    """
    Main helper used by all ML operations.

    What happens:
    1) Call Ollama
    2) Try to parse JSON from the response
    3) If parsing fails -> do ONE repair call
    4) If schema_hint is valid JSON, shape parsed output to it and
       use schema defaults as a final fallback
    5) If no valid schema_hint and parsing still fails -> raise ValueError

    IMPORTANT:
    - We do not keep retrying; that can lock up requests.
    """
    parsed, _meta = await call_and_parse_json_with_meta(system, user, schema_hint)
    return parsed


async def call_and_parse_json_with_meta(
    system: str,
    user: str,
    schema_hint: str,
) -> tuple[Union[dict, list], dict]:
    """
    Same behavior as call_and_parse_json, with basic observability metadata.

    meta fields:
    - repair_applied: True when one-shot repair path produced parseable JSON
    - fallback_applied: True when schema-based default object was returned
    """
    schema_obj = parse_json_strict(schema_hint)

    raw = await ollama_chat(system, user)
    parsed = parse_json_strict(raw)
    if parsed is not None:
        return _normalize_with_schema(parsed, schema_obj), {
            "repair_applied": False,
            "fallback_applied": False,
        }

    # One repair attempt
    repaired = await repair_once(raw, schema_hint)
    if repaired is not None:
        return _normalize_with_schema(repaired, schema_obj), {
            "repair_applied": True,
            "fallback_applied": False,
        }

    # Final deterministic fallback if schema hint itself is valid JSON.
    if schema_obj is not None:
        fallback = _default_from_schema(schema_obj)
        if isinstance(fallback, (dict, list)):
            return fallback, {
                "repair_applied": False,
                "fallback_applied": True,
            }

    raise ValueError("LLM did not return valid JSON after one repair attempt.")
