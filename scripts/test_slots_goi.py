"""
scripts/test_slots_goi.py

Purpose:
- Smoke test GOI slots end-to-end:
  - DB session
  - RAG retrieval
  - LLM JSON pipeline
  - slot output validation

Run:
  python scripts/test_slots_goi.py
"""

from __future__ import annotations

# --- sys.path bootstrap so "import app" works when running scripts directly ---
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# --- end bootstrap ---

import asyncio

from app.db import SessionLocal
from app.ml.slots.goi_letter import generate_subject, draft_numbered_paras


PROMPT = (
    "Subject: Update on issues likely to be raised during the forthcoming Session of Parliament.\n"
    "Request consolidated inputs and soft copy by 10.02.2024."
)


async def main():
    db = SessionLocal()
    try:
        subject = await generate_subject(db, PROMPT)
        paras = await draft_numbered_paras(db, PROMPT, min_paras=2, max_paras=3)

        print("\n=== GOI SLOT OUTPUT ===")
        print("Subject:", subject)
        print("\nParas:")
        for p in paras:
            print("-", p)

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
