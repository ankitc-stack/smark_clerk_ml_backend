"""
scripts/test_rag_retrieval.py

Purpose:
- Smoke test vector retrieval by doctype.
- Prints top retrieved rule chunks for each query.

Run:
  python scripts/test_rag_retrieval.py
"""

from __future__ import annotations

# --- sys.path bootstrap so "import app" works when running scripts directly ---
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# --- end bootstrap ---

from app.db import SessionLocal
from app.ml.rag_context import build_rules_context


TESTS = [
    ("GOI_LETTER", "layout of government of india letter subject numbered paragraphs"),
    ("DO_LETTER", "demi official letter my dear layout"),
    ("LEAVE_CERTIFICATE", "leave certificate spare chit leave address"),
    ("MOVEMENT_ORDER", "movement order proceed to itinerary distribution"),
]


def main():
    db = SessionLocal()
    try:
        for doctype, q in TESTS:
            ctx = build_rules_context(db, doctype, q, k=6)
            print("\n=================================================")
            print("DOCTYPE:", doctype)
            print("QUERY  :", q)
            print("-------------------------------------------------")
            print(ctx["rules_context"][:2500])  # print first 2500 chars only
            print("\n=================================================")
    finally:
        db.close()


if __name__ == "__main__":
    main()
