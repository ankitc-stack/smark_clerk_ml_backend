"""
app/services/action_bridge.py

Bridge: ML pipeline ActionObject  →  Doc-Engine ActionObject

The ML pipeline (intent extractor) produces ActionObjects with the schema:
    { action, scope, target: {section_id, para_id, para_index},
      params: {tone, style_params, ...}, content, confidence, ... }

The Doc-Engine command endpoint accepts structural ops only:
    { action, target_type, target_ref, position, ai_instruction }

Content ops (REWRITE, EXPAND, SHORTEN, CHANGE_TONE) are handled entirely by the
ML pipeline: the LLM generates new text, which is then applied via PATCH /sections.
The doc-engine command endpoint cannot run LLM content generation (Phase 3 is the
ML pipeline's responsibility in this microservice architecture).
"""
from __future__ import annotations

from app.schemas import ActionObject, CommandAction

# Structural ML actions → doc-engine action names (command endpoint)
_DIRECT_MAP: dict[str, str] = {
    CommandAction.ADD_PARAGRAPH.value:    "ADD_PARAGRAPH",
    CommandAction.REMOVE_PARAGRAPH.value: "DELETE_SECTION",
    CommandAction.INSERT_SECTION.value:   "INSERT_SECTION",
    CommandAction.DELETE_SECTION.value:   "DELETE_SECTION",
    CommandAction.MOVE_SECTION.value:     "MOVE_SECTION",
}

# Actions handled locally by the ML pipeline (not forwarded to doc-engine /command)
BYPASS_ACTIONS: frozenset[str] = frozenset({
    CommandAction.SET_FORMAT.value,
    CommandAction.REPLACE_TEXT.value,
    CommandAction.INSERT_TEXT.value,
    CommandAction.DELETE_TEXT.value,
    CommandAction.UNDO.value,
    CommandAction.REWRITE_CONTENT.value,
    CommandAction.EXPAND_CONTENT.value,
    CommandAction.SHORTEN_CONTENT.value,
    CommandAction.CHANGE_TONE.value,
    CommandAction.FIX_GRAMMAR.value,
})

# Content ops: require ML LLM transform then PATCH /sections (not doc-engine /command)
CONTENT_OPS: frozenset[str] = frozenset({
    CommandAction.REWRITE_CONTENT.value,
    CommandAction.EXPAND_CONTENT.value,
    CommandAction.SHORTEN_CONTENT.value,
    CommandAction.CHANGE_TONE.value,
    CommandAction.FIX_GRAMMAR.value,
})


def ml_action_to_de(action_obj: ActionObject, doc_sections: list[dict] | None = None) -> dict | None:
    """
    Convert an ML pipeline ActionObject to a doc-engine ActionObject dict.

    Returns None if the action should be handled locally (SET_FORMAT, UNDO, etc.)
    rather than forwarded to the doc-engine.

    Args:
        action_obj:    The validated ActionObject from the ML intent extractor.
        doc_sections:  Optional list of section dicts from doc state — used to
                       look up section type from section_id for target_type.

    Returns:
        dict suitable for DocEngineClient.apply_command(..., action_obj=...) or None.
    """
    # CommandAction is a str-enum; use .value to get the raw string "ADD_PARAGRAPH" etc.
    action_str = action_obj.action.value if hasattr(action_obj.action, "value") else str(action_obj.action)

    if action_str in BYPASS_ACTIONS:
        return None

    de_action = _DIRECT_MAP.get(action_str)
    if de_action is None:
        return None

    # --- target_type: section type from section_id ---
    target_type: str | None = None
    if doc_sections and action_obj.target.section_id:
        sec = next(
            (s for s in doc_sections if s.get("id") == action_obj.target.section_id),
            None,
        )
        if sec:
            target_type = sec.get("type")
    # ADD_PARAGRAPH always inserts a paragraph section; default if LLM didn't provide section_id
    if target_type is None and action_str == CommandAction.ADD_PARAGRAPH.value:
        target_type = "paragraph"

    # --- target_ref: paragraph reference string ---
    # For MOVE_SECTION: target_ref identifies the SOURCE section (what to move).
    # The doc-engine expects a natural-language phrase like "paragraph 2", not a UUID.
    # We compute it from section_id's position among same-type sections in doc_sections.
    target_ref: str | None = None
    # For MOVE_SECTION and DELETE_SECTION: convert section_id → "type N" phrase
    # that doc-engine's section_resolver.resolve_id() can parse.
    if action_str in (CommandAction.MOVE_SECTION.value, CommandAction.DELETE_SECTION.value) \
            and doc_sections and action_obj.target.section_id:
        src_type = next(
            (s.get("type") for s in doc_sections if s.get("id") == action_obj.target.section_id),
            None,
        )
        if src_type:
            same_type = [s for s in doc_sections if s.get("type") == src_type]
            for i, s in enumerate(same_type):
                if s.get("id") == action_obj.target.section_id:
                    target_ref = f"{src_type.replace('_', ' ')} {i + 1}"
                    break
    if target_ref is None:
        if action_obj.target.para_id:
            # para_id like "p1", "p2" → "paragraph 1", "paragraph 2"
            pid = str(action_obj.target.para_id)
            if pid.startswith("p") and pid[1:].isdigit():
                target_ref = f"paragraph {pid[1:]}"
            else:
                target_ref = pid
        elif action_obj.target.para_index is not None:
            target_ref = f"paragraph {action_obj.target.para_index + 1}"

    # --- position: for MOVE_SECTION, INSERT_SECTION, ADD_PARAGRAPH ---
    position: dict | None = None
    if action_str in (CommandAction.MOVE_SECTION, CommandAction.INSERT_SECTION, CommandAction.ADD_PARAGRAPH):
        dest_id = action_obj.target.para_id  # anchor UUID or virtual "pN" ID
        # Track whether para_id was a virtual "pN" reference BEFORE resolving it to a UUID.
        # "pN" means the user/resolver specified a concrete paragraph number (e.g. "after para 2").
        # A raw UUID means command_contract used the last-paragraph fallback — not a specific anchor.
        _orig_para_id = dest_id
        _para_id_is_virtual = (
            _orig_para_id
            and str(_orig_para_id).startswith("p")
            and str(_orig_para_id)[1:].isdigit()
        )
        # Resolve virtual "pN" ID → actual section UUID using live doc_sections.
        if dest_id and doc_sections and str(dest_id).startswith("p") and str(dest_id)[1:].isdigit():
            _para_num = int(str(dest_id)[1:])
            _para_secs = [s for s in doc_sections if isinstance(s, dict) and s.get("type") == "paragraph"]
            if 0 < _para_num <= len(_para_secs):
                dest_id = _para_secs[_para_num - 1].get("id") or dest_id
        idx = action_obj.target.para_index   # 0=before, 1=after
        if action_str == CommandAction.ADD_PARAGRAPH.value:
            if _para_id_is_virtual and dest_id:
                # Specific "add after paragraph N" — honour the requested anchor.
                # target_ref stays as already set from the "pN" → "paragraph N" conversion above.
                position = {"policy": "after", "section_id": dest_id}
            elif doc_sections:
                # No specific anchor (or raw UUID fallback) — append after last paragraph
                # so we never violate blueprint order or produce a UUID target_ref that
                # doc-engine cannot parse.
                _para_secs = [s for s in doc_sections if isinstance(s, dict) and s.get("type") == "paragraph"]
                if _para_secs:
                    _last_idx = len(_para_secs)
                    target_ref = f"paragraph {_last_idx}"
                    position = {"policy": "after", "section_id": _para_secs[-1].get("id")}
                else:
                    # No paragraphs yet — insert before signee_block if present
                    _signee = next(
                        (s for s in doc_sections if isinstance(s, dict) and s.get("type") == "signee_block"),
                        None,
                    )
                    if _signee:
                        position = {"policy": "before", "section_id": _signee.get("id")}
                    else:
                        position = {"policy": "end"}
            else:
                position = {"policy": "end"}
        elif dest_id:
            policy = "after" if idx == 1 else "before"
            position = {"policy": policy, "section_id": dest_id}
        else:
            position = {"policy": "end"}

    # --- ai_instruction: tone hint, shorten hint, or free text ---
    ai_instruction: str | None = None
    tone = action_obj.params.tone
    if tone:
        ai_instruction = f"tone={tone}"
    if action_str == CommandAction.SHORTEN_CONTENT:
        ai_instruction = (ai_instruction or "") + " shorten"
    if action_obj.content:
        ai_instruction = (ai_instruction + " " if ai_instruction else "") + action_obj.content

    return {
        "action": de_action,
        "target_type": target_type,
        "target_ref": target_ref,
        "position": position,
        "ai_instruction": ai_instruction or None,
    }
