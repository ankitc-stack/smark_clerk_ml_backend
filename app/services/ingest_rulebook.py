from __future__ import annotations
import fitz
from sqlalchemy.orm import Session
from app.models import RuleChunk
from app.providers.embedding_provider import embed
from app.ml.rulebook_doctype import classify_rule_chunk

ALLOWED = {"DO_LETTER", "GOI_LETTER", "LEAVE_CERTIFICATE", "MOVEMENT_ORDER", "GENERAL_RULES"}

def _chunk(text: str, max_chars: int = 1400, overlap: int = 180):
    text = " ".join(text.split())
    out = []
    i = 0
    while i < len(text):
        out.append(text[i:i+max_chars])
        i = i + max_chars - overlap
    return [c.strip() for c in out if c.strip()]

def ingest_pdf(db: Session, pdf_path: str):
    doc = fitz.open(pdf_path)
    for pidx in range(len(doc)):
        page = doc[pidx]
        text = page.get_text("text") or ""
        if not text.strip():
            continue
        for ch in _chunk(text):
            dt = classify_rule_chunk(ch)
            if dt not in ALLOWED:
                dt = "GENERAL_RULES"
            db.add(RuleChunk(
                doc_type=dt,
                page_start=pidx+1,
                page_end=pidx+1,
                text=ch,
                embedding=embed(ch)
            ))
    db.commit()
