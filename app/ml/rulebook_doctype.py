"""
app/ml/rulebook_doctype.py

Purpose:
- Assign a document type label to a chunk of rulebook text during ingestion.
- This fixes the current problem: most chunks become GENERAL_RULES, so RAG is weak.

Approach:
- Use scoring with strong anchors (layout headings, appendix language)
- Use small secondary boosts
- If best score is low -> GENERAL_RULES
"""

from __future__ import annotations
from typing import Dict


def classify_rule_chunk(text: str) -> str:
    t = (text or "").upper()

    scores: Dict[str, int] = {
        "DO_LETTER": 0,
        "GOI_LETTER": 0,
        "LEAVE_CERTIFICATE": 0,
        "MOVEMENT_ORDER": 0,
        "GENERAL_RULES": 1,  # baseline
    }

    # --- Strong anchors ---
    if "LAYOUT OF A GOVERNMENT OF INDIA LETTER" in t or "GOVERNMENT OF INDIA" in t:
        scores["GOI_LETTER"] += 6
    if "APPENDIX C" in t and "GOVERNMENT OF INDIA" in t:
        scores["GOI_LETTER"] += 8

    if "DEMI OFFICIAL" in t or "DEMI-OFFICIAL" in t or "MY DEAR" in t or "D.O." in t:
        scores["DO_LETTER"] += 6

    if "LEAVE CERTIFICATE" in t or "SPARE CHIT" in t or "LEAVE ADDRESS" in t:
        scores["LEAVE_CERTIFICATE"] += 6
    if "LEAVE" in t and ("CERTIFICATE" in t or "APPLICATION" in t or "LEAVE ADDRESS" in t):
        scores["LEAVE_CERTIFICATE"] += 3

    if "MOVEMENT ORDER" in t or "PROCEED TO" in t or "ITINERARY" in t:
        scores["MOVEMENT_ORDER"] += 5
    if "LAYOUT OF A SERVICE LETTER" in t:
        scores["MOVEMENT_ORDER"] += 2
        scores["GENERAL_RULES"] += 1
    if "LAYOUT OF MESSAGE" in t or "SIGNAL FORM" in t or "DTG" in t:
        scores["GENERAL_RULES"] += 2

    # --- Secondary signals ---
    if "SUBJECT:" in t:
        scores["GOI_LETTER"] += 1
        scores["MOVEMENT_ORDER"] += 1

    if "YOURS FAITHFULLY" in t or "YOURS SINCERELY" in t:
        scores["GOI_LETTER"] += 1
        scores["DO_LETTER"] += 1

    if "ANNEXURE" in t or "ENCLOSURE" in t or "COPY TO" in t:
        scores["GOI_LETTER"] += 1
        scores["MOVEMENT_ORDER"] += 1

    # Pick best score
    best = max(scores.items(), key=lambda kv: kv[1])[0]

    # Threshold: if weak match, keep GENERAL_RULES
    if best != "GENERAL_RULES" and scores[best] < 3:
        return "GENERAL_RULES"

    return best
