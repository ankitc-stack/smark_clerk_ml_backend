"""Letter Template Store — Phase 7 (DB-backed).

Saves the *structure* (section order + static fields) of a letter as a reusable
template in PostgreSQL.  Variable content (subject, paragraphs, ref, date,
receiver) is blanked out; static fields (letterhead, signee_block) are preserved.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

# Section types whose content should be preserved when saving as template.
# Everything else is blanked so the user fills it fresh each time.
_STICKY_TYPES: frozenset[str] = frozenset({"letterhead", "signee_block"})


def _get_db():
    from app.db import SessionLocal
    return SessionLocal()


def save_template(
    letter_type: str,
    display_name: str,
    doc_id: str,
    section_schema: list[dict],
) -> str:
    """Persist a template to the DB. Returns the new template_id."""
    from app.models import UserSavedTemplate
    template_id = f"{letter_type}_{uuid.uuid4().hex[:8]}"
    db = _get_db()
    try:
        db.add(UserSavedTemplate(
            template_id=template_id,
            letter_type=letter_type,
            display_name=display_name or letter_type.replace("_", " ").title(),
            source_doc_id=doc_id,
            section_schema=section_schema,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()
    finally:
        db.close()
    return template_id


def list_templates(letter_type: Optional[str] = None) -> list[dict]:
    """Return saved templates from DB, optionally filtered by letter_type, newest first."""
    from app.models import UserSavedTemplate
    db = _get_db()
    try:
        q = db.query(UserSavedTemplate)
        if letter_type:
            q = q.filter(UserSavedTemplate.letter_type == letter_type)
        rows = q.order_by(UserSavedTemplate.created_at.desc()).all()
        return [
            {
                "template_id":   r.template_id,
                "letter_type":   r.letter_type,
                "display_name":  r.display_name,
                "saved_at":      r.created_at.isoformat() if r.created_at else "",
                "section_count": len(r.section_schema or []),
            }
            for r in rows
        ]
    finally:
        db.close()


def load_template(template_id: str) -> Optional[dict]:
    """Load a template by ID from DB. Returns None if not found."""
    from app.models import UserSavedTemplate
    db = _get_db()
    try:
        row = db.get(UserSavedTemplate, template_id)
        if not row:
            return None
        return {
            "template_id":    row.template_id,
            "letter_type":    row.letter_type,
            "display_name":   row.display_name,
            "saved_at":       row.created_at.isoformat() if row.created_at else "",
            "source_doc_id":  row.source_doc_id,
            "section_schema": row.section_schema or [],
        }
    finally:
        db.close()


def delete_template(template_id: str) -> bool:
    """Delete a template from DB. Returns True if deleted, False if not found."""
    from app.models import UserSavedTemplate
    db = _get_db()
    try:
        row = db.get(UserSavedTemplate, template_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()


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
