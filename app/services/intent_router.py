from __future__ import annotations

"""Intent router (lightweight).

Goal: route requests without needing an LLM in most cases.
We bucket prompts into:
- text_fill_only: fill skeleton JSON text fields
- format_rich_edit: formatting/structure edits (often patch-ops)
- needs_extraction: user uploaded file to extract from

This is used by the new structured JSON flow endpoints:
- POST /documents/fill
- POST /documents/{document_id}/patch (optional prompt)
"""

from dataclasses import dataclass

FORMAT_KWS = [
    "bold", "italic", "underline", "highlight",
    "align", "left", "right", "center", "justify",
    "font", "size", "spacing", "margin", "indent",
    "move signature", "signature to left", "header", "footer",
]
STRUCT_KWS = [
    "add paragraph", "insert paragraph", "delete paragraph", "remove para",
    "bullet", "numbering", "add point", "remove point",
]
EXTRACT_KWS = ["extract", "from this file", "use uploaded", "use attached", "scan", "ocr", "pdf", "image"]

@dataclass
class IntentResult:
    intent: str
    confidence: float
    reasons: list[str]

def route_intent(prompt: str, has_file: bool, mode: str) -> IntentResult:
    p = (prompt or "").lower()

    if has_file:
        return IntentResult("needs_extraction", 0.95, ["file_uploaded"])

    if any(k in p for k in EXTRACT_KWS):
        return IntentResult("needs_extraction", 0.80, ["extract_keyword"])

    if any(k in p for k in FORMAT_KWS) or any(k in p for k in STRUCT_KWS):
        return IntentResult("format_rich_edit", 0.85, ["format_or_structure_keywords"])

    return IntentResult("text_fill_only", 0.75, ["default_fill"])
