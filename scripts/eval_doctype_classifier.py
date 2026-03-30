"""
scripts/eval_doctype_classifier.py

Purpose:
- Evaluate classifier accuracy using labeled prompts.
- Prints accuracy + confusion counts.

Run:
  python scripts/eval_doctype_classifier.py
"""

from __future__ import annotations
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

# Allow running this file directly from repo root without setting PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ml.doctype_classifier import classify_doctype

CASES_PATH = Path("tests/ml/doctype_cases.json")

async def main():
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8-sig"))
    cases = raw.get("cases", []) if isinstance(raw, dict) else raw
    total = 0
    correct = 0

    # confusion[expected][predicted] = count
    confusion = defaultdict(lambda: defaultdict(int))

    for c in cases:
        prompt = c["prompt"]
        expected = c["expected"]
        res = await classify_doctype(prompt)
        predicted = res.doc_type.value

        total += 1
        if predicted == expected:
            correct += 1
        confusion[expected][predicted] += 1

    acc = (correct / total) * 100.0 if total else 0.0
    print(f"\nTotal: {total}")
    print(f"Correct: {correct}")
    print(f"Accuracy: {acc:.2f}%")

    print("\nConfusion Matrix (counts):")
    labels = ["DO_LETTER", "GOI_LETTER", "LEAVE_CERTIFICATE", "MOVEMENT_ORDER", "UNKNOWN"]
    header = "expected\\pred".ljust(22) + "".join([lbl.ljust(18) for lbl in labels])
    print(header)
    for exp in labels:
        row = exp.ljust(22)
        for pred in labels:
            row += str(confusion[exp][pred]).ljust(18)
        print(row)

if __name__ == "__main__":
    asyncio.run(main())
