"""
app/ml/rag_context.py

Purpose:
- Build small, high-signal context for LLM calls (generation + edits).
- Uses your existing vector retrieval: app/services/rag.py::search_rules

Design:
- Retrieve rules filtered by doctype (plus GENERAL_RULES as fallback is already in search_rules)
- Cap context length to keep LLM responses stable

Output:
- dict with:
  - rules_context: concatenated rule chunks
"""

from __future__ import annotations
from typing import Dict, List
from sqlalchemy.orm import Session

from app.services.rag import search_rules


def _join_chunks(chunks: List[str], max_chars: int = 6000) -> str:
    """
    Join chunks with separators and cap to max_chars.
    Prevents overloading the prompt context window.
    """
    out: List[str] = []
    total = 0

    for c in chunks:
        c = (c or "").strip()
        if not c:
            continue

        # separator cost approx
        extra = len(c) + 5
        if total + extra > max_chars:
            break

        out.append(c)
        total += extra

    return "\n\n---\n\n".join(out)


def build_rules_context(db: Session, doctype: str, query: str, k: int = 6) -> Dict[str, str]:
    """
    Retrieve and return rules context for a doctype.

    Notes:
    - search_rules already uses .where(doc_type in [doctype, GENERAL_RULES])
    - so you will always get fallback rules even if the doctype pool is small
    """
    chunks = search_rules(db=db, query=query, doc_type=doctype, k=k)
    texts = [c.text for c in chunks]
    return {"rules_context": _join_chunks(texts, max_chars=6000)}
