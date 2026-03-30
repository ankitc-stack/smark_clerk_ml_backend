"""Letter Template Store — Phase 7.

Saves the *structure* (section order + static fields) of a letter as a reusable
template.  Variable content (subject, paragraphs, ref, date, receiver) is blanked
out; static fields (letterhead, signee_block) are preserved.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_STORE_DIR = os.path.join("data", "saved_templates")

# Section types whose content should be preserved when saving as template.
# Everything else is blanked so the user fills it fresh each time.
_STICKY_TYPES: frozenset[str] = frozenset({"letterhead", "signee_block"})


def _store_dir() -> str:
    os.makedirs(_STORE_DIR, exist_ok=True)
    return _STORE_DIR


def save_template(
    letter_type: str,
    display_name: str,
    doc_id: str,
    section_schema: list[dict],
) -> str:
    """Persist a template JSON to disk.  Returns the new template_id."""
    template_id = f"{letter_type}_{uuid.uuid4().hex[:8]}"
    data = {
        "template_id": template_id,
        "letter_type": letter_type,
        "display_name": display_name or letter_type.replace("_", " ").title(),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source_doc_id": doc_id,
        "section_schema": section_schema,
    }
    path = os.path.join(_store_dir(), f"{template_id}.json")
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return template_id


def list_templates(letter_type: Optional[str] = None) -> list[dict]:
    """Return saved templates, optionally filtered by letter_type, newest first."""
    d = _store_dir()
    results: list[dict] = []
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        try:
            data = json.loads(Path(os.path.join(d, fname)).read_text())
        except Exception:
            continue
        if letter_type and data.get("letter_type") != letter_type:
            continue
        results.append({
            "template_id":  data.get("template_id", fname[:-5]),
            "letter_type":  data.get("letter_type", ""),
            "display_name": data.get("display_name", ""),
            "saved_at":     data.get("saved_at", ""),
            "section_count": len(data.get("section_schema") or []),
        })
    results.sort(key=lambda x: x["saved_at"], reverse=True)
    return results


def load_template(template_id: str) -> Optional[dict]:
    """Load a template by ID.  Returns None if not found."""
    path = os.path.join(_store_dir(), f"{template_id}.json")
    if not os.path.exists(path):
        return None
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def delete_template(template_id: str) -> bool:
    """Delete a template.  Returns True if deleted, False if not found."""
    path = os.path.join(_store_dir(), f"{template_id}.json")
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def build_section_schema(sections: list[dict], section_text_fn) -> list[dict]:
    """Build the section_schema list from doc-engine section objects.

    For sticky types (letterhead, signee_block) the current content is kept.
    For variable types (subject, paragraph, reference_number, date, receiver_block)
    the content is blanked so the user fills it fresh on each new document.
    """
    schema: list[dict] = []
    for sec in sections:
        sec_type = sec.get("type", "")
        content = section_text_fn(sec) if sec_type in _STICKY_TYPES else ""
        schema.append({"type": sec_type, "content": content})
    return schema
