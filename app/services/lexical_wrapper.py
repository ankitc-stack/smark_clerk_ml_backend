"""
app/services/lexical_wrapper.py

INTEGRATION SEAM — READ THIS BEFORE TOUCHING THIS FILE
=======================================================

WHAT:
    Convert plain text strings (LLM output) into Lexical JSON nodes so they
    can be stored in a section's richtext.state and rendered by the frontend editor.

WHY THIS FILE EXISTS:
    The LLM returns only plain text values (subject text, paragraph texts, etc).
    It never touches Lexical JSON or section structure — that is the Golden Rule.
    This file is the ONLY place where plain text becomes a Lexical node.
    Centralising the conversion here means:
    - One place to update when Lexical's schema changes.
    - One place to apply style_defaults consistently across all section types.
    - Clear seam for the backend developer to swap out the sandbox implementation.

CURRENT STATE (sandbox):
    Uses a minimal, valid Lexical structure sufficient for:
    - sandbox testing
    - export via docxtpl (current renderer)
    - verifying text flows: LLM → section → DOCX

WHEN DOCUMENT ENGINE IS READY (backend dev action):
    Replace the body of text_to_lexical_node() with your full implementation.
    Everything else in this file — function signatures, inject helpers, the
    lexical_to_plain_text utility — stays unchanged.

CONTRACT (DO NOT change function signatures — downstream code depends on these):
    text_to_lexical_node(text, style_defaults) -> dict
    inject_text_into_section(section, text, style_defaults) -> dict
    inject_paras_into_section(section, para_texts, style_defaults) -> dict
    lexical_to_plain_text(state) -> str
"""

from __future__ import annotations
import copy
import re
from typing import Any


# ---------------------------------------------------------------------------
# Color name → hex lookup (used by style application + DOCX rendering)
# ---------------------------------------------------------------------------

_COLOR_HEX: dict[str, str] = {
    "red": "FF0000", "blue": "0000FF", "green": "008000", "black": "000000",
    "white": "FFFFFF", "gray": "808080", "grey": "808080", "yellow": "FFFF00",
    "orange": "FFA500", "purple": "800080", "navy": "000080", "teal": "008080",
    "maroon": "800000", "olive": "808000", "cyan": "00FFFF", "magenta": "FF00FF",
    "pink": "FFC0CB", "brown": "A52A2A",
}


def _to_hex_color(raw: str) -> str:
    """Return a 6-char uppercase hex string for a CSS color name or hex value.
    Returns empty string if the value cannot be resolved."""
    raw = raw.strip().lstrip("#").lower()
    if raw in _COLOR_HEX:
        return _COLOR_HEX[raw]
    if len(raw) == 6 and all(c in "0123456789abcdef" for c in raw):
        return raw.upper()
    if len(raw) == 3 and all(c in "0123456789abcdef" for c in raw):
        return "".join(c * 2 for c in raw).upper()
    return ""


# ---------------------------------------------------------------------------
# CSS helpers (used by format apply functions)
# ---------------------------------------------------------------------------

def _parse_css(style_str: str) -> dict:
    css = {}
    for part in (style_str or "").split(";"):
        part = part.strip()
        if ":" in part:
            k, _, v = part.partition(":")
            css[k.strip()] = v.strip()
    return css


def _serialize_css(css: dict) -> str:
    return "".join(f"{k}:{v};" for k, v in css.items())


def _apply_style_to_node(node: dict, style: dict) -> dict:
    """Apply a style dict to a single Lexical text node. Mutates and returns node."""
    FLAG_BOLD, FLAG_ITALIC, FLAG_UNDERLINE = 1, 2, 8
    fmt = node.get("format", 0)
    if style.get("bold"):      fmt |= FLAG_BOLD
    if style.get("italic"):    fmt |= FLAG_ITALIC
    if style.get("underline"): fmt |= FLAG_UNDERLINE
    node["format"] = fmt
    if style.get("font") or style.get("color") or style.get("size") or style.get("highlight"):
        css = _parse_css(node.get("style", ""))
        if style.get("font"):      css["font-family"] = style["font"]
        if style.get("color"):
            _hex = _to_hex_color(style["color"])
            css["color"] = f"#{_hex}" if _hex else style["color"]
        if style.get("size"):      css["font-size"] = f"{style['size']}pt"
        if style.get("highlight"): css["background-color"] = str(style["highlight"])
        node["style"] = _serialize_css(css)
    return node


# ---------------------------------------------------------------------------
# Format application — section/paragraph level
# ---------------------------------------------------------------------------

def apply_format_to_lexical(state: dict, style: dict) -> dict:
    """Apply style to ALL text nodes in a Lexical root state (section/paragraph level).

    bold/italic/underline → format bitmask on text nodes (OR with existing flags).
    font/color/size       → CSS style string on text nodes.
    align                 → paragraph node format field ("left"/"right"/"center").

    Returns a deep copy — does not mutate the input.
    """
    state = copy.deepcopy(state)
    align = style.get("align")
    for para in (state.get("root") or {}).get("children") or []:
        if align is not None:
            para["format"] = align
        for node in para.get("children") or []:
            if node.get("type") == "text":
                _apply_style_to_node(node, style)
    return state


# ---------------------------------------------------------------------------
# Format application — word level
# ---------------------------------------------------------------------------

def apply_format_to_word(
    state: dict,
    word: str,
    style: dict,
    occurrence: int = 1,
) -> dict:
    """Apply style to a specific word (case-insensitive) in the Lexical state.

    Finds the Nth occurrence of `word` across all text nodes and splits the
    containing node into [before | word | after], applying the style only to
    the middle node.

    occurrence=1 targets the first match, occurrence=2 the second, etc.
    Returns a deep copy. If word not found, returns state unchanged.
    """
    state = copy.deepcopy(state)
    hit = 0
    word_lower = word.lower()

    for para in (state.get("root") or {}).get("children") or []:
        new_children: list = []
        replaced = False
        for node in (para.get("children") or []):
            if replaced or node.get("type") != "text":
                new_children.append(node)
                continue
            text = node.get("text", "")
            idx = text.lower().find(word_lower)
            if idx == -1:
                new_children.append(node)
                continue
            hit += 1
            if hit != occurrence:
                new_children.append(node)
                continue
            # Split into up to 3 nodes: before / word / after
            before_text = text[:idx]
            word_text   = text[idx: idx + len(word)]
            after_text  = text[idx + len(word):]
            base = {k: v for k, v in node.items() if k not in ("text", "format")}
            if before_text:
                new_children.append({**base, "text": before_text,
                                     "format": node.get("format", 0)})
            word_node = copy.deepcopy(base)
            word_node["text"] = word_text
            word_node["format"] = node.get("format", 0)
            _apply_style_to_node(word_node, style)
            new_children.append(word_node)
            if after_text:
                new_children.append({**base, "text": after_text,
                                     "format": node.get("format", 0)})
            replaced = True
        para["children"] = new_children
        if replaced:
            return state   # stop after the target occurrence is handled

    return state   # word not found — return unchanged


# ---------------------------------------------------------------------------
# Lexical → docxtpl RichText (for DOCX rendering)
# ---------------------------------------------------------------------------

def lexical_nodes_to_rich_text(state: dict):
    """Convert a Lexical root state into a docxtpl RichText object (or plain str).

    Walks all text nodes and maps format flags + CSS style properties to
    RichText.add() kwargs so docxtpl preserves bold/italic/color/font/size
    in the rendered Word document.

    Returns a plain str (not RichText) when no text node has non-default
    formatting — avoids RichText overhead for unformatted content.
    """
    try:
        from docxtpl import RichText
    except ImportError:
        return lexical_to_plain_text(state)

    FLAG_BOLD, FLAG_ITALIC, FLAG_UNDERLINE = 1, 2, 8
    parts: list[tuple[str, dict]] = []
    has_format = False

    for para in (state.get("root") or {}).get("children") or []:
        for node in para.get("children") or []:
            if node.get("type") != "text":
                continue
            text = node.get("text", "")
            if not text:
                continue
            fmt = node.get("format", 0)
            css = _parse_css(node.get("style", ""))
            kwargs: dict = {}
            if fmt & FLAG_BOLD:
                kwargs["bold"] = True; has_format = True
            if fmt & FLAG_ITALIC:
                kwargs["italic"] = True; has_format = True
            if fmt & FLAG_UNDERLINE:
                kwargs["underline"] = True; has_format = True
            raw_color = css.get("color", "")
            if raw_color:
                _hx = _to_hex_color(raw_color)
                kwargs["color"] = _hx if _hx else raw_color.lstrip("#")
                has_format = True
            raw_font = css.get("font-family", "")
            if raw_font:
                kwargs["font"] = raw_font; has_format = True
            sz_str = css.get("font-size", "").replace("pt", "").strip()
            if sz_str.isdigit():
                kwargs["size"] = int(sz_str) * 2; has_format = True  # docx uses half-points
            raw_bg = css.get("background-color", "")
            if raw_bg:
                kwargs["highlight"] = raw_bg.lstrip("#"); has_format = True
            parts.append((text, kwargs))

    if not parts:
        return ""
    if not has_format:
        return " ".join(t for t, _ in parts)

    rt = RichText()
    for text, kwargs in parts:
        rt.add(text, **kwargs)
    return rt


# ---------------------------------------------------------------------------
# Core builder — replace BODY only when Document Engine is ready
# ---------------------------------------------------------------------------

def text_to_lexical_node(
    text: str,
    style_defaults: dict | None = None,
    bold: bool = False,
    align: str = "",
    underline: bool = False,
) -> dict:
    """Convert a plain text string into a Lexical root state dict.

    Handles multi-line text:
      - Double newline (\\n\\n) → separate Lexical paragraph nodes (paragraph spacing)
      - Single newline (\\n) → LineBreak node within the same paragraph (no extra spacing)

    Args:
        text:           Plain text. \\n\\n = paragraph break, \\n = line break.
        style_defaults: Optional font/size/color overrides.
        bold:           Apply bold format to all text nodes.
        align:          Lexical paragraph format string, e.g. "center", "right", "".
        underline:      Apply underline format to all text nodes.
    """
    sd = style_defaults or {}
    font_family = sd.get("font_family", "Times New Roman")
    font_size   = sd.get("font_size_pt", 12)
    text_color  = sd.get("text_color", "#000000")
    inline_style = (
        f"font-family:{font_family};"
        f"font-size:{font_size}pt;"
        f"color:{text_color};"
    )

    _fmt = 0
    if bold:
        _fmt |= 1   # bold bitmask
    if underline:
        _fmt |= 8   # underline bitmask

    def _text_node(t: str) -> dict:
        return {
            "type": "text", "version": 1,
            "text": t, "format": _fmt,
            "detail": 0, "mode": "normal", "style": inline_style,
        }

    def _linebreak_node() -> dict:
        return {"type": "linebreak", "version": 1}

    def _make_para(lines: list[str]) -> dict:
        """Build one Lexical paragraph from a list of lines.
        Lines are joined with linebreak nodes (no extra paragraph spacing)."""
        children: list[dict] = []
        for i, line in enumerate(lines):
            if i > 0:
                children.append(_linebreak_node())
            children.append(_text_node(line))
        if not children:
            children.append(_text_node(""))
        return {
            "type": "paragraph", "version": 1,
            "format": align, "indent": 0, "direction": "ltr",
            "children": children,
        }

    # Split on \n\n for paragraph breaks; within each block split on \n for line breaks
    logical_blocks = (text or "").split("\n\n")
    paragraphs = [_make_para(block.split("\n")) for block in logical_blocks]
    if not paragraphs:
        paragraphs = [_make_para([""])]

    return {"root": {"type": "root", "version": 1, "children": paragraphs}}


# ---------------------------------------------------------------------------
# Section injectors — used by fill_adapter and future Document Engine bridge
# ---------------------------------------------------------------------------

def inject_text_into_section(
    section: dict,
    text: str,
    style_defaults: dict | None = None,
) -> dict:
    """
    WHAT: Return a copy of `section` with both content.text and richtext.state
          updated from the given plain text string.

    WHY TWO FIELDS:
        - content.text is read by the current docxtpl renderer (sandbox).
        - richtext.state is read by the Lexical frontend editor.
        Both must stay in sync so the same document looks correct in both contexts.

    WORKS FOR: Single-text sections — subject, reference_number, date, salutation.
    DOES NOT MUTATE the input dict (returns deep copy).

    Args:
        section:        Section dict from skeleton (will be deep-copied).
        text:           Plain text from LLM to inject.
        style_defaults: Forwarded to text_to_lexical_node().
    """
    # WHAT: Deep copy prevents accidental mutation of the source skeleton.
    # WHY:  Skeletons are loaded from JSON files and cached. Mutating them would
    #       corrupt every subsequent document creation in the same process.
    section = copy.deepcopy(section)
    content = section.setdefault("content", {})

    # WHAT: Set the flat text field (renderer path).
    content["text"] = text

    # WHAT: Set the Lexical state (editor path).
    richtext = content.setdefault("richtext", {})
    richtext["format"] = "lexical"
    richtext["state"]  = text_to_lexical_node(text, style_defaults)

    return section


def inject_paras_into_section(
    section: dict,
    para_texts: list[str],
    style_defaults: dict | None = None,
) -> dict:
    """
    WHAT: Return a copy of `section` (type: numbered_paragraphs) with each item's
          text and richtext.state updated from the corresponding string in para_texts.

    WHY ITEMS NOT REPLACED ENTIRELY:
        items[] may contain non-text fields (id, number, style_overrides) set by
        the skeleton or by previous edits. We only overwrite text + richtext.state,
        leaving all other item fields intact so patch_ops.py keeps working.

    BOUNDARY BEHAVIOUR:
        - If para_texts has fewer entries than items[], remaining items stay unchanged.
        - If para_texts has more entries than items[], extra texts are silently ignored.
        WHY: The Blueprint controls how many paragraphs a section may contain.
             Adding items here would bypass Blueprint validation.
             To add paragraphs, use the Document Engine's insert_section call instead.

    DOES NOT MUTATE the input dict (returns deep copy).

    Args:
        section:    numbered_paragraphs section dict from skeleton.
        para_texts: List of paragraph strings, e.g. ["1. First para", "2. Second para"].
        style_defaults: Forwarded to text_to_lexical_node().
    """
    section = copy.deepcopy(section)
    content = section.setdefault("content", {})
    items: list[dict[str, Any]] = content.setdefault("items", [])

    for idx, text in enumerate(para_texts):
        # WHAT: Stop when para_texts is longer than the items array.
        # WHY:  Blueprint controls item count — don't silently add items here.
        if idx >= len(items):
            break

        item = items[idx]

        # WHAT: Update flat text (renderer path).
        item["text"] = text

        # WHAT: Update Lexical state (editor path).
        richtext = item.setdefault("richtext", {})
        richtext["format"] = "lexical"
        richtext["state"]  = text_to_lexical_node(text, style_defaults)

    return section


# ---------------------------------------------------------------------------
# Utility — extract plain text from Lexical state (for logs, audit, transforms)
# ---------------------------------------------------------------------------

def lexical_to_plain_text(state: dict) -> str:
    """
    WHAT: Extract concatenated plain text from a Lexical root state dict.

    WHY THIS EXISTS:
        content_transform.py needs the plain text of an existing paragraph
        to pass to the LLM for rewrite/expand/shorten transforms.
        The source of truth in production will be richtext.state, not content.text.
        This utility bridges that gap without coupling transform logic to Lexical internals.

    Args:
        state: The dict stored in section.content.richtext.state.

    Returns:
        Plain text string, or "" if state is malformed.
    """
    try:
        root = state.get("root") or {}
        parts: list[str] = []
        for para in root.get("children") or []:
            for node in para.get("children") or []:
                if node.get("type") == "text":
                    parts.append(node.get("text") or "")
        return " ".join(parts).strip()
    except Exception:
        # WHAT: Swallow all exceptions and return empty string.
        # WHY:  This is a read-only utility used in logs and transform prep.
        #       A crash here should never block document operations.
        return ""
