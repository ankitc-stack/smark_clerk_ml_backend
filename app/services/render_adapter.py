from __future__ import annotations

"""Adapter: filled skeleton JSON (document.json shape) -> renderer doc_state (fields/blocks/lists).

We keep your current renderer (docxtpl templates) by converting the filled structure
into the keys your templates expect.

For blueprint documents (from the doc-engine microservice), section content is stored as
Lexical JSON ({ "richtext": { "format": "lexical", "state": {...} } }) rather than plain
text.  _section_text() transparently extracts plain text from either format.
"""

import re

from app.services.lexical_wrapper import lexical_to_plain_text, lexical_nodes_to_rich_text


def _section_text(section: dict, field: str = "text") -> str:
    """Extract plain text from a section's content, handling both legacy plain-text
    and blueprint Lexical JSON formats.

    Legacy format:  section["content"]["text"] = "some string"
    Blueprint format: section["content"]["richtext"]["state"] = { Lexical root ... }
    """
    content = section.get("content") or {}
    # Try plain text first (legacy skeleton / slot-filled path).
    plain = content.get(field) or content.get("text") or content.get("value") or ""
    if plain:
        return str(plain)
    # Fall back to Lexical JSON richtext (blueprint doc-engine path).
    richtext = content.get("richtext") or {}
    state = richtext.get("state")
    if isinstance(state, dict):
        return lexical_to_plain_text(state)
    return ""

def _section_rich(section: dict):
    """Return a docxtpl RichText (with formatting) or plain str for a section.

    For blueprint sections with Lexical JSON: reads format flags from text nodes
    and builds a RichText so bold/italic/color/font/size survive DOCX rendering.
    For legacy plain-text sections: returns a plain str (no formatting info).
    """
    content = section.get("content") or {}
    richtext = content.get("richtext") or {}
    state = richtext.get("state")
    if isinstance(state, dict):
        return lexical_nodes_to_rich_text(state)
    return content.get("text") or content.get("value") or ""


def populate_sections_from_slots(filled: dict) -> None:
    """
    Before sending a document to the refill LLM, write slot values into
    the matching section content fields.

    WHY: After the initial /fill, _slots holds all key values but the
    skeleton sections (subject, receiver_block, date, signee_block) have
    empty content stubs.  The refill LLM sees those empty stubs and either
    leaves them blank or invents values.  Populating them first gives the
    LLM accurate context so it can make targeted edits.
    """
    sections = filled.get("sections") or []
    by_type = {s.get("type"): s for s in sections if isinstance(s, dict)}
    slots = filled.get("_slots") or {}

    def _sec_content(sec_type: str) -> dict:
        sec = by_type.get(sec_type) or {}
        if "content" not in sec:
            sec["content"] = {}
        return sec["content"]

    # Subject line
    subj_text = slots.get("subject", "")
    if subj_text:
        _sec_content("subject")["text"] = subj_text

    # Reference number
    ref_val = slots.get("file_reference_number") or slots.get("army_no") or ""
    if ref_val:
        _sec_content("reference_number")["value"] = ref_val

    # Date (use first non-empty slot date)
    date_val = slots.get("date") or slots.get("date_gregorian") or ""
    if date_val:
        _sec_content("date")["value"] = date_val

    # Receiver block — build lines list from addressee slots
    a1 = slots.get("addressee_1", "")
    a2 = slots.get("addressee_2", "")
    recv_lines = [l for l in [a1, a2] if l]
    if recv_lines:
        _sec_content("receiver_block")["lines"] = recv_lines

    # Signee block
    sig_c = _sec_content("signee_block")
    if slots.get("signatory_name"):
        sig_c["signer_name"] = slots["signatory_name"]
    if slots.get("signatory_designation"):
        sig_c["rank_or_title"] = slots["signatory_designation"]
    if slots.get("signatory_dept"):
        sig_c["organization"] = slots["signatory_dept"]


def sync_slots_from_sections(filled: dict, doc_type: str) -> None:
    """
    After the refill LLM returns an updated document, copy section content
    values back into _slots so the slot-priority logic in
    doc_state_from_filled_skeleton picks up the LLM's edits.

    WHY: doc_state_from_filled_skeleton gives _slots priority over section
    content for key fields (subject, addressee, date).  If the LLM edits
    a section but _slots still holds the old value, the rendered DOCX
    silently ignores the edit.  This function closes that gap.
    """
    sections = filled.get("sections") or []
    by_type = {s.get("type"): s for s in sections if isinstance(s, dict)}
    slots = filled.setdefault("_slots", {})

    def _get(sec_type: str, key: str):
        return ((by_type.get(sec_type) or {}).get("content") or {}).get(key) or ""

    # Subject
    subj = _get("subject", "text").strip()
    if subj:
        slots["subject"] = subj

    # Reference number
    ref_val = _get("reference_number", "value").strip()
    if ref_val:
        if doc_type == "LEAVE_CERTIFICATE":
            slots["army_no"] = ref_val
        else:
            slots["file_reference_number"] = ref_val

    # Date
    date_val = _get("date", "value").strip()
    if date_val:
        if doc_type == "GOI_LETTER":
            slots["date_gregorian"] = date_val
        else:
            slots["date"] = date_val

    # Receiver block → addressee lines
    recv_lines = _get("receiver_block", "lines")
    if isinstance(recv_lines, list) and recv_lines:
        if recv_lines[0]:
            slots["addressee_1"] = recv_lines[0].strip()
        if len(recv_lines) > 1 and recv_lines[1]:
            slots["addressee_2"] = recv_lines[1].strip()

    # Signee block
    sig_c = (by_type.get("signee_block") or {}).get("content") or {}
    if sig_c.get("signer_name", "").strip():
        slots["signatory_name"] = sig_c["signer_name"].strip()
    desig = (sig_c.get("rank_or_title") or sig_c.get("appointment") or "").strip()
    if desig:
        slots["signatory_designation"] = desig
    if sig_c.get("organization", "").strip():
        slots["signatory_dept"] = sig_c["organization"].strip()


def _is_blueprint_structured(structured: dict) -> bool:
    """Return True when structured data comes from the doc-engine blueprint format.

    Blueprint docs have individual section objects (type="paragraph", type="subject", …).
    Legacy skeleton docs have a single "numbered_paragraphs" section with an items array.
    """
    types = {s.get("type") for s in (structured.get("sections") or []) if isinstance(s, dict)}
    return bool(types) and "numbered_paragraphs" not in types


def _blueprint_body_paras(sections: list, numbered: bool = True) -> list:
    """Collect all 'paragraph' sections, return text or RichText per entry.

    numbered=True  → prepends "N. " for templates that use {{ p }} directly (GOI, MO).
    numbered=False → plain text/RichText for templates with their own numbering (DO letter).

    Returns RichText objects for sections that carry Lexical formatting (bold/italic/etc.)
    so docxtpl preserves the formatting in the rendered Word document.
    Plain strings are returned for unformatted sections.
    """
    paras: list = []
    idx = 1
    for sec in sections:
        if not isinstance(sec, dict) or sec.get("type") != "paragraph":
            continue
        rich = _section_rich(sec)
        # Determine plain text for emptiness check and number prefix
        plain = rich if isinstance(rich, str) else _section_text(sec)
        plain = re.sub(r"^\d+\.\s*", "", plain.strip()).strip()
        if not plain:
            continue
        if numbered:
            prefix = f"{idx}. "
            try:
                # RichText: prepend number as unstyled run then append formatted runs
                from docxtpl import RichText
                if isinstance(rich, RichText):
                    rt = RichText(prefix)
                    for run in rich._runs:   # type: ignore[attr-defined]
                        rt._runs.append(run)  # type: ignore[attr-defined]
                    paras.append(rt)
                else:
                    paras.append(f"{prefix}{plain}")
            except Exception:
                paras.append(f"{prefix}{plain}")
        else:
            paras.append(rich if not isinstance(rich, str) else plain)
        idx += 1
    return paras


def _doc_state_from_blueprint(structured: dict, doc_type: str) -> dict:
    """Build render doc_state from a blueprint-format structured dict.

    Blueprint sections are individual objects (no numbered_paragraphs container).
    _slots may be populated (when created with a prompt) or empty (when created without).
    """
    sections = structured.get("sections") or []
    slots = structured.get("_slots") or {}

    # Build a type → first section mapping for single-instance sections
    by_type: dict = {}
    for s in sections:
        t = s.get("type") if isinstance(s, dict) else None
        if t and t not in by_type:
            by_type[t] = s

    # Extract text/RichText from named sections.
    # _section_rich() returns RichText when Lexical formatting is present, plain str otherwise.
    # Plain text fallback (_section_text) used wherever only str is needed (splitting, slots).
    ref_text  = _section_rich(by_type.get("reference_number") or {})
    date_text = _section_rich(by_type.get("date") or {})
    subj_text = _section_rich(by_type.get("subject") or {})

    recv_plain  = _section_text(by_type.get("receiver_block") or {})
    recv_lines  = [l.strip() for l in recv_plain.splitlines() if l.strip()]

    sign_plain  = _section_text(by_type.get("signee_block") or {})
    sign_lines  = [l.strip() for l in sign_plain.splitlines() if l.strip()]

    # Plain-text versions for slots fallback logic (slots are always plain strings)
    ref_plain  = _section_text(by_type.get("reference_number") or {})
    date_plain = _section_text(by_type.get("date") or {})
    subj_plain = _section_text(by_type.get("subject") or {})

    # DO_LETTER DOCX template renders its own "{{ loop.index }}." numbering,
    # so body_paras must be plain text. All other templates use {{ p }} directly
    # and expect "N. text" strings.
    _do_plain = doc_type.upper().replace("-", "_") == "DO_LETTER"
    body_paras = _blueprint_body_paras(sections, numbered=not _do_plain)

    fields: dict = {}
    blocks: dict = {}
    lists:  dict = {}

    dt = doc_type.upper().replace("-", "_")

    if dt == "GOI_LETTER":
        fields = {
            "file_reference_number": slots.get("file_reference_number") or ref_text,
            "date_gregorian":        slots.get("date_gregorian") or date_text,
            "date_indian":           slots.get("date_indian") or "",
            "ministry_name":         slots.get("ministry_name") or "",
            "address_line_1":        slots.get("address_line_1") or "",
            "address_line_2":        slots.get("address_line_2") or "",
            "telephone":             slots.get("telephone") or "",
            "email":                 slots.get("email") or "",
            "subject":               subj_text or slots.get("subject") or "",
            "addressee_1":           slots.get("addressee_1") or (recv_lines[0] if recv_lines else ""),
            "addressee_2":           slots.get("addressee_2") or (recv_lines[1] if len(recv_lines) > 1 else ""),
            "signatory_designation": slots.get("signatory_designation") or (sign_lines[1] if len(sign_lines) > 1 else ""),
            "signatory_dept":        slots.get("signatory_dept") or (sign_lines[2] if len(sign_lines) > 2 else ""),
        }
        copy_to = slots.get("copy_to_list") or []
        blocks = {"body_paras": body_paras, "copy_to_list": copy_to if isinstance(copy_to, list) else []}

    elif dt == "DO_LETTER":
        fields = {
            "file_reference_number": slots.get("file_reference_number") or ref_text,
            "date":                  slots.get("date") or date_text,
            "subject":               subj_text or slots.get("subject") or "",
            "addressee_1":           slots.get("addressee_1") or (recv_lines[0] if recv_lines else ""),
            "addressee_2":           slots.get("addressee_2") or (recv_lines[1] if len(recv_lines) > 1 else ""),
            "salutation":            slots.get("salutation") or "My dear Sir,",
            "signatory_name":        slots.get("signatory_name") or (sign_lines[0] if sign_lines else ""),
            "signatory_designation": slots.get("signatory_designation") or (sign_lines[1] if len(sign_lines) > 1 else ""),
            "signatory_dept":        slots.get("signatory_dept") or (sign_lines[2] if len(sign_lines) > 2 else ""),
        }
        copy_to = slots.get("copy_to_list") or []
        blocks = {"body_paras": body_paras, "copy_to_list": copy_to if isinstance(copy_to, list) else []}

    elif dt == "MOVEMENT_ORDER":
        fields = {k: slots.get(k) or "" for k in (
            "army_no", "rank", "person_name", "unit", "att_unit",
            "destination", "departure_date", "departure_time",
            "route", "destination_desc", "remarks", "distr_unit",
        )}
        if not fields["departure_date"]:
            fields["departure_date"] = date_text
        blocks = {"order_paras": body_paras, "distribution_lines": []}

    elif dt == "LEAVE_CERTIFICATE":
        prefix_date = slots.get("prefix_date", "")
        suffix_date = slots.get("suffix_date", "")
        if prefix_date and suffix_date:
            prefix_suffix = (
                f" with permission to prefix on {prefix_date}"
                f" and suffix on {suffix_date} being Holiday/ Sunday"
            )
        elif prefix_date:
            prefix_suffix = f" with permission to prefix on {prefix_date}"
        elif suffix_date:
            prefix_suffix = f" with permission to suffix on {suffix_date} being Holiday/ Sunday"
        else:
            prefix_suffix = ""

        fields = {k: slots.get(k) or "" for k in (
            "army_no", "date", "person_name", "rank", "unit", "att_unit",
            "leave_type", "days", "from_date", "to_date",
            "leave_vill", "leave_teh", "leave_dist", "leave_state", "leave_pin",
            "contact_no", "station", "signatory_name", "signatory_designation",
        )}
        if not fields["army_no"]:
            # Only use ref_text if it looks like a real army number (not the catalog placeholder)
            _ref_str = str(ref_plain).strip()
            if _ref_str and "[" not in _ref_str and "REFERENCE" not in _ref_str.upper():
                fields["army_no"] = ref_text
        if not fields["date"]:
            fields["date"] = date_text
        if not fields["date"]:
            from datetime import date as _today
            fields["date"] = _today.today().strftime("%d %b %Y")
        fields["prefix_suffix"] = prefix_suffix
        fields["subject"] = slots.get("subject") or subj_text
        # Build combined leave_address block expected by template
        fields["leave_address"] = "\n".join(filter(None, [
            f"Vill & PO - {fields['leave_vill']}" if fields["leave_vill"] else "Vill & PO -",
            f"Teh - {fields['leave_teh']}" if fields["leave_teh"] else "Teh -",
            f"Dist - {fields['leave_dist']}" if fields["leave_dist"] else "Dist -",
            f"State - {fields['leave_state']}" if fields["leave_state"] else "State -",
            f"Pin - {fields['leave_pin']}" if fields["leave_pin"] else "",
        ]))
        blocks = {"body_paras": body_paras}

    else:
        # invitation_letter and any other blueprint doc types
        fields = {
            "file_reference_number": slots.get("file_reference_number") or ref_text,
            "date":                  slots.get("date") or date_text,
            "subject":               subj_text or slots.get("subject") or "",
            "addressee_1":           slots.get("addressee_1") or (recv_lines[0] if recv_lines else ""),
            "addressee_2":           slots.get("addressee_2") or (recv_lines[1] if len(recv_lines) > 1 else ""),
            "signatory_name":        slots.get("signatory_name") or (sign_lines[0] if sign_lines else ""),
            "signatory_designation": slots.get("signatory_designation") or (sign_lines[1] if len(sign_lines) > 1 else ""),
        }
        blocks = {"body_paras": body_paras}

    return {
        "doc_type": doc_type,
        "fields":   fields,
        "blocks":   blocks,
        "lists":    lists,
        "meta":     {"source": "blueprint"},
    }


def doc_state_from_filled_skeleton(filled: dict, doc_type: str) -> dict:
    if _is_blueprint_structured(filled):
        return _doc_state_from_blueprint(filled, doc_type)

    sections = filled.get("sections", []) or []
    by_type = {s.get("type"): s for s in sections if isinstance(s, dict)}

    fields: dict = {}
    blocks: dict = {}
    lists: dict = {}

    # Subject
    subj = by_type.get("subject") or {}
    fields["subject"] = _section_text(subj)

    # Body paragraphs — items may use plain text or Lexical JSON per item
    body = by_type.get("numbered_paragraphs") or {}
    body_content = body.get("content") or {}
    items = body_content.get("items") or []

    def _para_text(it: dict) -> str:
        plain = it.get("text") or ""
        if plain:
            return str(plain)
        richtext = (it.get("richtext") or it.get("content", {}).get("richtext") or {})
        state = richtext.get("state")
        if isinstance(state, dict):
            return lexical_to_plain_text(state)
        return ""

    paras = [_para_text(it) for it in items if _para_text(it).strip()]
    # Map to different block keys depending on doctype/templates
    if doc_type == "MOVEMENT_ORDER":
        blocks["order_paras"] = paras          # kept for legacy templates
        blocks.setdefault("distribution_lines", [])

        # Map _slots to individual template variables for v2 template
        slot_fields = filled.get("_slots") or {}
        for k in (
            "army_no", "rank", "person_name", "unit", "att_unit",
            "destination", "departure_date", "departure_time",
            "route", "destination_desc", "remarks", "distr_unit",
        ):
            fields.setdefault(k, slot_fields.get(k, ""))
    elif doc_type == "DO_LETTER":
        blocks["body_paras"] = paras

        # --- DO_LETTER: map skeleton sections → template variables ---
        slot_fields = filled.get("_slots") or {}

        # Reference number (top-left header).
        # ONLY use the regex-slot value (explicit "DO No" / "Ref No" in prompt).
        # The LLM-filled ref_val is unreliable (1B model hallucinates numbers like "311").
        fields["file_reference_number"] = slot_fields.get("file_reference_number") or ""

        # Date (top-right header)
        date_sec = by_type.get("date") or {}
        date_val = ((date_sec.get("content") or {}).get("value") or "")
        fields["date"] = slot_fields.get("date") or date_val

        # Addressee / receiver block.
        # Prefer regex-slot extraction (reliable). Only fall back to skeleton recv_lines
        # when the slot found something (acts as confirmation), otherwise leave empty.
        # This prevents LLM hallucinated receiver lines from bleeding into the template.
        recv_sec = by_type.get("receiver_block") or {}
        recv_c = recv_sec.get("content") or {}
        recv_lines = recv_c.get("lines") or []
        a1_slot = slot_fields.get("addressee_1", "")
        a2_slot = slot_fields.get("addressee_2", "")
        if a1_slot:
            # Slot found an addressee — use it; supplement with line[1] from skeleton
            fields["addressee_1"] = a1_slot
            fields["addressee_2"] = a2_slot or (recv_lines[1] if len(recv_lines) > 1 else "")
        else:
            # No slot addressee — leave blank rather than show LLM-hallucinated names
            fields["addressee_1"] = ""
            fields["addressee_2"] = ""

        # Subject is already set at the top via fields["subject"] = subj.get("content").text
        # but override with slot if present
        if slot_fields.get("subject"):
            fields["subject"] = slot_fields["subject"]

        # Salutation — default "My dear Sir," if not provided
        fields["salutation"] = slot_fields.get("salutation") or "My dear Sir,"

        # Signatory block
        sig_sec = by_type.get("signee_block") or {}
        sig_c = sig_sec.get("content") or {}
        fields["signatory_name"] = (
            slot_fields.get("signatory_name") or sig_c.get("signer_name") or ""
        )
        fields["signatory_designation"] = (
            slot_fields.get("signatory_designation")
            or sig_c.get("rank_or_title") or sig_c.get("appointment") or ""
        )
        fields["signatory_dept"] = (
            slot_fields.get("signatory_dept") or sig_c.get("organization") or ""
        )

        # Copy-to list (stored as a list in blocks so Jinja2 for loop works)
        copy_to = slot_fields.get("copy_to_list") or []
        blocks["copy_to_list"] = copy_to if isinstance(copy_to, list) else []
    elif doc_type == "GOI_LETTER":
        blocks["body_paras"] = paras

        slot_fields = filled.get("_slots") or {}

        # Reference number — only from regex slot (LLM hallucinates numbers)
        fields["file_reference_number"] = slot_fields.get("file_reference_number") or ""

        # Date — only from regex slot (LLM hallucinates ISO dates like "2026-02-15")
        fields["date_gregorian"] = slot_fields.get("date_gregorian") or ""
        fields["date_indian"] = slot_fields.get("date_indian") or ""

        # Sender / ministry info (often not in prompt — left blank)
        fields["ministry_name"] = slot_fields.get("ministry_name") or ""
        fields["address_line_1"] = slot_fields.get("address_line_1") or ""
        fields["address_line_2"] = slot_fields.get("address_line_2") or ""
        fields["telephone"] = slot_fields.get("telephone") or ""
        fields["email"] = slot_fields.get("email") or ""

        # Addressee — slot preferred; blank if not found (no hallucination)
        recv_sec = by_type.get("receiver_block") or {}
        recv_c = recv_sec.get("content") or {}
        recv_lines = recv_c.get("lines") or []
        a1_slot = slot_fields.get("addressee_1", "")
        a2_slot = slot_fields.get("addressee_2", "")
        if a1_slot:
            fields["addressee_1"] = a1_slot
            fields["addressee_2"] = a2_slot or (recv_lines[1] if len(recv_lines) > 1 else "")
        else:
            fields["addressee_1"] = ""
            fields["addressee_2"] = ""

        # Subject — override with slot if present
        if slot_fields.get("subject"):
            fields["subject"] = slot_fields["subject"]

        # Signatory (GOI template has designation + dept, no separate name field)
        sig_sec = by_type.get("signee_block") or {}
        sig_c = sig_sec.get("content") or {}
        fields["signatory_designation"] = (
            slot_fields.get("signatory_designation")
            or sig_c.get("rank_or_title") or sig_c.get("appointment") or ""
        )
        fields["signatory_dept"] = (
            slot_fields.get("signatory_dept") or sig_c.get("organization") or ""
        )

        # Copy-to list
        copy_to = slot_fields.get("copy_to_list") or []
        blocks["copy_to_list"] = copy_to if isinstance(copy_to, list) else []
    else:
        blocks["body_paras"] = paras

    # --- LEAVE_CERTIFICATE: map specific template variables ---
    if doc_type == "LEAVE_CERTIFICATE":
        # Date section — read now but slots value takes priority (set below)
        date_sec = by_type.get("date") or {}
        _date_from_section = ((date_sec.get("content") or {}).get("value") or "")

        # Slot fields — extracted at fill time via fast regex, stored under _slots.
        # Slots are preferred over LLM-filled skeleton sections (more reliable).
        slot_fields = filled.get("_slots") or {}

        # army_no: prefer regex slot (exact copy from prompt) over LLM-filled ref section
        ref_sec = by_type.get("reference_number") or {}
        ref_val = ((ref_sec.get("content") or {}).get("value") or "")
        fields["army_no"] = slot_fields.get("army_no") or ref_val

        # date: prefer slot date (original format "08 Sep 2025") over LLM-filled section
        fields["date"] = slot_fields.get("date") or _date_from_section

        # Signee block → template {{ signatory_name }}, {{ signatory_designation }}
        # Prefer slot signatory fields; fall back to skeleton signee_block
        sig_sec = by_type.get("signee_block") or {}
        sig_c = sig_sec.get("content") or {}
        fields["signatory_name"] = (
            slot_fields.get("signatory_name")
            or sig_c.get("signer_name") or ""
        )
        fields["signatory_designation"] = (
            slot_fields.get("signatory_designation")
            or sig_c.get("rank_or_title") or sig_c.get("appointment") or ""
        )

        # Direct 1-to-1 slot → template variable mappings
        for k in (
            "person_name", "rank", "unit", "att_unit",
            "leave_type", "days", "from_date", "to_date",
            "leave_vill", "leave_teh", "leave_dist", "leave_state", "leave_pin",
            "contact_no", "station",
        ):
            fields.setdefault(k, slot_fields.get(k, ""))

        # Build prefix_suffix sentence from individual prefix/suffix dates
        prefix_date = slot_fields.get("prefix_date", "")
        suffix_date = slot_fields.get("suffix_date", "")
        if prefix_date and suffix_date:
            fields["prefix_suffix"] = (
                f" with permission to prefix on {prefix_date}"
                f" and suffix on {suffix_date} being Holiday/ Sunday"
            )
        elif prefix_date:
            fields["prefix_suffix"] = f" with permission to prefix on {prefix_date}"
        elif suffix_date:
            fields["prefix_suffix"] = (
                f" with permission to suffix on {suffix_date} being Holiday/ Sunday"
            )
        else:
            fields["prefix_suffix"] = ""

        # Build combined leave_address block expected by template {{ leave_address }}
        fields["leave_address"] = "\n".join(filter(None, [
            f"Vill & PO - {fields.get('leave_vill', '')}" if fields.get("leave_vill") else "Vill & PO -",
            f"Teh - {fields.get('leave_teh', '')}" if fields.get("leave_teh") else "Teh -",
            f"Dist - {fields.get('leave_dist', '')}" if fields.get("leave_dist") else "Dist -",
            f"State - {fields.get('leave_state', '')}" if fields.get("leave_state") else "State -",
            f"Pin - {fields.get('leave_pin', '')}" if fields.get("leave_pin") else "",
        ]))

    return {
        "doc_type": doc_type,
        "fields": fields,
        "blocks": blocks,
        "lists": lists,
        "meta": {"source": "filled_skeleton"},
    }
