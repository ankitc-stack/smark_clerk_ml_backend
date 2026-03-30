#!/usr/bin/env python
"""Unit tests for alignment patch ops (set_section_style / set_para_style with align).

Tests run directly against apply_patch_ops — no server or database needed.

Cases:
1. signee_block align right  → layout_hints.alignment changes, Lexical format unchanged
2. signee_block align left   → layout_hints.alignment changes to left
3. receiver_block align right→ layout_hints.alignment changes, Lexical format unchanged
4. subject section align center → Lexical paragraph.format changes (Word/Google Docs)
5. set_para_style align right   → para richtext paragraph.format changes
6. _detect_action: "move signee block to right" → SET_FORMAT
7. _detect_action: "move section 2 after section 3" → MOVE_SECTION (not misrouted)
8. _extract_format_style: extracts align from direction words
"""
from __future__ import annotations
import os, sys
from pathlib import Path

os.environ.setdefault("AUTO_BOOTSTRAP", "false")
os.environ.setdefault("ENABLE_COMMAND_API", "true")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.patch_ops import apply_patch_ops
from app.services.command_contract import _detect_action, _extract_format_style
from app.schemas import CommandAction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lexical_state(fmt: str = "") -> dict:
    return {
        "root": {
            "type": "root", "version": 1,
            "children": [{
                "type": "paragraph", "version": 1,
                "format": fmt, "indent": 0, "direction": "ltr",
                "children": [{"type": "text", "version": 1, "text": "RS Bhatia",
                               "format": 0, "detail": 0, "mode": "normal",
                               "style": "font-family:Times New Roman;"}],
            }],
        }
    }


def _signee_doc(alignment: str = "right") -> dict:
    return {
        "sections": [{
            "id": "sec_signee_001",
            "type": "signee_block",
            "content": {
                "signer_name": "RS Bhatia",
                "richtext": {"format": "lexical", "state": _lexical_state("")},
            },
            "layout_hints": {"alignment": alignment, "placement": "body_bottom"},
        }]
    }


def _receiver_doc(alignment: str = "left") -> dict:
    return {
        "sections": [{
            "id": "sec_receiver_001",
            "type": "receiver_block",
            "content": {
                "lines": ["Room No 000", "South Block"],
                "richtext": {"format": "lexical", "state": _lexical_state("")},
            },
            "layout_hints": {"alignment": alignment, "placement": "body_top"},
        }]
    }


def _subject_doc() -> dict:
    return {
        "sections": [{
            "id": "sec_subject_001",
            "type": "subject",
            "content": {
                "text": "SMART CLK SOFTWARE",
                "richtext": {"format": "lexical", "state": _lexical_state("")},
            },
        }]
    }


def _numbered_doc() -> dict:
    return {
        "sections": [{
            "id": "sec_body_001",
            "type": "numbered_paragraphs",
            "content": {"items": [{
                "id": "p1",
                "text": "Ref our tele conversation",
                "richtext": {"format": "lexical", "state": _lexical_state("")},
            }]},
        }]
    }


def _get_signee_section(doc: dict) -> dict:
    return next(s for s in doc["sections"] if s["id"] == "sec_signee_001")

def _get_receiver_section(doc: dict) -> dict:
    return next(s for s in doc["sections"] if s["id"] == "sec_receiver_001")

def _get_subject_section(doc: dict) -> dict:
    return next(s for s in doc["sections"] if s["id"] == "sec_subject_001")

def _para_lexical_fmt(section: dict) -> str:
    state = section["content"]["richtext"]["state"]
    return state["root"]["children"][0]["format"]

def _para_item_lexical_fmt(doc: dict) -> str:
    sec = next(s for s in doc["sections"] if s["id"] == "sec_body_001")
    item = sec["content"]["items"][0]
    return item["richtext"]["state"]["root"]["children"][0]["format"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
results = []


def check(name: str, condition: bool):
    status = PASS if condition else FAIL
    results.append((name, status))
    icon = "✓" if condition else "✗"
    print(f"  {icon} {name}")


# --- Test 1: signee_block align right keeps text, moves block ---
doc = _signee_doc(alignment="left")
out = apply_patch_ops(doc, [{"op": "set_section_style",
                              "target": {"section_id": "sec_signee_001"},
                              "style": {"align": "right"}}])
sec = _get_signee_section(out)
check("signee_block: layout_hints.alignment → right",
      sec["layout_hints"]["alignment"] == "right")
check("signee_block: Lexical paragraph.format stays unchanged (not text alignment)",
      _para_lexical_fmt(sec) == "")
check("signee_block: style_overrides.align set",
      sec.get("style_overrides", {}).get("align") == "right")

# --- Test 2: signee_block align left ---
doc = _signee_doc(alignment="right")
out = apply_patch_ops(doc, [{"op": "set_section_style",
                              "target": {"section_id": "sec_signee_001"},
                              "style": {"align": "left"}}])
sec = _get_signee_section(out)
check("signee_block: layout_hints.alignment → left",
      sec["layout_hints"]["alignment"] == "left")
check("signee_block: Lexical paragraph.format still unchanged",
      _para_lexical_fmt(sec) == "")

# --- Test 3: receiver_block align right ---
doc = _receiver_doc(alignment="left")
out = apply_patch_ops(doc, [{"op": "set_section_style",
                              "target": {"section_id": "sec_receiver_001"},
                              "style": {"align": "right"}}])
sec = _get_receiver_section(out)
check("receiver_block: layout_hints.alignment → right",
      sec["layout_hints"]["alignment"] == "right")
check("receiver_block: Lexical paragraph.format unchanged",
      _para_lexical_fmt(sec) == "")

# --- Test 4: subject section → text alignment like Word/Google Docs ---
doc = _subject_doc()
out = apply_patch_ops(doc, [{"op": "set_section_style",
                              "target": {"section_id": "sec_subject_001"},
                              "style": {"align": "center"}}])
sec = _get_subject_section(out)
check("subject: Lexical paragraph.format → center (Word/Google Docs style)",
      _para_lexical_fmt(sec) == "center")
check("subject: no layout_hints to update (no crash)",
      sec.get("layout_hints") is None)

# --- Test 5: set_para_style on numbered para ---
doc = _numbered_doc()
out = apply_patch_ops(doc, [{"op": "set_para_style",
                              "target": {"section_id": "sec_body_001", "para_id": "p1"},
                              "style": {"align": "right"}}])
check("numbered para: Lexical paragraph.format → right",
      _para_item_lexical_fmt(out) == "right")

# --- Test 6: _detect_action routing ---
action, _ = _detect_action("move signee block to right")
check("_detect_action: 'move signee block to right' → SET_FORMAT",
      action == CommandAction.SET_FORMAT)

action, _ = _detect_action("align receiver block left")
check("_detect_action: 'align receiver block left' → SET_FORMAT",
      action == CommandAction.SET_FORMAT)

action, _ = _detect_action("move section 2 after section 3")
check("_detect_action: 'move section 2 after section 3' → MOVE_SECTION (not misrouted)",
      action == CommandAction.MOVE_SECTION)

action, _ = _detect_action("move signee block to center")
check("_detect_action: 'move signee block to center' → SET_FORMAT",
      action == CommandAction.SET_FORMAT)

# --- Test 7: _extract_format_style alignment extraction ---
style = _extract_format_style("move signee block to right")
check("_extract_format_style: extracts align=right", style.get("align") == "right")

style = _extract_format_style("align receiver to left")
check("_extract_format_style: extracts align=left", style.get("align") == "left")

style = _extract_format_style("center the subject")
check("_extract_format_style: extracts align=center", style.get("align") == "center")

style = _extract_format_style("make subject bold")
check("_extract_format_style: bold only — no align extracted", "align" not in style)

# --- Test 8: input mutation guard (apply_patch_ops must not mutate original) ---
doc = _signee_doc(alignment="left")
import copy
original = copy.deepcopy(doc)
apply_patch_ops(doc, [{"op": "set_section_style",
                        "target": {"section_id": "sec_signee_001"},
                        "style": {"align": "right"}}])
check("apply_patch_ops: does not mutate original doc",
      doc["sections"][0]["layout_hints"]["alignment"] == "left")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
passed = sum(1 for _, s in results if s == PASS)
failed = sum(1 for _, s in results if s == FAIL)
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {len(results)} tests")
if failed:
    print("\nFailed tests:")
    for name, status in results:
        if status == FAIL:
            print(f"  ✗ {name}")
    sys.exit(1)
else:
    print("All tests passed.")