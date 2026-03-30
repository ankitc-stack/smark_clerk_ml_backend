from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asyncio
from app.db import SessionLocal
from app.ml.slots.movement_order import draft_numbered_paras, draft_distribution_lines

PROMPT = "Prepare movement order to proceed from Delhi to Jaipur for duty on 20 Feb 2026 and report back by 25 Feb 2026. Include copy to Dir (Ops) and AAO."

async def main():
    db = SessionLocal()
    try:
        paras = await draft_numbered_paras(db, PROMPT, 2, 4)
        dist = await draft_distribution_lines(db, PROMPT, 6)
        print("\n=== MOVEMENT SLOT OUTPUT ===")
        for p in paras:
            print(p)
        print("\nDistribution:")
        for l in dist:
            print(l)
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
