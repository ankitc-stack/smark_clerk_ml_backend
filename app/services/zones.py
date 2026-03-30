from __future__ import annotations
from docx import Document
import re

ANCHORS = [
  ("SUBJECT", re.compile(r"^\s*Subject\s*:", re.I)),
  ("COPY_TO", re.compile(r"^\s*Copy\s*to\s*:?-", re.I)),
  ("DISTRIBUTION", re.compile(r"^\s*Distr\s*:?", re.I)),
  ("STATION", re.compile(r"^\s*Station\s*:", re.I)),
  ("DATED", re.compile(r"^\s*Dated\s*:", re.I)),
  ("SIGNATURE", re.compile(r"^\s*\(.*\)\s*$")),  # (Name)
]

def suggest_zones(docx_path: str) -> dict:
    """Returns minimal 'zones' map based on anchor detection."""
    doc = Document(docx_path)
    hits = []
    for i, p in enumerate(doc.paragraphs):
        t = (p.text or "").strip()
        if not t:
            continue
        for key, rx in ANCHORS:
            if rx.search(t):
                hits.append({"key": key, "paragraph_index": i, "text": t})
    return {"anchors": hits}
