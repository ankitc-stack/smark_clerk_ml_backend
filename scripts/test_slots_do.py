from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asyncio
from app.db import SessionLocal
from app.ml.slots.do_letter import generate_salutation, draft_body_paras

PROMPT = "Write a demi official letter requesting Dir (MSSD) to send consolidated inputs by 10.02.2024."

async def main():
    db = SessionLocal()
    try:
        sal = await generate_salutation(db, PROMPT)
        paras = await draft_body_paras(db, PROMPT, 1, 2)
        print("\n=== DO SLOT OUTPUT ===")
        print("Salutation:", sal)
        print("Paras:")
        for p in paras:
            print("-", p)
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
