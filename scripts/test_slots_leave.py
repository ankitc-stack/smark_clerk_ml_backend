from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asyncio
from app.db import SessionLocal
from app.ml.slots.leave_certificate import extract_fields

PROMPT = "Create leave certificate for Hav Ram Kumar, 10 days EL from 01 Mar 2026 to 10 Mar 2026. Leave address: Pune, Contact: 9876543210."

async def main():
    db = SessionLocal()
    try:
        fields = await extract_fields(db, PROMPT)
        print("\n=== LEAVE SLOT OUTPUT ===")
        for k, v in fields.items():
            print(f"{k}: {v}")
    finally:
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
