from __future__ import annotations
import asyncio
import re
from typing import Dict, Any, List

from app.config import settings
from app.db import SessionLocal

# Slots
from app.ml.slots.goi_letter import generate_subject, draft_numbered_paras as goi_draft_paras
from app.ml.slots.do_letter import generate_salutation, draft_body_paras as do_draft_paras
from app.ml.slots.movement_order import (
    draft_numbered_paras as mov_draft_paras,
    draft_distribution_lines,
)
from app.ml.slots.leave_certificate import extract_fields


def _normalize_doc_type(doc_type: str) -> str:
    """
    Make doc_type robust against:
    - None / whitespace
    - Enum-like "DocType.GOI_LETTER"
    - case differences
    - common aliases
    """
    s = (doc_type or "").strip()

    # Handle enum string forms like "DocType.GOI_LETTER"
    if "." in s:
        s = s.split(".")[-1]

    s = s.upper()

    aliases = {
        "GOI": "GOI_LETTER",
        "GOVT_LETTER": "GOI_LETTER",
        "GOVERNMENT_OF_INDIA_LETTER": "GOI_LETTER",
        "DO": "DO_LETTER",
        "DEMI_OFFICIAL": "DO_LETTER",
        "MOVEMENT": "MOVEMENT_ORDER",
        "MOVE_ORDER": "MOVEMENT_ORDER",
        "LEAVE": "LEAVE_CERTIFICATE",
        "LEAVE_CERT": "LEAVE_CERTIFICATE",
    }
    return aliases.get(s, s)


def _stub_generate(doc_type: str, prompt: str, rules: str, template_zones: dict, extra_fields: dict) -> Dict[str, Any]:
    return {
        "doc_type": doc_type,
        "fields": {
            **extra_fields,
            "SUBJECT": extra_fields.get("SUBJECT", prompt[:120]),
            "BODY": extra_fields.get("BODY", prompt),
        },
        "lists": {
            "DISTRIBUTION": extra_fields.get("DISTRIBUTION", []),
            "COPY_TO": extra_fields.get("COPY_TO", []),
        }
    }

def _stub_patch(doc_type: str, edit_prompt: str, current_state: Dict[str, Any], zones: dict) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    ep = edit_prompt.lower()
    if "highlight" in ep:
        ops.append({"op": "highlight_contains", "value": "Date"})
    if "change subject" in ep:
        ops.append({"op": "set_field", "field": "SUBJECT", "value": edit_prompt})
    if "move signature left" in ep or "signature left" in ep:
        ops.append({"op": "set_alignment", "zone": "SIGNATURE", "value": "left"})
    if not ops:
        ops.append({"op": "rewrite_body_append", "value": f"\n\n[EDIT NOTE]: {edit_prompt}"})
    return ops

def _ollama_chat(messages: list[dict]) -> str:
    import requests
    url = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/chat"
    payload = {
        "model": settings.OLLAMA_CHAT_MODEL,
        "messages": messages,
        "options": {"temperature": settings.OLLAMA_TEMPERATURE},
        "stream": False,
    }
    r = requests.post(url, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "")


def _run_async(coro):
    """
    Run an async coroutine from sync code.

    This is suitable for the current sync FastAPI endpoint path.
    """
    return asyncio.run(coro)


def _split_extra(extra_fields: dict | None) -> tuple[dict, dict]:
    """
    Split caller-provided extras into scalar fields and optional blocks.
    """
    extra = _sanitize_optional_values(extra_fields or {})
    blocks = extra.get("blocks", {}) if isinstance(extra.get("blocks"), dict) else {}
    scalar = {k: v for k, v in extra.items() if k != "blocks"}
    return scalar, blocks


def _sanitize_optional_values(value):
    """
    Convert placeholder-like optional marker strings into empty values.
    """
    if isinstance(value, str):
        return "" if value.strip().upper() in {"[[OPTIONAL]]", "<OPTIONAL>"} else value
    if isinstance(value, dict):
        return {k: _sanitize_optional_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_optional_values(v) for v in value]
    return value


def _extract_dot_date_token(text: str) -> str:
    """Return first date token in dd.mm.yyyy format from text, else empty."""
    m = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", text or "")
    return m.group(0) if m else ""


def _derive_recipient_surname(extra_scalar: dict) -> str:
    """
    Best-effort recipient salutation token for templates using RECIPIENT_SURNAME.
    """
    raw = (
        extra_scalar.get("RECIPIENT_SURNAME")
        or extra_scalar.get("recipient_surname")
        or extra_scalar.get("recipient_name")
        or extra_scalar.get("RECIPIENT_NAME")
        or ""
    )
    return str(raw).strip().strip(",")


def generate_doc_state(doc_type: str, prompt: str, rules: str, template_zones: dict, extra_fields: dict) -> Dict[str, Any]:
    """
    Generate doc_state using slot-based generation.

    Notes:
    - Signature kept unchanged for compatibility.
    - `rules` is retained but slots do their own RAG retrieval.
    - We return both `blocks` and `lists` so existing renderer paths continue working.
    """
    doc_type = _normalize_doc_type(doc_type)
    print(f"[generate_doc_state] normalized doc_type={doc_type!r}")
    extra_fields = _sanitize_optional_values(extra_fields or {})

    if settings.LLM_PROVIDER.lower() != "ollama":
        return _stub_generate(doc_type, prompt, rules, template_zones, extra_fields)

    prompt = (prompt or "").strip()
    extra_scalar, extra_blocks = _split_extra(extra_fields)

    db = SessionLocal()
    try:
        if doc_type == "GOI_LETTER":
            subject = _run_async(generate_subject(db, prompt))
            paras = _run_async(goi_draft_paras(db, prompt, min_paras=2, max_paras=4))
            copy_to_list = extra_scalar.get("copy_to_list", [])

            return {
                "doc_type": doc_type,
                "fields": {
                    **extra_scalar,
                    "subject": subject,
                },
                "blocks": {
                    **extra_blocks,
                    "body_paras": paras,
                    "copy_to_list": copy_to_list,
                },
                "lists": extra_scalar.get("lists", {}),
                "meta": {"generator": "slots_v1"},
            }

        if doc_type == "DO_LETTER":
            salutation = _run_async(generate_salutation(db, prompt))
            paras = _run_async(do_draft_paras(db, prompt, min_paras=1, max_paras=2))
            dot_date = _extract_dot_date_token(prompt)
            if dot_date and dot_date not in "\n".join(paras):
                if paras:
                    paras[-1] = f"{paras[-1].rstrip('.')} The required inputs may please be forwarded by {dot_date}."
                else:
                    paras = [f"The required inputs may please be forwarded by {dot_date}."]
            body_text = "\n".join(paras)
            recipient_surname = _derive_recipient_surname(extra_scalar)

            return {
                "doc_type": doc_type,
                "fields": {
                    **extra_scalar,
                    "SENDER_NAME": extra_scalar.get("SENDER_NAME", extra_scalar.get("sender_name", "")),
                    "SENDER_APPOINTMENT": extra_scalar.get("SENDER_APPOINTMENT", extra_scalar.get("sender_designation", "")),
                    "RECIPIENT_NAME": extra_scalar.get("RECIPIENT_NAME", extra_scalar.get("recipient_name", "")),
                    "RECIPIENT_SURNAME": extra_scalar.get("RECIPIENT_SURNAME", recipient_surname),
                    "SALUTATION": salutation,
                    "salutation": salutation,
                    "BODY": body_text,
                },
                "blocks": {
                    **extra_blocks,
                    "body_paras": paras,
                },
                "lists": {
                    "body_paras": paras,
                    "PARAS": paras,
                },
                "meta": {"generator": "slots_v1"},
            }

        if doc_type == "MOVEMENT_ORDER":
            paras = _run_async(mov_draft_paras(db, prompt, min_paras=2, max_paras=4))
            dist = _run_async(draft_distribution_lines(db, prompt, max_lines=6))
            body_text = "\n".join(paras)

            return {
                "doc_type": doc_type,
                "fields": {
                    **extra_scalar,
                    "BODY": body_text,
                },
                "blocks": {
                    **extra_blocks,
                    "order_paras": paras,
                    "distribution_lines": dist,
                },
                "lists": {
                    "order_paras": paras,
                    "distribution_lines": dist,
                    "DISTRIBUTION": dist,
                },
                "meta": {"generator": "slots_v1"},
            }

        if doc_type == "LEAVE_CERTIFICATE":
            fields = _run_async(extract_fields(db, prompt))
            return {
                "doc_type": doc_type,
                "fields": {
                    **fields,
                    **extra_scalar,
                },
                "blocks": {
                    **extra_blocks,
                },
                "lists": {},
                "meta": {"generator": "slots_v1"},
            }

        # Fallback for unknown/legacy doctypes.
        return _stub_generate(doc_type, prompt, rules, template_zones, extra_fields)
    except Exception:
        return _stub_generate(doc_type, prompt, rules, template_zones, extra_fields)
    finally:
        db.close()

def generate_section_texts(doc_type: str, prompt: str, db) -> "SectionTexts":
    """
    PRIMARY INTEGRATION FUNCTION — backend developer calls this.
    Generates plain text content for all LLM-fillable sections of a document.
    WHY THIS INSTEAD OF generate_doc_state():
        generate_doc_state() returns a renderer-specific flat dict (fields, blocks, lists)
        shaped for the docxtpl template engine. That shape is a sandbox implementation detail.

        generate_section_texts() returns SectionTexts — a stable, typed contract
        that the Document Engine can consume without knowing anything about the renderer.
        The SectionTexts fields map cleanly to Blueprint section types via
        SectionTexts.get_for_section_type().

    Input:
        doc_type: normalized document type string (e.g. "GOI_LETTER")
        prompt:   officer's natural language description of the document
        db:       SQLAlchemy session (used for RAG rulebook retrieval)

    Output:
        SectionTexts — a typed contract containing ONLY plain text values.
        No structure. No Lexical JSON. No rendering keys.
        The caller (Document Engine bridge) wraps these texts into Lexical JSON
        via lexical_wrapper.text_to_lexical_node() and hands them to the engine.

    Integration flow (when Document Engine is ready):
        section_texts = generate_section_texts(doc_type, prompt, db)
        for section in blueprint.fillable_sections(doc_type):
            text = section_texts.get_for_section_type(section.type)
            if text:
                lexical = text_to_lexical_node(text, doc.style_defaults)
                engine.update_section(section.id, lexical)

    Sandbox behaviour (current):
        Calls the same slot functions used by generate_doc_state().
        Returns a SectionTexts object. fill_adapter.py handles the rest
        temporarily until Document Engine exists.

    NOTE: generate_doc_state() below still exists for backward compatibility
    with the current sandbox renderer. It will be removed after integration.
    """
    from app.ml.contracts import SectionTexts

    doc_type = _normalize_doc_type(doc_type)
    # WHAT: Return a minimal stub when Ollama is not configured.
    # WHY:  Allows sandbox to run in stub mode (LLM_PROVIDER=stub) without
    #       Ollama running, e.g. on a dev machine without a GPU.
    if settings.LLM_PROVIDER.lower() != "ollama":
        # Stub: return minimal valid SectionTexts for sandbox without Ollama
        return SectionTexts(
            subject=prompt[:120],
            paras=[f"1. {prompt}"],
        )

    prompt = (prompt or "").strip()
    db_session = db

    try:
        # WHAT: Call the same slot functions used by generate_doc_state().
        # WHY:  Slots encapsulate doctype-specific prompt logic and RAG retrieval.
        #       We reuse them here to avoid duplicating generation logic.
        if doc_type == "GOI_LETTER":
            subject = _run_async(generate_subject(db_session, prompt))
            paras = _run_async(goi_draft_paras(db_session, prompt, min_paras=2, max_paras=4))
            return SectionTexts(subject=subject, paras=paras)

        if doc_type == "DO_LETTER":
            salutation = _run_async(generate_salutation(db_session, prompt))
            paras = _run_async(do_draft_paras(db_session, prompt, min_paras=1, max_paras=2))
            # WHAT: Append deadline date if mentioned in the prompt but missing from paras.
            # WHY:  DO letters often contain a "please revert by DD.MM.YYYY" sentence.
            #       The slot may omit it if the date was implicit in the prompt.
            dot_date = _extract_dot_date_token(prompt)
            if dot_date and dot_date not in "\n".join(paras):
                if paras:
                    paras[-1] = f"{paras[-1].rstrip('.')} The required inputs may please be forwarded by {dot_date}."
                else:
                    paras = [f"The required inputs may please be forwarded by {dot_date}."]
            return SectionTexts(salutation=salutation, paras=paras)

        if doc_type == "MOVEMENT_ORDER":
            paras = _run_async(mov_draft_paras(db_session, prompt, min_paras=2, max_paras=4))
            dist = _run_async(draft_distribution_lines(db_session, prompt, max_lines=6))
            return SectionTexts(paras=paras, distribution_lines=dist)

        if doc_type == "LEAVE_CERTIFICATE":
            # WHAT: Leave certificate extracts structured fields, not free prose.
            # WHY:  The certificate template has named placeholders (rank, name, unit, dates)
            #       rather than numbered paragraphs, so fields dict is the right contract.
            fields = _run_async(extract_fields(db_session, prompt))
            return SectionTexts(fields=fields)

        # Unknown doctype — return minimal stub
        # WHY:  Prevents a hard failure for document types added to the system before
        #       their slot module is implemented.
        return SectionTexts(subject=prompt[:120], paras=[f"1. {prompt}"])

    except Exception:
        # WHAT: Catch-all fallback returns minimal stub rather than raising.
        # WHY:  A generation failure should not prevent the document from being created.
        #       The officer can edit the placeholder text manually.
        #       The exception is swallowed here — add logging here if you need visibility.
        return SectionTexts(subject=prompt[:120], paras=[f"1. {prompt}"])


def edit_to_patch(doc_type: str, edit_prompt: str, current_state: Dict[str, Any], zones: dict) -> List[Dict[str, Any]]:
    """Generate patch ops via local Ollama (STRICT JSON array). Falls back to stub."""
    if settings.LLM_PROVIDER.lower() != "ollama":
        return _stub_patch(doc_type, edit_prompt, current_state, zones)

    system = (
        "Output STRICT JSON only (no markdown). Return a JSON array of patch ops. "
        "Allowed ops: set_field{field,value}, replace_all{old,new}, highlight_contains{value}, "
        "set_alignment{zone,value}, rewrite_body_append{value}."
    )
    user = {
        "doc_type": doc_type,
        "edit_prompt": edit_prompt,
        "known_zones": zones.get("anchors", []),
        "current_fields": current_state.get("fields", {}),
    }

    try:
        content = _ollama_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": str(user)}
        ])
        import json
        ops = json.loads(content)
        if not isinstance(ops, list):
            return _stub_patch(doc_type, edit_prompt, current_state, zones)
        return ops
    except Exception:
        return _stub_patch(doc_type, edit_prompt, current_state, zones)
