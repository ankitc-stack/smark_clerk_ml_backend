from __future__ import annotations

"""PatchOps engine (deterministic).

PatchOps are *structure-preserving* edits applied to the filled skeleton JSON.
The LLM (or UI) can propose ops, but this file applies them deterministically.

Supported ops (minimal starter set):
- replace_section_text: set sections[i].content.text OR content.value
- replace_receiver_lines: set receiver_block content.lines
- replace_signee_lines: set signee_block content.lines (or signer fields if present)
- set_para_style: set style overrides on a paragraph item (bold/italic/underline/highlight/align/font/color/size)
- replace_para_text: replace a paragraph item text
- insert_para_after: insert a new paragraph after an existing para_id
- delete_para: delete a paragraph by id
- insert_section_after: insert a full section payload after a section id (or append)
- delete_section: remove a section by id
- move_section: reposition a section before/after another section id

We use stable IDs already present in document.json example:
- section id e.g. "sec_body_001"
- para id e.g. "p2"
"""

import copy
from app.config import settings
from app.services.lexical_wrapper import apply_format_to_lexical

ALLOWED_STYLE_KEYS = {"bold", "italic", "underline", "highlight", "align", "font", "color", "size"}

def _index_sections(filled: dict) -> dict[str, dict]:
    return {s.get("id"): s for s in filled.get("sections", []) if isinstance(s, dict) and s.get("id")}


def _find_section_index(filled: dict, section_id: str | None) -> int | None:
    if not section_id:
        return None
    for idx, section in enumerate(filled.get("sections", []) or []):
        if isinstance(section, dict) and section.get("id") == section_id:
            return idx
    return None


def _first_numbered_section(filled: dict) -> dict | None:
    for sec in filled.get("sections", []) or []:
        if isinstance(sec, dict) and sec.get("type") == "numbered_paragraphs":
            return sec
    return None


def _resolve_para_section(out: dict, sections: dict[str, dict], section_id: str | None) -> dict | None:
    sec = sections.get(section_id) if section_id else None
    if sec is not None:
        return sec

    # Fallback is intentionally guarded because silent fallback can mutate the wrong section.
    if settings.PATCHOPS_ALLOW_FIRST_NUMBERED_FALLBACK:
        return _first_numbered_section(out)
    return None

def _find_para(section: dict, para_id: str):
    items = (section.get("content") or {}).get("items") or []
    for it in items:
        if it.get("id") == para_id:
            return it
    return None

def apply_patch_ops(filled: dict, ops: list[dict]) -> dict:
    """Apply ops and return a NEW filled dict (does not mutate input)."""
    out = copy.deepcopy(filled)
    out.setdefault("sections", [])
    sections = _index_sections(out)

    for op in ops or []:
        kind = op.get("op")
        tgt = op.get("target") or {}
        section_id = tgt.get("section_id")
        sec = sections.get(section_id)

        if kind == "replace_section_text":
            if not sec:
                continue
            content = sec.setdefault("content", {})
            # Works for subject/date/reference that have content.text or content.value
            text = op.get("text", "")
            if "text" in content:
                content["text"] = text
            elif "value" in content:
                content["value"] = text
            else:
                content["text"] = text

        elif kind == "replace_receiver_lines":
            if not sec:
                continue
            content = sec.setdefault("content", {})
            lines = op.get("lines") or []
            content["lines"] = list(lines)

        elif kind == "replace_signee_lines":
            if not sec:
                continue
            content = sec.setdefault("content", {})
            lines = op.get("lines") or []
            content["lines"] = list(lines)

        elif kind == "replace_para_text":
            sec = _resolve_para_section(out, sections, section_id)
            para_id = tgt.get("para_id")
            para = _find_para(sec, para_id) if para_id else None
            if para:
                para["text"] = op.get("text", "")

        elif kind == "set_para_style":
            sec = _resolve_para_section(out, sections, section_id)
            para_id = tgt.get("para_id")
            para = _find_para(sec, para_id) if para_id else None
            if para:
                style = op.get("style") or {}
                cleaned = {k: v for k, v in style.items() if k in ALLOWED_STYLE_KEYS}
                para.setdefault("style_overrides", {}).update(cleaned)
                # Apply align to Lexical paragraph.format (Word/Google Docs behaviour).
                if "align" in cleaned:
                    richtext_state = (para.get("richtext") or {}).get("state")
                    if isinstance(richtext_state, dict):
                        para["richtext"]["state"] = apply_format_to_lexical(
                            richtext_state, cleaned
                        )

        elif kind == "set_section_style":
            # Applies style overrides to a whole section (subject, date, reference, etc.)
            if sec:
                style = op.get("style") or {}
                cleaned = {k: v for k, v in style.items() if k in ALLOWED_STYLE_KEYS}
                sec.setdefault("style_overrides", {}).update(cleaned)
                if "align" in cleaned:
                    _sec_type = sec.get("type")
                    if _sec_type in ("signee_block", "receiver_block"):
                        # For these blocks, align means block-level positioning:
                        # the whole container moves left/right, text inside stays
                        # as-is. Only layout_hints.alignment controls this.
                        layout_hints = sec.get("layout_hints")
                        if isinstance(layout_hints, dict):
                            layout_hints["alignment"] = cleaned["align"]
                    else:
                        # For all other sections (subject, date, body paragraphs),
                        # align is text alignment — update Lexical paragraph.format
                        # exactly like Word/Google Docs.
                        richtext_state = (sec.get("content") or {}).get("richtext", {}).get("state")
                        if isinstance(richtext_state, dict):
                            sec["content"]["richtext"]["state"] = apply_format_to_lexical(
                                richtext_state, cleaned
                            )

        elif kind == "insert_para_after":
            sec = _resolve_para_section(out, sections, section_id)
            if not sec:
                continue
            content = sec.setdefault("content", {})
            after_id = tgt.get("after_para_id")
            new_para = op.get("para") or {}
            items = content.get("items") or []
            # Find insertion index
            idx = None
            for i, it in enumerate(items):
                if it.get("id") == after_id:
                    idx = i
                    break
            if idx is None:
                items.append(new_para)
            else:
                items.insert(idx + 1, new_para)
            content["items"] = items

        elif kind == "delete_para":
            sec = _resolve_para_section(out, sections, section_id)
            if not sec:
                continue
            content = sec.setdefault("content", {})
            para_id = tgt.get("para_id")
            if para_id:
                items = content.get("items") or []
                content["items"] = [it for it in items if it.get("id") != para_id]

        elif kind == "insert_section_after":
            new_section = op.get("section")
            if not isinstance(new_section, dict) or not new_section.get("id"):
                continue
            after_section_id = tgt.get("after_section_id")
            insert_at = _find_section_index(out, after_section_id)
            section_items = out.get("sections") or []
            if insert_at is None:
                section_items.append(new_section)
            else:
                section_items.insert(insert_at + 1, new_section)
            out["sections"] = section_items
            sections = _index_sections(out)

        elif kind == "delete_section":
            target_section_id = tgt.get("section_id")
            if not target_section_id:
                continue
            out["sections"] = [
                sec_item
                for sec_item in (out.get("sections") or [])
                if not (isinstance(sec_item, dict) and sec_item.get("id") == target_section_id)
            ]
            sections = _index_sections(out)

        elif kind == "move_section":
            source_section_id = tgt.get("section_id")
            anchor_section_id = tgt.get("anchor_section_id")
            if not source_section_id or not anchor_section_id:
                continue

            source_idx = _find_section_index(out, source_section_id)
            anchor_idx = _find_section_index(out, anchor_section_id)
            if source_idx is None or anchor_idx is None or source_idx == anchor_idx:
                continue

            section_items = out.get("sections") or []
            moving_section = section_items.pop(source_idx)
            if source_idx < anchor_idx:
                anchor_idx -= 1

            position = str(tgt.get("position") or "after").lower()
            insert_idx = anchor_idx if position == "before" else anchor_idx + 1
            insert_idx = max(0, min(insert_idx, len(section_items)))
            section_items.insert(insert_idx, moving_section)
            out["sections"] = section_items
            sections = _index_sections(out)

        # Unknown ops are ignored to keep patch application robust.

    return out
