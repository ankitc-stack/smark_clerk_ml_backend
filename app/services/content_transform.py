from __future__ import annotations

import json
import re
from typing import Any

import requests

from app.config import settings
from app.ml.ollama_client import ollama_chat
from app.schemas import ActionObject, CommandAction, ToneValue
from app.services.prompt_library import load_system_prompt


class TransformError(ValueError):
    """Base typed error for transform failures."""


class TransformResolutionError(TransformError):
    """Raised when ActionObject target cannot be resolved to one paragraph."""


class TransformUnsupportedActionError(TransformError):
    """Raised when action is outside v1 transform safe set."""


class TransformValidationError(TransformError):
    """Raised when transformed text violates output safety/shape rules."""


_NUMBERING_PREFIX_RE = re.compile(r"^(\s*\d+\.\s+)(.*)$", flags=re.DOTALL)
_MARKDOWN_MARKERS_RE = re.compile(r"```|\*\*|(?:^|\s)#", flags=re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"^\s*\{[\s\S]*\}\s*$")
_AI_META_RE = re.compile(r"\bas an ai\b", flags=re.IGNORECASE)
# Matches echo-back prefixes like "Here is the transformed paragraph text only:"
_LLM_META_PREFIX_RE = re.compile(
    r"^(?:here\s+is\s+(?:the\s+)?(?:transformed|rewritten|expanded|shortened|revised|updated|"
    r"modified|improved|new|requested)\s+(?:paragraph|text|content|version|output)"
    r"(?:\s+(?:text\s+)?only)?[:\-–]?\s*\n+|"
    r"(?:transformed|rewritten|expanded|shortened|revised|updated|new|improved)\s+"
    r"(?:paragraph|text|content)[:\-–]\s*\n*)",
    flags=re.IGNORECASE,
)


def _supported_transform_prompt(action_object: ActionObject) -> str:
    action = action_object.action
    if action == CommandAction.REWRITE_CONTENT:
        return "rewrite_v2"
    if action == CommandAction.EXPAND_CONTENT:
        return "expand_v2"
    if action == CommandAction.SHORTEN_CONTENT:
        return "shorten_v2"
    if action == CommandAction.FIX_GRAMMAR:
        return "fix_grammar_v1"
    if action == CommandAction.CHANGE_TONE:
        if action_object.params.tone == ToneValue.formal:
            return "tone_formal_v2"
        if action_object.params.tone == ToneValue.concise:
            return "tone_concise_v2"
        raise TransformUnsupportedActionError(
            "unsupported_action: CHANGE_TONE supports only formal/concise in v2"
        )
    raise TransformUnsupportedActionError(f"unsupported_action: {action.value}")


def _paragraph_items(section: dict | None) -> list[dict]:
    if not isinstance(section, dict):
        return []
    return [
        item
        for item in (((section.get("content") or {}).get("items")) or [])
        if isinstance(item, dict)
    ]


def _resolve_source_paragraph_text(action_object: ActionObject, doc: dict) -> str:
    section_id = action_object.target.section_id
    para_id = action_object.target.para_id
    if not section_id or not para_id:
        raise TransformResolutionError("missing target section_id/para_id")

    section = None
    for sec in doc.get("sections", []) or []:
        if isinstance(sec, dict) and sec.get("id") == section_id:
            section = sec
            break
    if section is None or section.get("type") != "numbered_paragraphs":
        raise TransformResolutionError("target section is missing or not paragraph-capable")

    matches = [item for item in _paragraph_items(section) if str(item.get("id") or "") == para_id]
    if len(matches) != 1:
        raise TransformResolutionError("target paragraph did not resolve uniquely")

    return str(matches[0].get("text") or "")


def _normalize_whitespace(text: str) -> str:
    # Keep output as one plain paragraph line to avoid markdown/list artifacts.
    return re.sub(r"\s+", " ", text or "").strip()


def _shorten_stub(text: str) -> str:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return normalized
    if len(normalized) <= 48:
        return normalized[:36].rstrip(" ,;:") + "..." if len(normalized) > 40 else normalized

    target_len = max(40, int(len(normalized) * 0.65))
    clipped = normalized[:target_len].rstrip()
    if len(clipped) < len(normalized):
        clipped = clipped.rsplit(" ", 1)[0].rstrip(" ,;:")
        return (clipped or normalized[:target_len]).rstrip(" ,;:") + "..."
    return clipped


def _stub_transform(action_object: ActionObject, source_text: str) -> str:
    action = action_object.action
    tone = action_object.params.tone
    text = _normalize_whitespace(source_text)

    if action == CommandAction.EXPAND_CONTENT:
        return f"{text} This is issued for clarity and further necessary action."
    if action == CommandAction.SHORTEN_CONTENT:
        return _shorten_stub(text)
    if action == CommandAction.REWRITE_CONTENT:
        return text
    if action == CommandAction.CHANGE_TONE and tone == ToneValue.formal:
        if text.lower().startswith("it is submitted that"):
            return text
        return f"It is submitted that {text[0].lower() + text[1:] if text else text}"
    if action == CommandAction.CHANGE_TONE and tone == ToneValue.concise:
        return _shorten_stub(text)
    if action == CommandAction.FIX_GRAMMAR:
        # Stub: return text as-is (LLM handles the actual correction)
        return text

    raise TransformUnsupportedActionError(f"unsupported_action: {action.value}")


def prepare_input_text(source_text: str, preserve_numbering: bool) -> tuple[str, dict[str, Any]]:
    """
    Prepare paragraph text for transform.

    Why this split exists:
    - Keeps numbering stable by removing the prefix before generation.
    - Gives postprocess_text enough metadata to reattach original prefix exactly.
    """
    working = source_text or ""
    prefix = None
    if preserve_numbering:
        match = _NUMBERING_PREFIX_RE.match(working)
        if match:
            prefix = match.group(1)
            working = match.group(2)

    return working.strip(), {"numbering_prefix": prefix}


async def _repair_transform_output(raw_text: str, source_text: str) -> str:
    """
    Single repair pass for malformed LLM text output.

    This mirrors the JSON repair strategy: one deterministic retry only.
    """
    system = "Return only plain paragraph text. No markdown, no JSON, no labels, no commentary."
    user = (
        "Rewrite the bad output as one plain paragraph.\n\n"
        f"Source paragraph:\n{source_text}\n\n"
        f"Bad output:\n{raw_text}\n\n"
        "Return only the fixed paragraph text."
    )
    raw = await ollama_chat(system, user)
    return _LLM_META_PREFIX_RE.sub("", (raw or "").strip()).strip()


async def call_llm_transform(
    action_object: ActionObject,
    source_text: str,
    prompt_version: str,
) -> tuple[str, dict[str, Any]]:
    """
    Run transform using LLM when available, otherwise deterministic stub.
    """
    if settings.LLM_PROVIDER.lower() != "ollama" or not settings.COMMAND_TRANSFORM_USE_LLM:
        return _stub_transform(action_object, source_text), {
            "transform_source": "stub",
            "transform_prompt_version": prompt_version,
            "transform_repair_applied": False,
        }

    try:
        tags_url = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/tags"
        requests.get(tags_url, timeout=max(0.1, float(settings.COMMAND_TRANSFORM_HEALTH_TIMEOUT_S)))
    except Exception:
        return _stub_transform(action_object, source_text), {
            "transform_source": "stub",
            "transform_prompt_version": prompt_version,
            "transform_repair_applied": False,
        }

    system_prompt = load_system_prompt(prompt_version)
    user_payload = {
        "action": action_object.action.value,
        "tone": action_object.params.tone.value if action_object.params.tone else None,
        "text": source_text,
        "instructions": "Return transformed paragraph text only.",
    }
    raw = await ollama_chat(system_prompt, json.dumps(user_payload, ensure_ascii=False))
    candidate = _LLM_META_PREFIX_RE.sub("", (raw or "").strip()).strip()

    try:
        validate_output_text(candidate, action_object.action, source_text)
        return candidate, {
            "transform_source": "llm",
            "transform_prompt_version": prompt_version,
            "transform_repair_applied": False,
        }
    except TransformValidationError:
        repaired = await _repair_transform_output(candidate, source_text)
        try:
            validate_output_text(repaired, action_object.action, source_text)
            return repaired, {
                "transform_source": "llm",
                "transform_prompt_version": prompt_version,
                "transform_repair_applied": True,
            }
        except TransformValidationError:
            # LLM output and repair both fail validation — fall back to stub.
            return _stub_transform(action_object, source_text), {
                "transform_source": "stub",
                "transform_prompt_version": prompt_version,
                "transform_repair_applied": False,
            }


def postprocess_text(transformed_text: str, prep_meta: dict[str, Any]) -> str:
    """
    Final cleanup after transform.

    Why this step exists:
    - Keeps spacing deterministic.
    - Reattaches numbering prefix exactly when preserve_numbering is enabled.
    """
    normalized = _normalize_whitespace(transformed_text)
    prefix = prep_meta.get("numbering_prefix")
    if prefix:
        return f"{prefix}{normalized}"
    return normalized


def validate_output_text(text: str, action: CommandAction, source_text: str) -> None:
    """
    Hard safety checks for LLM/stub transform output.
    """
    candidate = (text or "").strip()
    if not candidate:
        raise TransformValidationError("transform output is empty")

    if _JSON_OBJECT_RE.match(candidate):
        raise TransformValidationError("transform output must not be a JSON object")

    if _MARKDOWN_MARKERS_RE.search(candidate):
        raise TransformValidationError("transform output must not contain markdown markers")

    if _AI_META_RE.search(candidate):
        raise TransformValidationError("transform output contains model meta language")

    src_len = len(_normalize_whitespace(source_text))
    out_len = len(candidate)
    # FIX_GRAMMAR: no length constraints (corrected text may be same or slightly different length)
    if src_len >= 20 and action != CommandAction.FIX_GRAMMAR:
        tolerance = max(3, int(src_len * 0.03))
        if action == CommandAction.SHORTEN_CONTENT and out_len > max(1, src_len - tolerance):
            raise TransformValidationError("shorten action did not reduce length")
        if action == CommandAction.EXPAND_CONTENT and out_len < (src_len + tolerance):
            raise TransformValidationError("expand action did not increase length")


async def apply_transform(
    action_object: ActionObject,
    doc: dict,
    context: dict | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Apply v1 content transform and return plain text plus transform metadata.

    Contract:
    - Returns one paragraph string only.
    - Raises typed TransformError subclasses on any deterministic/validation failure.
    """
    _ = context  # Reserved for future deterministic cursor-aware transforms.
    prompt_version = _supported_transform_prompt(action_object)

    source_text = _resolve_source_paragraph_text(action_object, doc)
    prepared_text, prep_meta = prepare_input_text(
        source_text=source_text,
        preserve_numbering=bool(action_object.params.preserve_numbering),
    )

    transformed_text, transform_meta = await call_llm_transform(
        action_object=action_object,
        source_text=prepared_text,
        prompt_version=prompt_version,
    )
    validate_output_text(transformed_text, action_object.action, prepared_text)
    final_text = postprocess_text(transformed_text, prep_meta)

    if not final_text.strip():
        raise TransformValidationError("final transform output is empty after postprocess")
    return final_text, transform_meta
