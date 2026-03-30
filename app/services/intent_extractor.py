from __future__ import annotations

import json
from typing import Any
import requests
import re
from dataclasses import dataclass

from app.config import settings
from app.ml.json_repair import call_and_parse_json_with_meta
from app.schemas import (
    ActionClarification,
    ActionObject,
    ActionParams,
    ActionTarget,
    ClarificationOption,
    CommandAction,
    CommandContext,
    CommandScope,
    ToneValue,
)
from app.services.command_contract import (
    IntentParseError,
    resolve_cursor_paragraph,
    resolve_action_object_from_request,
)
from app.services.prompt_library import load_few_shot_examples, load_system_prompt


_INTENT_SCHEMA_HINT = json.dumps(
    {
        "action": "REWRITE_CONTENT",
        "scope": "PARAGRAPH",
        "target": {
            "section_id": None,
            "para_id": None,
            "para_index": None,
        },
        "params": {
            "tone": None,
            "preserve_numbering": True,
            "preserve_style": True,
            "style_params": None,
        },
        "content": None,
        "confidence": 0.5,
        "needs_clarification": False,
        "clarification": None,
    }
)


INTENT_PROMPT_VERSION = "intent_extraction_v1"


@dataclass
class IntentExtractionResult:
    action_object: ActionObject
    intent_source: str
    repair_applied: bool
    prompt_version: str


def _rule_based_fallback(prompt: str, context: CommandContext, structured: dict) -> ActionObject:
    # Rule-based fallback keeps contract behavior stable when LLM is unavailable or malformed.
    try:
        return resolve_action_object_from_request(prompt, context, structured)
    except Exception as ex:
        raise IntentParseError(str(ex))


def _contains_deictic_reference(prompt: str) -> bool:
    return bool(re.search(r"\b(this|here)\b", (prompt or "").lower()))


def _section_by_id(structured: dict, section_id: str | None) -> dict | None:
    if not section_id:
        return None
    for sec in structured.get("sections", []) or []:
        if isinstance(sec, dict) and sec.get("id") == section_id:
            return sec
    return None


def _paragraphs_from_section(section: dict | None) -> list[dict]:
    if not isinstance(section, dict):
        return []
    return [
        it
        for it in (((section.get("content") or {}).get("items")) or [])
        if isinstance(it, dict)
    ]


def _clarification_from_section(section: dict | None, question: str = "Which paragraph?") -> ActionClarification:
    def _preview_text(item: dict, max_chars: int = 60) -> str:
        raw = " ".join(str(item.get("text") or "").split())
        if not raw:
            return ""
        trimmed = raw[: max_chars - 3].rstrip() + "..." if len(raw) > max_chars else raw
        return trimmed.replace('"', "'")

    options: list[ClarificationOption] = []
    for idx, item in enumerate(_paragraphs_from_section(section), start=1):
        para_id = str(item.get("id") or "").strip()
        if para_id:
            preview = _preview_text(item)
            label = f'Para {idx}: "{preview}"' if preview else f"Para {idx}"
            options.append(ClarificationOption(label=label, token=para_id))
    return ActionClarification(question=question, options=options)


def _cursor_resolves_para_deterministically(context: CommandContext, structured: dict) -> tuple[str | None, int | None]:
    return resolve_cursor_paragraph(
        structured=structured,
        section_id=context.current_section_id,
        cursor_position=context.cursor_position,
    )


def _section_catalog_for_prompt(structured: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    para_counter = 0  # for doc-engine blueprint sections where each paragraph is a separate section
    for sec in structured.get("sections", []) or []:
        if not isinstance(sec, dict):
            continue
        row: dict[str, Any] = {
            "id": sec.get("id"),
            "type": sec.get("type"),
        }
        if sec.get("type") == "numbered_paragraphs":
            # Legacy ML pipeline format: paragraphs are items inside one section
            items = ((sec.get("content") or {}).get("items") or [])
            row["paragraphs"] = [
                {
                    "id": it.get("id"),
                    "index_1based": idx,
                }
                for idx, it in enumerate(items, start=1)
                if isinstance(it, dict)
            ]
        elif sec.get("type") == "paragraph":
            # Doc-engine blueprint format: each paragraph is its own section.
            # Expose a sequential 1-based index and a human label so the LLM can
            # resolve "paragraph 1" → section id directly via target.section_id.
            para_counter += 1
            row["index_1based"] = para_counter
            row["label"] = f"Paragraph {para_counter}"
            row["para_id"] = f"p{para_counter}"   # virtual para_id for LLM compatibility
        out.append(row)
    return out


def _normalize_payload_shape(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)

    # Normalize enum-like text so model output can be parsed by strict Pydantic enums.
    action = out.get("action")
    if isinstance(action, str):
        out["action"] = action.strip().upper()

    scope = out.get("scope")
    if isinstance(scope, str):
        out["scope"] = scope.strip().upper()

    params = out.get("params")
    if isinstance(params, dict):
        tone = params.get("tone")
        if isinstance(tone, str):
            params["tone"] = tone.strip().lower()
        out["params"] = params

    return out


def _merge_with_fallback(
    llm_obj: ActionObject,
    fallback_obj: ActionObject,
    prompt: str,
    context: CommandContext,
    structured: dict,
) -> ActionObject:
    # Merge policy is intentionally conservative:
    # - no silent target injection for ambiguous/deictic commands
    # - only merge when scope is unambiguous.
    merged = llm_obj.model_dump()

    # When LLM returns MOVE_SECTION but the rule-based resolver detected SET_FORMAT
    # (alignment change), the prompt contains a directional word ("right"/"left") that
    # the rule-based path correctly interprets as alignment, not physical movement.
    # "move receiver block to right" means align-right, NOT physically relocate.
    _llm_action = merged.get("action", "")
    _fb_action = fallback_obj.action.value if hasattr(fallback_obj.action, "value") else str(fallback_obj.action)
    if (_llm_action == CommandAction.MOVE_SECTION.value and
            _fb_action == CommandAction.SET_FORMAT.value):
        return fallback_obj

    # For SET_FORMAT on named sections (signee_block, receiver_block, subject, date, etc.),
    # always trust the fallback's section resolution.  The LLM reliably detects alignment
    # intent but often maps it to the wrong section UUID (e.g. first paragraph instead of
    # signee_block).  The rule-based resolver uses keyword matching and is always correct
    # for these named sections.
    _NON_PARA_SECTION_TYPES = {
        "signee_block", "receiver_block", "subject", "date", "reference_number",
        "letterhead", "salutation", "security_classification", "annexure_block",
        "enclosure", "copy_to", "distribution_list", "endorsement", "noo", "precedence",
    }
    if _llm_action == CommandAction.SET_FORMAT.value:
        _fb_sec_id = fallback_obj.target.section_id if fallback_obj.target else None
        _fb_sec = _section_by_id(structured, _fb_sec_id) if _fb_sec_id else None
        if _fb_sec and _fb_sec.get("type") in _NON_PARA_SECTION_TYPES:
            merged["target"]["section_id"] = _fb_sec_id

    section_id = merged["target"].get("section_id")
    para_id = merged["target"].get("para_id")
    para_index = merged["target"].get("para_index")
    section_level_actions = {
        CommandAction.INSERT_SECTION.value,
        CommandAction.DELETE_SECTION.value,
        CommandAction.MOVE_SECTION.value,
    }
    if merged.get("action") in section_level_actions:
        if not section_id:
            merged["target"]["section_id"] = fallback_obj.target.section_id
        if merged["action"] == CommandAction.MOVE_SECTION.value:
            # Always use the fallback's para_id for MOVE_SECTION.
            # The LLM often sets para_id to the source paragraph number, but
            # the action contract requires para_id to hold the anchor destination
            # UUID (the section to move near). The rule-based fallback correctly
            # extracts the anchor from "before/after paragraph N" in the prompt.
            merged["target"]["para_id"] = fallback_obj.target.para_id
            if merged["target"].get("para_index") is None:
                merged["target"]["para_index"] = fallback_obj.target.para_index
        merged["scope"] = CommandScope.SECTION.value
        return ActionObject.model_validate(merged)

    # For non-paragraph sections (subject, date, signee_block, etc.) para_id is
    # intentionally None — the section itself is the target. Only numbered_paragraphs
    # sections require a para_id to identify which paragraph to act on.
    _target_sec = _section_by_id(structured, section_id) if section_id else None
    _para_required = not _target_sec or _target_sec.get("type") == "numbered_paragraphs"
    target_missing = not section_id or (_para_required and not para_id)

    if target_missing and _contains_deictic_reference(prompt):
        resolved_para_id, resolved_para_index = _cursor_resolves_para_deterministically(context, structured)
        if not resolved_para_id:
            return ActionObject(
                action=llm_obj.action,
                scope=CommandScope.PARAGRAPH,
                target=ActionTarget(section_id=context.current_section_id, para_id=None, para_index=None),
                params=llm_obj.params,
                content=llm_obj.content,
                confidence=min(0.55, float(llm_obj.confidence)),
                needs_clarification=True,
                clarification=_clarification_from_section(
                    _section_by_id(structured, context.current_section_id),
                    question="Which paragraph does this refer to?",
                ),
            )
        merged["target"]["section_id"] = context.current_section_id
        merged["target"]["para_id"] = resolved_para_id
        merged["target"]["para_index"] = resolved_para_index

    elif target_missing:
        # Only merge defaults when command scope is unambiguous:
        # current_section_id exists AND model supplied explicit paragraph index.
        if context.current_section_id and para_index is not None:
            section = _section_by_id(structured, context.current_section_id)
            items = _paragraphs_from_section(section)
            idx = int(para_index)
            if section and 0 <= idx < len(items):
                resolved_para_id = str(items[idx].get("id") or "").strip() or None
                if resolved_para_id:
                    merged["target"]["section_id"] = context.current_section_id
                    merged["target"]["para_id"] = resolved_para_id
                    merged["target"]["para_index"] = idx
                else:
                    return ActionObject(
                        action=llm_obj.action,
                        scope=CommandScope.PARAGRAPH,
                        target=ActionTarget(section_id=context.current_section_id, para_id=None, para_index=None),
                        params=llm_obj.params,
                        content=llm_obj.content,
                        confidence=min(0.55, float(llm_obj.confidence)),
                        needs_clarification=True,
                        clarification=_clarification_from_section(section),
                    )
            else:
                return ActionObject(
                    action=llm_obj.action,
                    scope=CommandScope.PARAGRAPH,
                    target=ActionTarget(section_id=context.current_section_id, para_id=None, para_index=None),
                    params=llm_obj.params,
                    content=llm_obj.content,
                    confidence=min(0.55, float(llm_obj.confidence)),
                    needs_clarification=True,
                    clarification=_clarification_from_section(section),
                )
        else:
            section = _section_by_id(structured, context.current_section_id)
            return ActionObject(
                action=llm_obj.action,
                scope=CommandScope.PARAGRAPH,
                target=ActionTarget(section_id=context.current_section_id, para_id=None, para_index=None),
                params=llm_obj.params,
                content=llm_obj.content,
                confidence=min(0.55, float(llm_obj.confidence)),
                needs_clarification=True,
                clarification=_clarification_from_section(section),
            )

    if merged.get("scope") != CommandScope.PARAGRAPH.value:
        merged["scope"] = CommandScope.PARAGRAPH.value

    if merged["action"] == CommandAction.CHANGE_TONE.value and merged["params"].get("tone") is None:
        fb_tone = fallback_obj.params.tone.value if fallback_obj.params.tone else ToneValue.formal.value
        merged["params"]["tone"] = fb_tone

    if merged["action"] in {CommandAction.REPLACE_TEXT.value, CommandAction.INSERT_TEXT.value} and not merged.get("content"):
        merged["content"] = fallback_obj.content

    return ActionObject.model_validate(merged)


async def extract_action_object_with_meta(prompt: str, context: CommandContext, structured: dict) -> IntentExtractionResult:
    fallback_obj: ActionObject | None = None

    def _get_fallback() -> ActionObject:
        nonlocal fallback_obj
        if fallback_obj is None:
            fallback_obj = _rule_based_fallback(prompt, context, structured)
        return fallback_obj

    # In non-ollama modes, or when feature-flagged off, use deterministic resolver behavior.
    if settings.LLM_PROVIDER.lower() != "ollama" or not settings.COMMAND_INTENT_USE_LLM:
        return IntentExtractionResult(
            action_object=_get_fallback(),
            intent_source="fallback_rule",
            repair_applied=False,
            prompt_version=INTENT_PROMPT_VERSION,
        )

    # Pre-check: structural ops and document-wide format commands are fully deterministic
    # — always bypass LLM. The LLM often asks "Which paragraph?" for ADD_PARAGRAPH even
    # when the answer is unambiguous (add after last para). We also bypass when the fallback
    # already resolves without clarification.
    _STRUCTURAL_OPS = {
        CommandAction.ADD_PARAGRAPH.value,
        CommandAction.REMOVE_PARAGRAPH.value,
        CommandAction.INSERT_SECTION.value,
        CommandAction.DELETE_SECTION.value,
        CommandAction.MOVE_SECTION.value,
    }
    # Detect action from prompt directly (cheap, no LLM needed)
    from app.services.command_contract import _detect_action as _da
    _detected_action, _ = _da(prompt)
    _detected_action_val = _detected_action.value if hasattr(_detected_action, "value") else str(_detected_action)
    if _detected_action_val in _STRUCTURAL_OPS:
        return IntentExtractionResult(
            action_object=_get_fallback(),
            intent_source="fallback_rule",
            repair_applied=False,
            prompt_version=INTENT_PROMPT_VERSION,
        )

    _pre = _get_fallback()
    if not _pre.needs_clarification:
        _sp = (_pre.params.style_params or {}) if _pre.params else {}
        # Bypass Ollama when fallback already fully resolved the intent:
        # - document_wide format op (SET_FORMAT over whole doc)
        # - DOCUMENT scope content op (REWRITE/EXPAND/CHANGE_TONE with no para ref → all paras)
        # - specific section_id resolved (e.g. "expand para 3")
        # Without these bypasses, Ollama can misinterpret the action for document-scope ops.
        _pre_para_id = str((_pre.target.para_id if _pre.target else None) or "")
        if (
            _sp.get("document_wide")
            or _pre.scope == CommandScope.DOCUMENT
            or (_pre.target and _pre.target.section_id)
            or _pre_para_id.startswith("__create:")  # "add signee RS Sharma" → insert+fill
        ):
            return IntentExtractionResult(
                action_object=_pre,
                intent_source="fallback_rule",
                repair_applied=False,
                prompt_version=INTENT_PROMPT_VERSION,
            )

    # Fast health gate avoids long command latency when Ollama is down in sandbox environments.
    try:
        tags_url = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/tags"
        requests.get(tags_url, timeout=max(0.1, float(settings.COMMAND_INTENT_HEALTH_TIMEOUT_S)))
    except Exception:
        return IntentExtractionResult(
            action_object=_get_fallback(),
            intent_source="fallback_rule",
            repair_applied=False,
            prompt_version=INTENT_PROMPT_VERSION,
        )

    system_prompt = load_system_prompt(INTENT_PROMPT_VERSION)
    examples = load_few_shot_examples(INTENT_PROMPT_VERSION)

    # Embed few-shot examples directly in the system prompt as formatted text so the
    # LLM sees them as part of its instructions rather than buried user-payload JSON.
    if examples:
        ex_lines = ["\n\nEXAMPLES (input → output JSON):"]
        for ex in examples:
            inp = ex.get("input", {})
            out = ex.get("output", {})
            ex_lines.append(f'\nInput: {json.dumps(inp)}\nOutput: {json.dumps(out)}')
        system_prompt = system_prompt + "".join(ex_lines)

    user_payload = {
        "command": prompt,
        "context": context.model_dump(),
        "sections": _section_catalog_for_prompt(structured),
        "instructions": "Return ActionObject JSON only. Use section ids from the sections list above.",
    }

    try:
        parsed, parse_meta = await call_and_parse_json_with_meta(
            system=system_prompt,
            user=json.dumps(user_payload, ensure_ascii=False),
            schema_hint=_INTENT_SCHEMA_HINT,
        )
        if not isinstance(parsed, dict):
            return IntentExtractionResult(
                action_object=_get_fallback(),
                intent_source="fallback_rule",
                repair_applied=bool(parse_meta.get("repair_applied", False)),
                prompt_version=INTENT_PROMPT_VERSION,
            )

        llm_payload = _normalize_payload_shape(parsed)
        llm_obj = ActionObject.model_validate(llm_payload)

        if llm_obj.needs_clarification:
            # Before trusting LLM clarification, check if the rule-based resolver
            # can handle it deterministically (e.g. "make subject bold" is always
            # resolvable but small LLMs sometimes hallucinate a clarification).
            fallback = _get_fallback()
            if not fallback.needs_clarification:
                return IntentExtractionResult(
                    action_object=fallback,
                    intent_source="fallback_rule",
                    repair_applied=bool(parse_meta.get("repair_applied", False)),
                    prompt_version=INTENT_PROMPT_VERSION,
                )
            return IntentExtractionResult(
                action_object=llm_obj,
                intent_source="llm",
                repair_applied=bool(parse_meta.get("repair_applied", False)),
                prompt_version=INTENT_PROMPT_VERSION,
            )

        # If LLM already returned a fully resolved target, use it directly.
        # MOVE_SECTION is excluded: the LLM often sets para_id to the source's UUID
        # instead of the anchor UUID, so we always merge with the fallback to fix it.
        _is_move = (
            hasattr(llm_obj.action, "value") and llm_obj.action.value == "MOVE_SECTION"
        ) or str(llm_obj.action) == "MOVE_SECTION"
        if llm_obj.target.section_id and llm_obj.target.para_id and not _is_move:
            return IntentExtractionResult(
                action_object=llm_obj,
                intent_source="llm",
                repair_applied=bool(parse_meta.get("repair_applied", False)),
                prompt_version=INTENT_PROMPT_VERSION,
            )

        # Fill missing deterministic target fields from resolver defaults.
        merged = _merge_with_fallback(
            llm_obj=llm_obj,
            fallback_obj=_get_fallback(),
            prompt=prompt,
            context=context,
            structured=structured,
        )
        return IntentExtractionResult(
            action_object=merged,
            intent_source="llm",
            repair_applied=bool(parse_meta.get("repair_applied", False)),
            prompt_version=INTENT_PROMPT_VERSION,
        )
    except Exception:
        return IntentExtractionResult(
            action_object=_get_fallback(),
            intent_source="fallback_rule",
            repair_applied=False,
            prompt_version=INTENT_PROMPT_VERSION,
        )


async def extract_action_object(prompt: str, context: CommandContext, structured: dict) -> ActionObject:
    # Backward-compatible thin wrapper for callers that only need ActionObject.
    result = await extract_action_object_with_meta(prompt, context, structured)
    return result.action_object
