from __future__ import annotations

import re
import uuid
from typing import Any

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
from app.services.content_transform import TransformError, apply_transform


# JSON Schema is kept as a constant for downstream consumers that need a strict contract artifact.
ACTION_OBJECT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ActionObjectV1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action",
        "scope",
        "target",
        "params",
        "content",
        "confidence",
        "needs_clarification",
        "clarification",
    ],
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "REWRITE_CONTENT",
                "EXPAND_CONTENT",
                "SHORTEN_CONTENT",
                "CHANGE_TONE",
                "REPLACE_TEXT",
                "INSERT_TEXT",
                "DELETE_TEXT",
                "INSERT_SECTION",
                "DELETE_SECTION",
                "MOVE_SECTION",
                "ADD_PARAGRAPH",
                "REMOVE_PARAGRAPH",
                "SET_FORMAT",
                "UNDO",
            ],
        },
        "scope": {"type": "string", "enum": ["PARAGRAPH", "SECTION", "SELECTION", "DOCUMENT"]},
        "target": {
            "type": "object",
            "additionalProperties": False,
            "required": ["section_id", "para_id", "para_index"],
            "properties": {
                "section_id": {"type": ["string", "null"]},
                "para_id": {"type": ["string", "null"]},
                "para_index": {"type": ["integer", "null"], "minimum": 0},
            },
        },
        "params": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tone", "preserve_numbering", "preserve_style"],
            "properties": {
                "tone": {"type": ["string", "null"], "enum": ["formal", "concise", "neutral", None]},
                "preserve_numbering": {"type": "boolean"},
                "preserve_style": {"type": "boolean"},
            },
        },
        "content": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_clarification": {"type": "boolean"},
        "clarification": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["question", "options"],
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["label", "token"],
                        "properties": {
                            "label": {"type": "string"},
                            "token": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


class IntentParseError(ValueError):
    pass


class PlannerError(ValueError):
    pass


class NeedsClarificationError(PlannerError):
    def __init__(self, question: str, options: list[ClarificationOption] | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []


def _sections(structured: dict) -> list[dict]:
    return [s for s in (structured.get("sections") or []) if isinstance(s, dict)]


def _find_section_by_id(structured: dict, section_id: str | None) -> dict | None:
    if not section_id:
        return None
    for sec in _sections(structured):
        if sec.get("id") == section_id:
            return sec
    return None


def _paragraph_items(section: dict | None) -> list[dict]:
    if not isinstance(section, dict):
        return []
    return [i for i in (((section.get("content") or {}).get("items")) or []) if isinstance(i, dict)]


def resolve_cursor_paragraph(
    structured: dict,
    section_id: str | None,
    cursor_position: int | None,
) -> tuple[str | None, int | None]:
    """
    Deterministically map cursor_position to a paragraph in the current section.

    Cursor interpretation (safe order):
    1) 1-based paragraph index when value in [1, item_count]
    2) 0-based paragraph index when value in [0, item_count-1]
    """
    if cursor_position is None or section_id is None:
        return None, None
    section = _find_section_by_id(structured, section_id)
    if section is None or section.get("type") != "numbered_paragraphs":
        return None, None
    items = _paragraph_items(section)
    if not items:
        return None, None

    idx: int | None = None
    pos = int(cursor_position)
    if 1 <= pos <= len(items):
        idx = pos - 1
    elif 0 <= pos < len(items):
        idx = pos
    if idx is None:
        return None, None

    para_id = str(items[idx].get("id") or "").strip() or None
    if not para_id:
        return None, None
    return para_id, idx


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", (text or "").lower())


def _levenshtein_distance(a: str, b: str, cutoff: int) -> int:
    """
    Bounded Levenshtein distance.

    Returns cutoff+1 when distance is known to exceed cutoff.
    """
    if a == b:
        return 0
        
    if abs(len(a) - len(b)) > cutoff:
        return cutoff + 1

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        min_row = i
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            replace = prev[j - 1] + (0 if ca == cb else 1)
            val = min(ins, delete, replace)
            cur.append(val)
            if val < min_row:
                min_row = val
        if min_row > cutoff:
            return cutoff + 1
        prev = cur
    return prev[-1]


def _looks_like(token: str, word: str, max_distance: int = 1) -> bool:
    t = (token or "").lower()
    w = (word or "").lower()
    if not t or not w:
        return False
    if t == w:
        return True
    # Avoid noisy fuzzy matches for very short tokens.
    if len(t) <= 2 or len(w) <= 2:
        return False
    return _levenshtein_distance(t, w, cutoff=max_distance) <= max_distance


def _contains_any_like(tokens: list[str], words: set[str], max_distance: int = 1) -> bool:
    for token in tokens:
        for word in words:
            if _looks_like(token, word, max_distance=max_distance):
                return True
    return False


_PARA_REF_LAST = -1  # sentinel: "last paragraph"


def _parse_paragraph_ref(text: str) -> int | None:
    # -1 sentinel means "last paragraph"
    if re.search(r"\blast\s+(?:paragraph|para)\b|\b(?:paragraph|para)\s+last\b", text):
        return _PARA_REF_LAST

    # Ordinal words: "second paragraph", "third para", "my second paragraph", etc.
    _ORDINALS = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    }
    for word, num in _ORDINALS.items():
        if re.search(
            rf"\b{word}\s+(?:paragraph|para)\b|\b(?:paragraph|para)\s+{word}\b",
            text,
            re.IGNORECASE,
        ):
            return num

    # The regex supports "paragraph 2", "para 2", and compact "p2" references.
    patterns = [
        r"\bparagraph\s+(\d+)\b",
        r"\bpara\s+(\d+)\b",
        r"\bp(\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))

    tokens = _tokenize(text)
    paragraph_words = {"paragraph", "para", "paragrph", "paragraf", "pargraph"}
    for idx, token in enumerate(tokens):
        if not _contains_any_like([token], paragraph_words, max_distance=2):
            continue
        if idx + 1 < len(tokens) and tokens[idx + 1].isdigit():
            return int(tokens[idx + 1])
    for token in tokens:
        compact = re.fullmatch(r"p(\d+)", token)
        if compact:
            return int(compact.group(1))
    return None


def _canonicalize_section_ref(raw_ref: str) -> str | None:
    cleaned = str(raw_ref or "").strip().lower()
    if not cleaned:
        return None

    # Canonical existing IDs.
    canonical_id = re.fullmatch(r"sec_[a-z]+_\d+", cleaned)
    if canonical_id:
        return cleaned

    # Aliases:
    # - "body 1"
    # - "body_1"
    # - "body-1"
    # - "sec body 1"
    alias = re.fullmatch(r"(?:sec[\s_-]*)?body[\s_-]*(\d+)", cleaned)
    if alias:
        return f"sec_body_{int(alias.group(1)):03d}"

    return None


def _parse_section_refs(text: str) -> list[str]:
    # Parse in textual order to preserve source/destination mapping for move-section commands.
    found: list[tuple[int, str]] = []

    # Examples:
    # - "section sec_body_001"
    # - "section body 1"
    explicit_pattern = re.compile(
        r"\b(?:section|seciton|secion|secton|sec)\s+([a-zA-Z0-9_ -]+?)(?=\s+(?:after|before|and|to|near|with)\b|[,.]|$)",
        flags=re.IGNORECASE,
    )
    for match in explicit_pattern.finditer(text):
        raw_ref = str(match.group(1) or "").strip()
        canonical = _canonicalize_section_ref(raw_ref)
        if canonical:
            found.append((match.start(), canonical))
        elif raw_ref:
            found.append((match.start(), raw_ref))

    # Standalone alias references:
    # - "body 1"
    for match in re.finditer(r"\bbody[\s_-]*(\d+)\b", text, flags=re.IGNORECASE):
        canonical = f"sec_body_{int(match.group(1)):03d}"
        found.append((match.start(), canonical))

    for match in re.finditer(r"\bsec_[a-z]+_\d+\b", text, flags=re.IGNORECASE):
        found.append((match.start(), str(match.group(0)).lower()))

    out: list[str] = []
    for _, ref in sorted(found, key=lambda item: item[0]):
        cleaned = str(ref or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


_SECTION_TYPE_KEYWORDS: dict[str, list[str]] = {
    "subject":          ["subject", "title", "heading"],
    "date":             ["date", "dated"],
    "signee_block":     ["signee", "signatory", "signature", "sign", "signee block", "hastakshar"],
    "receiver_block":   ["receiver", "address", "addressee", "receiver block", "praaptak"],
    "reference_number": ["ref", "reference", "army no", "army number"],
}

# Subset of _SECTION_TYPE_KEYWORDS used for direct content-set detection.
# Excludes short/ambiguous tokens ("sign", "ref", "date") that appear in unrelated phrases.
_CONTENT_SET_KEYWORDS: dict[str, list[str]] = {
    "signee_block":     ["signee block", "signee", "signatory", "signature"],
    "subject":          ["subject", "title", "heading"],
    "date":             ["date"],
    "receiver_block":   ["receiver block", "receiver", "addressee"],
    "reference_number": ["reference number", "ref number", "army no", "army number", "ref no"],
}

_CONTENT_SET_VERBS_RE = re.compile(
    r"^(?:add|set|update|change|write|put|enter|fill(?:\s+in)?)\s+",
    re.IGNORECASE,
)
# Words that indicate the user is using the section keyword as a verb/adjective, not a target
_CONTENT_SET_STOP_STARTS = frozenset({
    "off", "out", "up", "in", "on", "to", "at", "by", "of", "for", "from", "with",
    "the", "a", "an", "this", "that", "my", "your",
    # "block" is part of a section type name (signee block, receiver block), not content.
    "block",
})


# ── Table-add detection ─────────────────────────────────────────────────────
_TABLE_ADD_RE = re.compile(
    r"\badd\b.*?\btable\b|\binsert\b.*?\btable\b|\bcreate\b.*?\btable\b",
    re.IGNORECASE,
)
_TABLE_DIMS_RE = re.compile(r"\b(\d{1,2})\s*[xX×*]\s*(\d{1,2})\b")
# "between para 2 & 3" / "between paragraph 2 and 3" → capture first index
_TABLE_BETWEEN_RE = re.compile(
    r"\bbetween\b.*?\bpara(?:graph)?\s+(\d+)\b",
    re.IGNORECASE,
)
# "at last" / "at the end" / "at end" / "at bottom"
_TABLE_LAST_RE = re.compile(
    r"\bat\s+(?:the\s+)?(?:last|end|bottom)\b",
    re.IGNORECASE,
)


def _parse_table_dims(prompt: str) -> tuple[int, int] | None:
    """Extract NxM from prompt. Returns (rows, cols) or None if not found."""
    m = _TABLE_DIMS_RE.search(prompt)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _parse_table_position(prompt: str) -> str:
    """Return position hint string for __create:table_block signal.

    Returns:
        "after_para:N"  — insert after paragraph N (1-based)
        "after_last"    — insert after all body content
        ""              — default (after last paragraph)
    """
    m = _TABLE_BETWEEN_RE.search(prompt)
    if m:
        return f"after_para:{m.group(1)}"
    if _TABLE_LAST_RE.search(prompt):
        return "after_last"
    return ""


def _try_section_content_set(prompt: str, structured: dict) -> "ActionObject | None":
    """
    Detect "add/set/update [section_keyword] [content]" pattern and return a
    pre-filled REWRITE_CONTENT ActionObject that bypasses the LLM.

    Examples:
      "add signee Brig RS Sharma"      → signee_block, content="Brig RS Sharma"
      "set subject Budget Allocation"  → subject, content="Budget Allocation"
      "change date 15 Mar 2026"        → date, content="15 Mar 2026"

    Returns None when the pattern does not match or content is missing.
    """
    m = _CONTENT_SET_VERBS_RE.match(prompt)
    if not m:
        return None
    after_verb = prompt[m.end():]

    # Try keywords longest-first to prefer "signee block" over "signee"
    for sec_type, keywords in _CONTENT_SET_KEYWORDS.items():
        for kw in sorted(keywords, key=len, reverse=True):
            if after_verb.lower().startswith(kw.lower()):
                # Verify it's a word boundary after the keyword
                rest = after_verb[len(kw):]
                if rest and rest[0].isalpha():
                    continue  # e.g. "subject matter" should not match "subject" kw
                content_raw = rest.strip().lstrip(":").strip()
                if not content_raw:
                    continue  # no content given — don't intercept
                # Reject if content starts with a stop word (e.g. "add sign off")
                first_word = content_raw.lower().split()[0]
                if first_word in _CONTENT_SET_STOP_STARTS:
                    continue
                # Find existing section in document
                sec = next(
                    (s for s in _sections(structured) if s.get("type") == sec_type),
                    None,
                )
                if sec:
                    return ActionObject(
                        action=CommandAction.REWRITE_CONTENT,
                        scope=CommandScope.SECTION,
                        target=ActionTarget(section_id=sec.get("id"), para_id=None, para_index=None),
                        params=ActionParams(tone=None, preserve_numbering=False, preserve_style=True),
                        content=content_raw,
                        confidence=0.93,
                        needs_clarification=False,
                        clarification=None,
                    )
                else:
                    # Section doesn't exist yet — signal main.py to INSERT then PATCH
                    return ActionObject(
                        action=CommandAction.REWRITE_CONTENT,
                        scope=CommandScope.SECTION,
                        target=ActionTarget(section_id=None, para_id=f"__create:{sec_type}", para_index=None),
                        params=ActionParams(tone=None, preserve_numbering=False, preserve_style=True),
                        content=content_raw,
                        confidence=0.93,
                        needs_clarification=False,
                        clarification=None,
                    )
    return None


def _resolve_section_by_keyword(text: str, structured: dict) -> dict | None:
    tokens = text.lower().split()
    for sec_type, keywords in _SECTION_TYPE_KEYWORDS.items():
        if any(kw in text.lower() for kw in keywords):
            match = next(
                (s for s in _sections(structured) if s.get("type") == sec_type),
                None,
            )
            if match:
                return match
    return None


def _resolve_section_from_prompt(text: str, context: CommandContext, structured: dict) -> dict | None:
    # Resolver default #1: explicit section mention wins.
    for section_ref in _parse_section_refs(text):
        sec = _find_section_by_id(structured, section_ref)
        if sec:
            return sec

    # Resolver default #1b: named section keyword (subject, date, signee, …).
    sec = _resolve_section_by_keyword(text, structured)
    if sec:
        return sec

    # Resolver default #2: fallback to current_section_id from request context.
    sec = _find_section_by_id(structured, context.current_section_id)
    if sec:
        return sec

    return None


def _build_clarification(question: str, items: list[dict]) -> ActionClarification:
    def _preview_text(item: dict, max_chars: int = 60) -> str:
        raw = " ".join(str(item.get("text") or "").split())
        if not raw:
            return ""
        trimmed = raw[: max_chars - 3].rstrip() + "..." if len(raw) > max_chars else raw
        return trimmed.replace('"', "'")

    options = []
    for idx, item in enumerate(items, start=1):
        para_id = str(item.get("id") or "").strip()
        if not para_id:
            continue
        preview = _preview_text(item)
        label = f'Para {idx}: "{preview}"' if preview else f"Para {idx}"
        options.append(ClarificationOption(label=label, token=para_id))
    return ActionClarification(question=question, options=options)


def _clarification_options_for_section(structured: dict, section_id: str | None) -> list[ClarificationOption]:
    sec = _find_section_by_id(structured, section_id)
    if not sec:
        return []
    items = _paragraph_items(sec)
    def _preview_text(item: dict, max_chars: int = 60) -> str:
        raw = " ".join(str(item.get("text") or "").split())
        if not raw:
            return ""
        trimmed = raw[: max_chars - 3].rstrip() + "..." if len(raw) > max_chars else raw
        return trimmed.replace('"', "'")

    options = []
    for idx, item in enumerate(items, start=1):
        para_id = str(item.get("id") or "").strip()
        if para_id:
            preview = _preview_text(item)
            label = f'Para {idx}: "{preview}"' if preview else f"Para {idx}"
            options.append(ClarificationOption(label=label, token=para_id))
    return options


def _clarification_options_for_document_sections(structured: dict) -> list[ClarificationOption]:
    options: list[ClarificationOption] = []
    for idx, section in enumerate(_sections(structured), start=1):
        section_id = str(section.get("id") or "").strip()
        section_type = str(section.get("type") or "").strip()
        if not section_id:
            continue
        suffix = f" ({section_type})" if section_type else ""
        options.append(ClarificationOption(label=f"Section {idx}: {section_id}{suffix}", token=section_id))
    return options


def _clarify_action(
    action: CommandAction,
    scope: CommandScope,
    question: str,
    confidence: float,
    options: list[ClarificationOption],
    section_id: str | None = None,
) -> ActionObject:
    return ActionObject(
        action=action,
        scope=scope,
        target=ActionTarget(section_id=section_id, para_id=None, para_index=None),
        params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True),
        content=None,
        confidence=confidence,
        needs_clarification=True,
        clarification=ActionClarification(question=question, options=options),
    )


def _detect_action(prompt: str) -> tuple[CommandAction, ToneValue | None]:
    p = (prompt or "").lower()
    tokens = _tokenize(p)

    # Format detection runs FIRST so exact format keywords ("bold", "italic", "highlight",
    # "color", "font", "underline") always win over fuzzy-matched structural action words.
    # This prevents "make ... bold" from fuzzy-matching "move ... body" → MOVE_SECTION.
    _fmt_bold  = _contains_any_like(tokens, {"bold"}, max_distance=1)
    _fmt_ital  = _contains_any_like(tokens, {"italic", "italicize"}, max_distance=1)
    _fmt_uline = _contains_any_like(tokens, {"underline"}, max_distance=1)
    _fmt_hilit = _contains_any_like(tokens, {"highlight"}, max_distance=1)
    _fmt_font  = bool(re.search(r"\bfont\s+(?!size\b)(?:to\s+)?[a-z]+\b|\buse\s+[a-z]+\s+font\b", p))
    _fmt_color = bool(re.search(
        r"\b(?:color|colour)\s+(?:to\s+)?[a-z]+\b"
        r"|\b(?:red|blue|green|black|white|gray|yellow|orange|purple)\s+(?:text|colou?r)\b", p))
    _fmt_size  = bool(re.search(r"\bfont\s+size\s+(?:to\s+)?\d+\b|\bsize\s+(?:to\s+)?\d+\b|\b\d+\s*pt\b", p))
    if _fmt_bold or _fmt_ital or _fmt_uline or _fmt_hilit or _fmt_font or _fmt_color or _fmt_size:
        # Exception: "add/insert [new/another] paragraph" wins even when format words are present.
        # e.g. "add another bold para about X" → ADD_PARAGRAPH (bold is a content hint, not a format cmd)
        _has_add_s = _contains_any_like(tokens, {"add", "insert", "append"}, max_distance=1)
        _has_para_s = _contains_any_like(tokens, {"paragraph", "para", "paragrph", "paragraf"}, max_distance=2)
        _has_new_s = _contains_any_like(tokens, {"new", "another"}, max_distance=1)
        if _has_add_s and _has_para_s and _has_new_s:
            return CommandAction.ADD_PARAGRAPH, None
        return CommandAction.SET_FORMAT, None

    # UNDO detection — runs before structural checks to avoid interference.
    _undo_phrase = bool(re.search(r"\bgo\s+back\b|\bundo\s+(last|that|this)\b", p))
    if _contains_any_like(tokens, {"undo", "revert", "rollback", "restore"}, max_distance=1) or _undo_phrase:
        return CommandAction.UNDO, None

    move_words = {"move", "mvoe", "reorder", "shift"}
    remove_words = {"remove", "rmove", "rmv"}
    delete_words = {"delete", "delet"}
    add_words = {"add", "insert", "append"}
    section_words = {"section", "seciton", "secion", "secton", "sec", "body"}
    paragraph_words = {"paragraph", "para", "paragrph", "paragraf", "pargraph"}
    expand_words = {"expand", "expnad", "elaborate"}
    shorten_words = {"shorten", "shortn", "shroten", "concise", "brief"}
    rewrite_words = {"rewrite", "rewirte", "rephrase", "paraphrase"}
    formal_words = {"formal", "formel", "frmol"}
    neutral_words = {"neutral", "nutral"}
    friendly_words = {"friendly", "casual", "informal", "approachable", "conversational"}
    grammar_words = {"grammar", "grammer", "gramr", "spelling", "speling", "spellcheck",
                     "spell check", "spell-check", "proofread", "proof read", "proof-read",
                     "typo", "typos", "punctuation"}

    # Alignment detection: "move/align/shift [block] to right/left/center/justify"
    # Must run before has_move check so directional moves aren't routed to MOVE_SECTION.
    _align_directions = {"right", "left", "center", "justify"}
    _align_trigger_words = {"align", "alignment", "position"}
    _has_direction = any(tok in _align_directions for tok in tokens)
    _has_align_trigger = _contains_any_like(tokens, _align_trigger_words, max_distance=1)
    if _has_direction and (_has_align_trigger or _contains_any_like(tokens, move_words, max_distance=1)):
        return CommandAction.SET_FORMAT, None

    # max_distance=1 for move prevents "remove" (dist 2) and "one" (dist 2) from false-triggering.
    has_move = _contains_any_like(tokens, move_words, max_distance=1)
    has_remove = _contains_any_like(tokens, remove_words, max_distance=2)
    has_delete = _contains_any_like(tokens, delete_words, max_distance=1)
    has_add = _contains_any_like(tokens, add_words, max_distance=1)
    has_section = _contains_any_like(tokens, section_words, max_distance=2)
    has_paragraph = _contains_any_like(tokens, paragraph_words, max_distance=2)
    has_newish = _contains_any_like(tokens, {"new", "another"}, max_distance=1)
    has_sentence_like = _contains_any_like(tokens, {"sentence", "line", "text"}, max_distance=1)
    has_after_before = _contains_any_like(tokens, {"after", "before", "befor", "aftr"}, max_distance=1)
    has_insert_word = _contains_any_like(tokens, {"insert"}, max_distance=1)
    # Compute expand/shorten early and return immediately — they must take priority over all
    # structural checks. "more" (edit dist 1 from "move", dist 2 from "rmove") causes false
    # MOVE_SECTION and REMOVE_PARAGRAPH matches when not guarded here.
    has_expand = _contains_any_like(tokens, expand_words, max_distance=2)
    has_shorten = _contains_any_like(tokens, shorten_words, max_distance=2)
    if has_expand:
        return CommandAction.EXPAND_CONTENT, None
    if has_shorten:
        _shorten_tone = ToneValue.concise if "tone" in p else None
        return CommandAction.SHORTEN_CONTENT, _shorten_tone

    if has_move and (has_section or has_paragraph):
        return CommandAction.MOVE_SECTION, None
    if (has_delete or has_remove) and has_section:
        return CommandAction.DELETE_SECTION, None
    # ADD_PARAGRAPH checked before INSERT_SECTION — when "paragraph" is explicit, prefer it.
    if has_add and has_paragraph and (has_newish or has_after_before or not (has_insert_word and has_sentence_like)):
        return CommandAction.ADD_PARAGRAPH, None
    if has_add and has_section:
        return CommandAction.INSERT_SECTION, None
    if (has_remove or has_delete) and has_paragraph and not has_sentence_like:
        return CommandAction.REMOVE_PARAGRAPH, None

    if _contains_any_like(tokens, rewrite_words, max_distance=2):
        return CommandAction.REWRITE_CONTENT, None
    if any(_contains_any_like(tokens, {gw}, max_distance=1) for gw in grammar_words):
        return CommandAction.FIX_GRAMMAR, None
    if has_add:
        return CommandAction.INSERT_TEXT, None
    if has_delete:
        return CommandAction.DELETE_TEXT, None
    if _contains_any_like(tokens, {"replace", "replce"}, max_distance=1):
        return CommandAction.REPLACE_TEXT, None

    if _contains_any_like(tokens, formal_words, max_distance=2):
        return CommandAction.CHANGE_TONE, ToneValue.formal
    if _contains_any_like(tokens, neutral_words, max_distance=1):
        return CommandAction.CHANGE_TONE, ToneValue.neutral
    if _contains_any_like(tokens, friendly_words, max_distance=2):
        return CommandAction.CHANGE_TONE, ToneValue.neutral
    if "tone" in p and "concise" in p:
        return CommandAction.CHANGE_TONE, ToneValue.concise

    # Default to rewrite for free-form editing asks to keep the sandbox deterministic.
    return CommandAction.REWRITE_CONTENT, None


def _extract_format_style(prompt: str) -> dict:
    """Parse style properties from a formatting command. Returns e.g. {"bold": True}."""
    p = prompt.lower()
    tokens = _tokenize(p)
    style: dict = {}
    if _contains_any_like(tokens, {"bold"}, max_distance=1):
        style["bold"] = True
    if _contains_any_like(tokens, {"italic", "italicize"}, max_distance=1):
        style["italic"] = True
    if _contains_any_like(tokens, {"underline"}, max_distance=1):
        style["underline"] = True
    if _contains_any_like(tokens, {"highlight"}, max_distance=1):
        style["highlight"] = "#FFFF00"   # default yellow highlight
    m = re.search(r"\bfont\s+(?!size\b)(?:to\s+)?([a-z]+)\b|\buse\s+([a-z]+)\s+font\b", p)
    if m:
        style["font"] = (m.group(1) or m.group(2) or "").strip()
    m = re.search(r"\b(?:color|colour)\s+(?:to\s+)?([a-z]+)\b", p)
    if not m:
        m = re.search(
            r"\b(red|blue|green|black|white|gray|grey|yellow|orange|purple)\s+(?:text|colou?r)\b", p)
    if m:
        style["color"] = m.group(1).strip()
    m = re.search(r"\bfont\s+size\s+(?:to\s+)?(\d+)\b|\bsize\s+(?:to\s+)?(\d+)\b|\b(\d+)\s*pt\b", p)
    if m:
        style["size"] = int(next(g for g in m.groups() if g))
    # Detect "entire letter / whole document / all sections" scope.
    _WHOLE_DOC_RE = re.compile(
        r"\b(?:entire|whole|full|throughout|everywhere)\s+"
        r"(?:letter|document|doc|page|text|content|note|draft|file)\b"
        r"|\ball\s+(?:sections|paragraphs|paras|text|pages)\b"
        r"|\beverywhere\b|\bwhole\s+doc(?:ument)?\b",
        re.IGNORECASE,
    )
    if _WHOLE_DOC_RE.search(prompt):
        style["document_wide"] = True
    # Font-size, font-family and color changes without a specific paragraph reference
    # are almost always intended for the whole document.  Default document_wide=True
    # unless the user explicitly targets a paragraph ("para 2", "paragraph 1", etc.).
    _has_para_ref = bool(re.search(r"\b(?:para(?:graph)?|section)\s*\d+\b", p, re.IGNORECASE))
    if not style.get("document_wide") and not _has_para_ref:
        if style.get("size") or style.get("font") or style.get("color"):
            style["document_wide"] = True
    # Prefer "to right/left" (destination) over "from left" (source).
    # "move receiver block from left to right" → "right", not "left".
    _dir_to = re.search(r"\bto\s+(right|left|center|justify)\b", p)
    if _dir_to:
        style["align"] = _dir_to.group(1)
    else:
        _all_dirs = re.findall(r"\b(right|left|center|justify)\b", p)
        if _all_dirs:
            style["align"] = _all_dirs[-1]  # last direction word = destination
    # Word-level: extract target word/phrase for single/multi-word formatting.
    # Handles straight quotes ' " and curly/smart quotes \u2018\u2019\u201c\u201d.
    _Q = r"['\"\u2018\u2019\u201c\u201d]"
    _FMT = r"(?:bold|italic|italicize|underline|format|highlight)"
    # Pattern A: format verb first — "bold 'budget'", "bold word 'budget'", "bold the word 'budget'"
    tw = re.search(
        rf"{_FMT}\s+(?:the\s+)?(?:word\s+|phrase\s+)?{_Q}([^'\"\u2018\u2019\u201c\u201d]+){_Q}",
        prompt, re.IGNORECASE
    ) or re.search(
        rf"{_FMT}\s+(?:the\s+)?(?:word|phrase)\s+(\w[\w ]*?)(?:\s+in\b|$)",
        prompt, re.IGNORECASE
    )
    # Pattern B: word/phrase first — "make 'budget' bold", "set 'Lt Col Kumar' italic"
    if not tw:
        tw = re.search(
            rf"(?:make|set|mark)\s+{_Q}([^'\"\u2018\u2019\u201c\u201d]+){_Q}\s+{_FMT}",
            prompt, re.IGNORECASE
        )
    if tw:
        style["target_word"] = tw.group(1).strip()
    return style


def _extract_replace_content(prompt: str) -> str | None:
    m = re.search(r"\breplace\b.+?\bwith\b\s+(.+)$", prompt, flags=re.IGNORECASE)
    if m:
        value = m.group(1).strip().strip('"').strip("'")
        return value or None
    return None


def _extract_insert_content(prompt: str) -> str | None:
    m = re.search(r"\binsert\b\s+(.+)$", prompt, flags=re.IGNORECASE)
    if m:
        value = m.group(1).strip().strip('"').strip("'")
        return value or None
    return None


def _extract_add_paragraph_content(prompt: str) -> str | None:
    patterns = [
        # sub-para patterns first (more specific)
        r"\b(?:add|insert|append)\s+(?:a\s+)?(?:new\s+)?sub[-\s]?para(?:graph)?\b.*?\bwith\b\s+(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+)?(?:new\s+)?sub[-\s]?para(?:graph)?\b.*?:\s*(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+)?(?:new\s+)?sub[-\s]?para(?:graph)?\b\s+(?:to|under|in)\b\s+.+?\s+(.+)$",
        # regular paragraph patterns — "about/on/regarding/covering X" suffix (handles "another", position phrases)
        r"\b(?:add|insert|append)\s+(?:a\s+|another\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b.*?\babout\b\s+(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+|another\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b.*?\bon\b\s+(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+|another\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b.*?\bregarding\b\s+(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+|another\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b.*?\bcovering\b\s+(.+)$",
        # with / colon patterns (also accept "another")
        r"\b(?:add|insert|append)\s+(?:a\s+|another\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b.*?\bwith\b\s+(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+|another\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b.*?:\s*(.+)$",
        r"\b(?:add|insert|append)\s+(?:a\s+)?(?:new\s+)?(?:paragraph|para|paragrph|paragraf|pargraph)\b\s+(.+)$",
    ]
    for pattern in patterns:
        m = re.search(pattern, prompt, flags=re.IGNORECASE)
        if not m:
            continue
        value = m.group(1).strip().strip('"').strip("'")
        # Ignore trailing anchor-only fragments.
        if value and not re.fullmatch(
            r"(?:after|before)\s+(?:sub[-\s]?para(?:graph)?|paragraph|para)\s+[\d.]+",
            value, flags=re.IGNORECASE,
        ):
            return value
    return None


def _normalize_new_paragraph_text(text: str | None) -> str:
    candidate = (text or "").strip()
    return candidate or "New paragraph."


def _is_sub_para_add(prompt: str) -> bool:
    """Return True if the prompt is asking to add a sub-paragraph (4.1, 4.1.1 …)."""
    p = prompt.lower()
    if re.search(r"\bsub[-\s]para(?:graph)?\b", p):
        return True
    tokens = _tokenize(p)
    has_sub = any(t == "sub" for t in tokens)
    paragraph_words = {"paragraph", "para", "paragrph", "paragraf", "pargraph"}
    has_para = _contains_any_like(tokens, paragraph_words, max_distance=2)
    return has_sub and has_para


def _parse_sub_para_ref(prompt: str) -> tuple[str | None, int | None]:
    """Extract the parent reference from a sub-para add prompt.

    Returns (sub_ref, parent_num):
      "add sub para to 4.1" → ("4.1", 4)   (add sub-sub under 4.1)
      "add sub para to para 4" → (None, 4)  (add sub-para 4.1 under para 4)
    """
    p = prompt.lower()
    # Look for an existing sub-para ref like "4.1" or "4.1.1" to nest under
    m = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", p)
    if m:
        ref = m.group(1)
        parent = int(ref.split(".")[0])
        return ref, parent
    # Fall back to plain paragraph number
    parent = _parse_paragraph_ref(p)
    return None, parent


def _next_sub_para_num(bp_paras: list[dict], parent_ref: str) -> str:
    """Compute the next available sub-paragraph number under parent_ref.

    parent_ref="4"   → scans for "4.N" prefixes → returns "4.1" (or "4.3" if "4.1","4.2" exist)
    parent_ref="4.1" → scans for "4.1.N" prefixes → returns "4.1.1" (or next)
    """
    from app.services.render_adapter import _section_text as _st  # local import avoids circular
    escaped = re.escape(parent_ref)
    pattern = re.compile(rf"^{escaped}\.(\d+)[\.\)]?\s+\S")
    existing: list[int] = []
    for sec in bp_paras:
        text = _st(sec).strip()
        m = pattern.match(text)
        if m:
            existing.append(int(m.group(1)))
    next_idx = (max(existing) + 1) if existing else 1
    return f"{parent_ref}.{next_idx}"


def _find_sub_para_anchor(bp_paras: list[dict], parent_ref: str, parent_num: int) -> dict | None:
    """Return the section to insert after: the last existing sub-para of parent,
    or the parent paragraph itself if no sub-paras exist yet.
    """
    from app.services.render_adapter import _section_text as _st
    escaped_ref = re.escape(parent_ref)
    sub_pattern = re.compile(rf"^{escaped_ref}\.\d+")
    # parent paragraph matches "4. text" or "4) text"
    parent_pattern = re.compile(rf"^{re.escape(str(parent_num))}[\.\)]\s+\S")

    parent_sec: dict | None = None
    last_sub: dict | None = None
    for sec in bp_paras:
        text = _st(sec).strip()
        if sub_pattern.match(text):
            last_sub = sec
        elif parent_pattern.match(text) and parent_sec is None:
            parent_sec = sec
    return last_sub or parent_sec


def _move_position_from_prompt(prompt: str) -> tuple[str, int]:
    tokens = _tokenize(prompt)
    if _contains_any_like(tokens, {"before", "befor", "bfore"}, max_distance=1):
        return "before", 0
    return "after", 1


_DOCUMENT_SCOPE_PHRASES: frozenset = frozenset({
    "document", "the document", "whole document", "entire document",
    "all paragraphs", "all paras", "all sections", "everything",
    "poori document", "puri document", "poora document",
})


def _is_document_scope(prompt: str) -> bool:
    """Return True when the prompt explicitly targets the whole document."""
    p = prompt.lower()
    return any(phrase in p for phrase in _DOCUMENT_SCOPE_PHRASES)


def resolve_action_object_from_request(prompt: str, context: CommandContext, structured: dict) -> ActionObject:
    if not isinstance(prompt, str) or not prompt.strip():
        raise IntentParseError("empty command prompt")

    # Early intercept: "add/set/update [section_keyword] [content]"
    # e.g. "add signee Brig RS Sharma", "set subject Budget Allocation 2026"
    _sec_content_obj = _try_section_content_set(prompt, structured)
    if _sec_content_obj is not None:
        return _sec_content_obj

    # Early intercept: "add table NxM" — must run before structural checks to avoid
    # "table" being routed to INSERT_SECTION with no type information.
    # Uses REWRITE_CONTENT + __create:table_block so main.py's __create: handler
    # (which is inside the CONTENT_OPS branch) fires correctly.
    if _TABLE_ADD_RE.search(prompt):
        dims = _parse_table_dims(prompt)
        dims_str = f"{dims[0]}x{dims[1]}" if dims else "4x4"
        pos = _parse_table_position(prompt)
        # Encode position hint in para_id so main.py can resolve insertion point:
        #   "__create:table_block"              → after last paragraph (default)
        #   "__create:table_block:after_para:N" → after Nth paragraph (1-based)
        #   "__create:table_block:after_last"   → after all body content
        para_id = f"__create:table_block:{pos}" if pos else "__create:table_block"
        return ActionObject(
            action=CommandAction.REWRITE_CONTENT,
            scope=CommandScope.SECTION,
            target=ActionTarget(section_id=None, para_id=para_id, para_index=None),
            params=ActionParams(tone=None, preserve_numbering=False, preserve_style=False),
            content=dims_str,
            confidence=0.95,
            needs_clarification=False,
            clarification=None,
        )

    action, tone = _detect_action(prompt)

    # Document-scope content ops: "shorten document", "make document formal", etc.
    # Bypass section resolver so active context.current_section_id is ignored.
    _doc_content_ops = {
        CommandAction.CHANGE_TONE, CommandAction.REWRITE_CONTENT,
        CommandAction.EXPAND_CONTENT, CommandAction.SHORTEN_CONTENT,
        CommandAction.FIX_GRAMMAR,
    }
    if action in _doc_content_ops and _is_document_scope(prompt):
        _bp_paras = [s for s in _sections(structured) if s.get("type") == "paragraph"]
        if _bp_paras:
            return ActionObject(
                action=action, scope=CommandScope.DOCUMENT,
                target=ActionTarget(section_id=None, para_id=None, para_index=None),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                content=None, confidence=0.90, needs_clarification=False, clarification=None,
            )

    if action == CommandAction.SET_FORMAT:
        style = _extract_format_style(prompt)
        if not style:
            return _clarify_action(
                action=action,
                scope=CommandScope.PARAGRAPH,
                question="What format should I apply? (bold, italic, highlight, font, color, size, align left/right/center)",
                confidence=0.50,
                options=[],
            )
        # Document-wide format (e.g. "change font to Arial for entire letter") —
        # skip section resolution entirely, no clarification needed.
        if style.get("document_wide"):
            return ActionObject(
                action=action, scope=CommandScope.SECTION,
                target=ActionTarget(section_id=None, para_id=None, para_index=None),
                params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True,
                                    style_params=style),
                content=None, confidence=0.92,
                needs_clarification=False, clarification=None,
            )
        fmt_section = _resolve_section_from_prompt(prompt.lower(), context, structured)
        if fmt_section is None:
            # No active section in context — fall back to the first numbered_paragraphs section.
            fmt_section = next(
                (s for s in _sections(structured) if s.get("type") == "numbered_paragraphs"),
                None,
            )
        if fmt_section is None:
            # Blueprint fallback: individual "paragraph" type sections (no numbered_paragraphs).
            _bp_fmt_paras = [s for s in _sections(structured) if s.get("type") == "paragraph"]
            _fmt_para_num = _parse_paragraph_ref(prompt.lower())
            if _bp_fmt_paras:
                if _fmt_para_num == _PARA_REF_LAST:
                    fmt_section = _bp_fmt_paras[-1]
                elif _fmt_para_num is not None and 0 < _fmt_para_num <= len(_bp_fmt_paras):
                    fmt_section = _bp_fmt_paras[_fmt_para_num - 1]
                else:
                    fmt_section = _bp_fmt_paras[0]
        if fmt_section is None:
            # No sections cached yet (stale state) — return document-wide format, no clarification.
            return ActionObject(
                action=action, scope=CommandScope.DOCUMENT,
                target=ActionTarget(section_id=None, para_id=None, para_index=None),
                params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True,
                                    style_params=_extract_format_style(prompt)),
                content=None, confidence=0.65, needs_clarification=False, clarification=None,
            )
        fmt_paragraph_number = _parse_paragraph_ref(prompt.lower())

        # If the resolved section is not a numbered_paragraphs section but a para ref was
        # explicitly given (e.g. "make 2000 in para 2 bold" while subject is active),
        # fall back to the first numbered_paragraphs section so the para ref resolves correctly.
        if fmt_section.get("type") != "numbered_paragraphs" and fmt_paragraph_number is not None:
            body_sec = next(
                (s for s in (_sections(structured)) if s.get("type") == "numbered_paragraphs"),
                None,
            )
            if body_sec:
                fmt_section = body_sec

        # Non-paragraph / blueprint-paragraph section with no explicit para ref →
        # format the whole section (e.g. "make subject bold", "make paragraph 2 italic").
        # For blueprint docs: individual "paragraph" sections ARE the target directly.
        if fmt_section.get("type") != "numbered_paragraphs":
            return ActionObject(
                action=action,
                scope=CommandScope.SECTION,
                target=ActionTarget(section_id=fmt_section.get("id"), para_id=None, para_index=None),
                params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True, style_params=style),
                content=None,
                confidence=0.88,
                needs_clarification=False,
                clarification=None,
            )

        fmt_items = _paragraph_items(fmt_section)
        fmt_resolved_para_id: str | None = None
        if fmt_paragraph_number is not None:
            fmt_candidate_id = f"p{fmt_paragraph_number}"
            fmt_by_id = next((i for i in fmt_items if i.get("id") == fmt_candidate_id), None)
            if fmt_by_id is not None:
                fmt_resolved_para_id = fmt_candidate_id
            elif 0 <= fmt_paragraph_number - 1 < len(fmt_items):
                fmt_resolved_para_id = str(fmt_items[fmt_paragraph_number - 1].get("id") or "")
        if fmt_resolved_para_id is None:
            fmt_cursor_para_id, _ = resolve_cursor_paragraph(
                structured=structured,
                section_id=fmt_section.get("id"),
                cursor_position=context.cursor_position,
            )
            fmt_resolved_para_id = fmt_cursor_para_id
        if not fmt_resolved_para_id:
            return _clarify_action(
                action=action,
                scope=CommandScope.PARAGRAPH,
                question="Which paragraph should I format?",
                confidence=0.50,
                options=_clarification_options_for_section(structured, fmt_section.get("id")),
            )
        return ActionObject(
            action=action,
            scope=CommandScope.PARAGRAPH,
            target=ActionTarget(section_id=fmt_section.get("id"), para_id=fmt_resolved_para_id, para_index=None),
            params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True, style_params=style),
            content=None,
            confidence=0.90,
            needs_clarification=False,
            clarification=None,
        )

    if action == CommandAction.UNDO:
        return ActionObject(
            action=action,
            scope=CommandScope.DOCUMENT,
            target=ActionTarget(section_id=None, para_id=None, para_index=None),
            params=ActionParams(),
            content=None,
            confidence=0.95,
            needs_clarification=False,
            clarification=None,
        )

    section_actions = {
        CommandAction.INSERT_SECTION,
        CommandAction.DELETE_SECTION,
        CommandAction.MOVE_SECTION,
    }

    if action in section_actions:
        section_refs = _parse_section_refs(prompt)
        known_refs = [ref for ref in section_refs if _find_section_by_id(structured, ref)]
        all_section_options = _clarification_options_for_document_sections(structured)

        if action == CommandAction.INSERT_SECTION:
            anchor_section_id = known_refs[0] if known_refs else context.current_section_id
            if anchor_section_id and not _find_section_by_id(structured, anchor_section_id):
                anchor_section_id = None
            if anchor_section_id is None and _sections(structured):
                return _clarify_action(
                    action=action,
                    scope=CommandScope.SECTION,
                    question="Which section should I insert after?",
                    confidence=0.50,
                    options=all_section_options,
                )
            return ActionObject(
                action=action,
                scope=CommandScope.SECTION,
                target=ActionTarget(section_id=anchor_section_id, para_id=None, para_index=None),
                params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True),
                content=None,
                confidence=0.84,
                needs_clarification=False,
                clarification=None,
            )

        if action == CommandAction.DELETE_SECTION:
            target_section_id = known_refs[0] if known_refs else context.current_section_id
            if not target_section_id or not _find_section_by_id(structured, target_section_id):
                return _clarify_action(
                    action=action,
                    scope=CommandScope.SECTION,
                    question="Which section should I remove?",
                    confidence=0.50,
                    options=all_section_options,
                )
            return ActionObject(
                action=action,
                scope=CommandScope.SECTION,
                target=ActionTarget(section_id=target_section_id, para_id=None, para_index=None),
                params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True),
                content=None,
                confidence=0.84,
                needs_clarification=False,
                clarification=None,
            )

        source_section_id: str | None = None
        anchor_section_id: str | None = None
        if len(known_refs) >= 2:
            source_section_id = known_refs[0]
            anchor_section_id = known_refs[1]
        elif len(known_refs) == 1:
            source_section_id = known_refs[0]
            candidate_anchor = context.current_section_id
            if candidate_anchor and candidate_anchor != source_section_id and _find_section_by_id(structured, candidate_anchor):
                anchor_section_id = candidate_anchor
        else:
            candidate_source = context.current_section_id
            if candidate_source and _find_section_by_id(structured, candidate_source):
                source_section_id = candidate_source

        # Blueprint fallback: resolve "paragraph N" refs to actual section UUIDs.
        # Used when the prompt says "move paragraph 2 before paragraph 1" and the
        # doc has individual paragraph-type sections (no numbered_paragraphs).
        if not source_section_id:
            _bp_paras = [s for s in _sections(structured) if s.get("type") == "paragraph"]
            if _bp_paras:
                _nums = [int(m) for m in re.findall(r'\b(?:paragraph|para)\s+(\d+)\b', prompt.lower())]
                if len(_nums) >= 2:
                    si, ai = _nums[0] - 1, _nums[1] - 1
                    if 0 <= si < len(_bp_paras) and 0 <= ai < len(_bp_paras):
                        source_section_id = _bp_paras[si].get("id")
                        anchor_section_id = _bp_paras[ai].get("id")
                elif len(_nums) == 1:
                    si = _nums[0] - 1
                    if 0 <= si < len(_bp_paras):
                        source_section_id = _bp_paras[si].get("id")

        if not source_section_id:
            return _clarify_action(
                action=action,
                scope=CommandScope.SECTION,
                question="Which section should I move?",
                confidence=0.50,
                options=all_section_options,
            )
        if not anchor_section_id:
            return _clarify_action(
                action=action,
                scope=CommandScope.SECTION,
                question="Which destination section should I move it near?",
                confidence=0.50,
                options=all_section_options,
                section_id=source_section_id,
            )
        if source_section_id == anchor_section_id:
            return _clarify_action(
                action=action,
                scope=CommandScope.SECTION,
                question="Source and destination sections must be different. Which destination section should I use?",
                confidence=0.50,
                options=all_section_options,
                section_id=source_section_id,
            )
        _position, position_idx = _move_position_from_prompt(prompt)
        return ActionObject(
            action=action,
            scope=CommandScope.SECTION,
            target=ActionTarget(section_id=source_section_id, para_id=anchor_section_id, para_index=position_idx),
            params=ActionParams(tone=None, preserve_numbering=True, preserve_style=True),
            content=None,
            confidence=0.82,
            needs_clarification=False,
            clarification=None,
        )

    section = _resolve_section_from_prompt(prompt.lower(), context, structured)
    paragraph_number = _parse_paragraph_ref(prompt.lower())

    if section is None:
        # No active section in context — mirror SET_FORMAT fallback: use first numbered_paragraphs section.
        section = next(
            (s for s in _sections(structured) if s.get("type") == "numbered_paragraphs"),
            None,
        )
    if section is None:
        # Blueprint doc fallback: no numbered_paragraphs and no active section.
        # Check for individual paragraph-type sections (each paragraph is its own section).
        _bp_paras = [s for s in _sections(structured) if s.get("type") == "paragraph"]
        # ── Sub-paragraph add (4.1, 4.2 … or 4.1.1 …) ────────────────────────
        if action == CommandAction.ADD_PARAGRAPH and _is_sub_para_add(prompt) and _bp_paras:
            _sub_ref, _parent_num = _parse_sub_para_ref(prompt)
            if _parent_num is not None:
                _parent_ref = _sub_ref if _sub_ref else str(_parent_num)
                _anchor = _find_sub_para_anchor(_bp_paras, _parent_ref, _parent_num)
                if _anchor:
                    _sub_num = _next_sub_para_num(_bp_paras, _parent_ref)
                    _raw = _extract_add_paragraph_content(prompt) or ""
                    _cont = f"{_sub_num} {_raw.strip()}" if _raw.strip() else f"{_sub_num} New sub-paragraph."
                    return ActionObject(
                        action=action, scope=CommandScope.SECTION,
                        target=ActionTarget(section_id=_anchor.get("id"), para_id=None, para_index=1),
                        params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                        content=_cont, confidence=0.88, needs_clarification=False, clarification=None,
                    )
        # ── Regular paragraph add / content op on specific para ───────────────
        _effective_para_num = (len(_bp_paras) if paragraph_number == _PARA_REF_LAST and _bp_paras
                               else paragraph_number)
        if _bp_paras and _effective_para_num is not None and 0 <= _effective_para_num - 1 < len(_bp_paras):
            _tgt = _bp_paras[_effective_para_num - 1]
            _cont = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt)) if action == CommandAction.ADD_PARAGRAPH else None
            return ActionObject(
                action=action,
                scope=CommandScope.SECTION,
                target=ActionTarget(section_id=_tgt.get("id"), para_id=f"p{_effective_para_num}", para_index=_effective_para_num - 1),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                content=_cont,
                confidence=0.86,
                needs_clarification=False,
                clarification=None,
            )
        # ADD_PARAGRAPH with no number → append after last paragraph
        if action == CommandAction.ADD_PARAGRAPH and _bp_paras:
            _last = _bp_paras[-1]
            _cont = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt))
            return ActionObject(
                action=action, scope=CommandScope.SECTION,
                target=ActionTarget(section_id=_last.get("id"), para_id=_last.get("id"), para_index=1),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                content=_cont, confidence=0.86, needs_clarification=False, clarification=None,
            )
        # Content ops with no paragraph number: apply to ALL paragraphs (scope=DOCUMENT,
        # section_id=None signals the command handler to loop over all paragraph sections).
        _content_ops = {
            CommandAction.CHANGE_TONE, CommandAction.REWRITE_CONTENT,
            CommandAction.EXPAND_CONTENT, CommandAction.SHORTEN_CONTENT,
            CommandAction.FIX_GRAMMAR,
        }
        if action in _content_ops and _bp_paras:
            return ActionObject(
                action=action, scope=CommandScope.DOCUMENT,
                target=ActionTarget(section_id=None, para_id=None, para_index=None),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                content=None, confidence=0.82, needs_clarification=False, clarification=None,
            )
        # ADD_PARAGRAPH with no _bp_paras (stale state or first-load) → append at end
        if action == CommandAction.ADD_PARAGRAPH:
            _cont = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt))
            return ActionObject(
                action=action, scope=CommandScope.SECTION,
                target=ActionTarget(section_id=None, para_id=None, para_index=None),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                content=_cont, confidence=0.70, needs_clarification=False, clarification=None,
            )
        # Content / format ops with no _bp_paras → apply to all (document scope)
        _all_ops = {
            CommandAction.CHANGE_TONE, CommandAction.REWRITE_CONTENT,
            CommandAction.EXPAND_CONTENT, CommandAction.SHORTEN_CONTENT,
            CommandAction.FIX_GRAMMAR, CommandAction.SET_FORMAT,
        }
        if action in _all_ops:
            return ActionObject(
                action=action, scope=CommandScope.DOCUMENT,
                target=ActionTarget(section_id=None, para_id=None, para_index=None),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True,
                                    style_params=_extract_format_style(prompt) if action == CommandAction.SET_FORMAT else None),
                content=None, confidence=0.65, needs_clarification=False, clarification=None,
            )
        return _clarify_action(
            action=action,
            scope=CommandScope.PARAGRAPH,
            question="Which section should I update?",
            confidence=0.40,
            options=[],
        )

    if section.get("type") != "numbered_paragraphs":
        # Try to fall back to first numbered_paragraphs section (mirrors SET_FORMAT behavior).
        body_sec = next(
            (s for s in _sections(structured) if s.get("type") == "numbered_paragraphs"),
            None,
        )
        if body_sec:
            section = body_sec
        else:
            # Blueprint doc fallback: no numbered_paragraphs — use individual paragraph sections.
            _bp_paras = [s for s in _sections(structured) if s.get("type") == "paragraph"]
            # ── Sub-paragraph add (4.1, 4.2 … or 4.1.1 …) ────────────────────
            if action == CommandAction.ADD_PARAGRAPH and _is_sub_para_add(prompt) and _bp_paras:
                _sub_ref, _parent_num = _parse_sub_para_ref(prompt)
                if _parent_num is not None:
                    _parent_ref = _sub_ref if _sub_ref else str(_parent_num)
                    _anchor = _find_sub_para_anchor(_bp_paras, _parent_ref, _parent_num)
                    if _anchor:
                        _sub_num = _next_sub_para_num(_bp_paras, _parent_ref)
                        _raw = _extract_add_paragraph_content(prompt) or ""
                        _cont = f"{_sub_num} {_raw.strip()}" if _raw.strip() else f"{_sub_num} New sub-paragraph."
                        return ActionObject(
                            action=action, scope=CommandScope.SECTION,
                            target=ActionTarget(section_id=_anchor.get("id"), para_id=None, para_index=1),
                            params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                            content=_cont, confidence=0.88, needs_clarification=False, clarification=None,
                        )
            # ── Regular paragraph add / content op on specific para ───────────
            _effective_para_num2 = (len(_bp_paras) if paragraph_number == _PARA_REF_LAST and _bp_paras
                                    else paragraph_number)
            if _bp_paras and _effective_para_num2 is not None and 0 <= _effective_para_num2 - 1 < len(_bp_paras):
                _tgt = _bp_paras[_effective_para_num2 - 1]
                _cont = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt)) if action == CommandAction.ADD_PARAGRAPH else None
                return ActionObject(
                    action=action,
                    scope=CommandScope.SECTION,
                    target=ActionTarget(section_id=_tgt.get("id"), para_id=f"p{_effective_para_num2}", para_index=_effective_para_num2 - 1),
                    params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                    content=_cont,
                    confidence=0.86,
                    needs_clarification=False,
                    clarification=None,
                )
            # ADD_PARAGRAPH with no number → append after last paragraph
            if action == CommandAction.ADD_PARAGRAPH and _bp_paras:
                _last = _bp_paras[-1]
                _cont = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt))
                return ActionObject(
                    action=action, scope=CommandScope.SECTION,
                    target=ActionTarget(section_id=_last.get("id"), para_id=_last.get("id"), para_index=1),
                    params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                    content=_cont, confidence=0.86, needs_clarification=False, clarification=None,
                )
            # Content ops with no paragraph number — apply to ALL paragraphs.
            _content_ops2 = {
                CommandAction.CHANGE_TONE, CommandAction.REWRITE_CONTENT,
                CommandAction.EXPAND_CONTENT, CommandAction.SHORTEN_CONTENT,
                CommandAction.FIX_GRAMMAR,
            }
            if action in _content_ops2 and _bp_paras:
                return ActionObject(
                    action=action, scope=CommandScope.DOCUMENT,
                    target=ActionTarget(section_id=None, para_id=None, para_index=None),
                    params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                    content=None, confidence=0.82, needs_clarification=False, clarification=None,
                )
            # ADD_PARAGRAPH with no _bp_paras → append at end
            if action == CommandAction.ADD_PARAGRAPH:
                _cont = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt))
                return ActionObject(
                    action=action, scope=CommandScope.SECTION,
                    target=ActionTarget(section_id=None, para_id=None, para_index=None),
                    params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                    content=_cont, confidence=0.70, needs_clarification=False, clarification=None,
                )
            # Content / format ops with no _bp_paras → apply to all (document scope)
            _all_ops2 = {
                CommandAction.CHANGE_TONE, CommandAction.REWRITE_CONTENT,
                CommandAction.EXPAND_CONTENT, CommandAction.SHORTEN_CONTENT,
                CommandAction.FIX_GRAMMAR, CommandAction.SET_FORMAT,
            }
            if action in _all_ops2:
                return ActionObject(
                    action=action, scope=CommandScope.DOCUMENT,
                    target=ActionTarget(section_id=None, para_id=None, para_index=None),
                    params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True,
                                        style_params=_extract_format_style(prompt) if action == CommandAction.SET_FORMAT else None),
                    content=None, confidence=0.65, needs_clarification=False, clarification=None,
                )
            return _clarify_action(
                action=action,
                scope=CommandScope.PARAGRAPH,
                question="Current section has no numbered paragraphs. Which paragraph section should I use?",
                confidence=0.45,
                options=[],
                section_id=section.get("id"),
            )

    items = _paragraph_items(section)

    if not items:
        raise IntentParseError("resolved paragraph section has no items")

    resolved_para_id: str | None = None
    resolved_para_index: int | None = None

    if paragraph_number is not None:
        # Resolver default #3: "paragraph N" resolves inside the selected section.
        candidate_id = f"p{paragraph_number}"
        by_id = next((i for i in items if i.get("id") == candidate_id), None)
        if by_id is not None:
            resolved_para_id = candidate_id
            resolved_para_index = max(0, paragraph_number - 1)
        else:
            idx = paragraph_number - 1
            if 0 <= idx < len(items):
                resolved_para_id = str(items[idx].get("id") or "")
                resolved_para_index = idx

    if resolved_para_id is None and paragraph_number is None:
        # Deterministic cursor default for ambiguous voice-like commands.
        # We only use this when the user did not explicitly ask for paragraph N.
        cursor_para_id, cursor_idx = resolve_cursor_paragraph(
            structured=structured,
            section_id=section.get("id"),
            cursor_position=context.cursor_position,
        )
        if cursor_para_id:
            resolved_para_id = cursor_para_id
            resolved_para_index = cursor_idx

    if resolved_para_id is None:
        if action == CommandAction.ADD_PARAGRAPH and items:
            # For add-paragraph commands, default to appending after the last paragraph.
            fallback_idx = len(items) - 1
            resolved_para_id = str(items[fallback_idx].get("id") or "")
            resolved_para_index = fallback_idx
        elif len(items) == 1:
            only = items[0]
            resolved_para_id = str(only.get("id") or "")
            resolved_para_index = 0
        else:
            # Resolver default #4: ambiguity triggers clarification, never silent guessing.
            clarification = _build_clarification("Which paragraph?", items)
            return ActionObject(
                action=action,
                scope=CommandScope.PARAGRAPH,
                target=ActionTarget(section_id=section.get("id"), para_id=None, para_index=None),
                params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
                content=None,
                confidence=0.50,
                needs_clarification=True,
                clarification=clarification,
            )

    content_value: str | None = None
    if action == CommandAction.REPLACE_TEXT:
        content_value = _extract_replace_content(prompt)
    elif action == CommandAction.INSERT_TEXT:
        content_value = _extract_insert_content(prompt)
    elif action == CommandAction.ADD_PARAGRAPH:
        content_value = _normalize_new_paragraph_text(_extract_add_paragraph_content(prompt))

    return ActionObject(
        action=action,
        scope=CommandScope.PARAGRAPH,
        target=ActionTarget(section_id=section.get("id"), para_id=resolved_para_id, para_index=resolved_para_index),
        params=ActionParams(tone=tone, preserve_numbering=True, preserve_style=True),
        content=content_value,
        confidence=0.86,
        needs_clarification=False,
        clarification=None,
    )


_V1_TRANSFORM_SAFE_ACTIONS: set[CommandAction] = {
    CommandAction.CHANGE_TONE,
    CommandAction.REWRITE_CONTENT,
    CommandAction.EXPAND_CONTENT,
    CommandAction.SHORTEN_CONTENT,
    CommandAction.FIX_GRAMMAR,
}


def _empty_transform_meta() -> dict[str, Any]:
    return {
        "transform_source": None,
        "transform_prompt_version": None,
        "transform_repair_applied": None,
    }


def _next_para_id(section: dict) -> str:
    highest = 0
    for item in _paragraph_items(section):
        para_id = str(item.get("id") or "").strip()
        match = re.fullmatch(r"p(\d+)", para_id, flags=re.IGNORECASE)
        if not match:
            continue
        highest = max(highest, int(match.group(1)))
    return f"p{highest + 1}"


def _next_section_id(structured: dict) -> str:
    highest = 0
    for section in _sections(structured):
        section_id = str(section.get("id") or "").strip()
        match = re.fullmatch(r"sec_cmd_(\d+)", section_id, flags=re.IGNORECASE)
        if not match:
            continue
        highest = max(highest, int(match.group(1)))
    return f"sec_cmd_{highest + 1:03d}"


async def plan_patch_ops_from_action(
    action_obj: ActionObject,
    structured: dict,
    context: dict | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    if action_obj.needs_clarification:
        return [], _empty_transform_meta()

    action = action_obj.action
    para_id = action_obj.target.para_id
    section_id = action_obj.target.section_id

    if action in _V1_TRANSFORM_SAFE_ACTIONS:
        if not para_id or not section_id:
            raise NeedsClarificationError(
                question="Which paragraph?",
                options=_clarification_options_for_section(structured, section_id),
            )

        section = _find_section_by_id(structured, section_id)
        if section is None or section.get("type") != "numbered_paragraphs":
            raise NeedsClarificationError(
                question="Which paragraph section should I use?",
                options=[],
            )

        matches = [i for i in _paragraph_items(section) if str(i.get("id")) == para_id]
        if len(matches) != 1:
            raise NeedsClarificationError(
                question="Which paragraph?",
                options=_clarification_options_for_section(structured, section_id),
            )

        try:
            new_text, transform_meta = await apply_transform(action_object=action_obj, doc=structured, context=context)
        except TransformError as ex:
            raise PlannerError(str(ex)) from ex

        # Resolver/planner default #5: v1 transforms produce replace_para_text only.
        return [
            {
                "op": "replace_para_text",
                "target": {
                    "section_id": section_id,
                    "para_id": para_id,
                },
                "text": new_text,
            }
        ], transform_meta

    if action == CommandAction.ADD_PARAGRAPH:
        if not section_id:
            raise NeedsClarificationError(
                question="Which paragraph section should I update?",
                options=_clarification_options_for_document_sections(structured),
            )
        section = _find_section_by_id(structured, section_id)
        if section is None or section.get("type") != "numbered_paragraphs":
            raise NeedsClarificationError(
                question="Which paragraph section should I use?",
                options=[],
            )
        items = _paragraph_items(section)
        if not items:
            raise NeedsClarificationError(question="Which paragraph should I insert after?", options=[])

        anchor_para_id = para_id or str(items[-1].get("id") or "")
        if not anchor_para_id:
            raise NeedsClarificationError(
                question="Which paragraph should I insert after?",
                options=_clarification_options_for_section(structured, section_id),
            )
        matches = [i for i in items if str(i.get("id")) == anchor_para_id]
        if len(matches) != 1:
            raise NeedsClarificationError(
                question="Which paragraph should I insert after?",
                options=_clarification_options_for_section(structured, section_id),
            )
        new_para = {
            "id": _next_para_id(section),
            "text": _normalize_new_paragraph_text(action_obj.content),
        }
        return [
            {
                "op": "insert_para_after",
                "target": {
                    "section_id": section_id,
                    "after_para_id": anchor_para_id,
                },
                "para": new_para,
            }
        ], _empty_transform_meta()

    if action == CommandAction.REMOVE_PARAGRAPH:
        if not para_id or not section_id:
            raise NeedsClarificationError(
                question="Which paragraph should I remove?",
                options=_clarification_options_for_section(structured, section_id),
            )
        section = _find_section_by_id(structured, section_id)
        if section is None or section.get("type") != "numbered_paragraphs":
            raise NeedsClarificationError(
                question="Which paragraph section should I use?",
                options=[],
            )
        matches = [i for i in _paragraph_items(section) if str(i.get("id")) == para_id]
        if len(matches) != 1:
            raise NeedsClarificationError(
                question="Which paragraph should I remove?",
                options=_clarification_options_for_section(structured, section_id),
            )
        return [
            {
                "op": "delete_para",
                "target": {
                    "section_id": section_id,
                    "para_id": para_id,
                },
            }
        ], _empty_transform_meta()

    if action == CommandAction.INSERT_SECTION:
        if section_id and _find_section_by_id(structured, section_id) is None:
            raise NeedsClarificationError(
                question="Which section should I insert after?",
                options=_clarification_options_for_document_sections(structured),
            )
        first_para_text = _normalize_new_paragraph_text(action_obj.content)
        return [
            {
                "op": "insert_section_after",
                "target": {
                    "after_section_id": section_id,
                },
                "section": {
                    "id": _next_section_id(structured),
                    "type": "numbered_paragraphs",
                    "content": {
                        "items": [
                            {
                                "id": "p1",
                                "text": first_para_text,
                            }
                        ]
                    },
                },
            }
        ], _empty_transform_meta()

    if action == CommandAction.DELETE_SECTION:
        if not section_id or _find_section_by_id(structured, section_id) is None:
            raise NeedsClarificationError(
                question="Which section should I remove?",
                options=_clarification_options_for_document_sections(structured),
            )
        return [
            {
                "op": "delete_section",
                "target": {
                    "section_id": section_id,
                },
            }
        ], _empty_transform_meta()

    if action == CommandAction.MOVE_SECTION:
        source_section_id = section_id
        anchor_section_id = para_id
        if not source_section_id or _find_section_by_id(structured, source_section_id) is None:
            raise NeedsClarificationError(
                question="Which section should I move?",
                options=_clarification_options_for_document_sections(structured),
            )
        if not anchor_section_id or _find_section_by_id(structured, anchor_section_id) is None:
            raise NeedsClarificationError(
                question="Which destination section should I move it near?",
                options=_clarification_options_for_document_sections(structured),
            )
        if source_section_id == anchor_section_id:
            raise NeedsClarificationError(
                question="Source and destination sections must be different. Which destination section should I use?",
                options=_clarification_options_for_document_sections(structured),
            )
        position = "before" if action_obj.target.para_index == 0 else "after"
        return [
            {
                "op": "move_section",
                "target": {
                    "section_id": source_section_id,
                    "anchor_section_id": anchor_section_id,
                    "position": position,
                },
            }
        ], _empty_transform_meta()

    if action == CommandAction.SET_FORMAT:
        style = action_obj.params.style_params or {}
        if not style:
            raise NeedsClarificationError(
                question="What format should I apply? (bold, italic, highlight, font, color, size)",
                options=[],
            )
        if not section_id:
            raise NeedsClarificationError(
                question="Which section should I format?",
                options=_clarification_options_for_document_sections(structured),
            )
        # Section-level format (subject, date, reference fields — no para_id).
        if not para_id:
            return [
                {
                    "op": "set_section_style",
                    "target": {"section_id": section_id},
                    "style": style,
                }
            ], {"transform_source": "deterministic", "transform_prompt_version": None,
                "transform_repair_applied": None, "format_applied": list(style.keys())}
        return [
            {
                "op": "set_para_style",
                "target": {
                    "section_id": section_id,
                    "para_id": para_id,
                },
                "style": style,
            }
        ], {"transform_source": "deterministic", "transform_prompt_version": None,
            "transform_repair_applied": None, "format_applied": list(style.keys())}

    if action == CommandAction.UNDO:
        # Sentinel op — the command endpoint handles the actual version revert directly.
        return [{"op": "revert_to_previous"}], {
            "transform_source": "deterministic",
            "transform_prompt_version": None,
            "transform_repair_applied": None,
        }

    raise PlannerError(f"unsupported_action: {action_obj.action.value}")


def build_clarification_token() -> str:
    return "cl_" + uuid.uuid4().hex
