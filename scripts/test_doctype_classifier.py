"""
scripts/test_doctype_classifier.py

Purpose:
- Quick smoke test for Step 2 classifier.
- Runs a few prompts and prints results.

Run:
  python scripts/test_doctype_classifier.py
"""

from __future__ import annotations
import asyncio
import sys
from pathlib import Path

# Allow running this file directly from repo root without setting PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ml.doctype_classifier import classify_doctype

PROMPTS = [
    "Write a demi official letter: My dear Sharma, please coordinate for visit next week.",
    "Generate a Government of India letter with subject and numbered paras regarding procurement update.",
    "Create a leave certificate for 10 days EL from 01 Mar 2026 to 10 Mar 2026 with leave address in Pune.",
    "Prepare movement order to proceed from Delhi to Jaipur for duty on 20 Feb 2026 and report back by 25 Feb 2026.",
    "hi",
]

async def main():
    for p in PROMPTS:
        r = await classify_doctype(p)
        print("\nPROMPT:", p)
        print("RESULT:", r.model_dump())

if __name__ == "__main__":
    asyncio.run(main())
