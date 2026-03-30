"""
scripts/test_json_pipeline.py

Purpose:
- Verify Step 1 works:
  "50 dummy calls and confirm no crashes, JSON always parsed or clean fallback"

How to run:
- From repo root:
    export OLLAMA_BASE_URL=http://localhost:11434
    export OLLAMA_CHAT_MODEL=llama3.1:8b
    export OLLAMA_TEMPERATURE=0.2
    python scripts/test_json_pipeline.py

Pass criteria:
- FAIL should be 0.
"""

from __future__ import annotations
import asyncio
import random
import sys

from app.ml.json_repair import call_and_parse_json

# Informal schema hint used by the repair step
SCHEMA_HINT = """
{
  "ok": true,
  "idx": 0,
  "echo": "string",
  "numbers": [1,2,3]
}
"""

# Strong instruction to force JSON-only outputs
SYSTEM = "You must output STRICT JSON only. No markdown, no extra text."


def build_user(idx: int) -> str:
    """
    Create different prompt variations to test robustness.
    Some prompts tempt the model to add extra text.
    Our json_guard + repair should handle it.
    """
    variants = [
        f"Return JSON with ok=true, idx={idx}, echo='hello', numbers=[1,2,3].",
        f"ONLY JSON. Keys: ok, idx, echo, numbers. idx={idx}.",
        f"Return a JSON object: ok true, idx {idx}, echo 'pipeline', numbers [1,2,3].",
        # Tempt it to add extra words; still must output JSON only.
        f"Briefly explain then return JSON. idx={idx}. (But output must be JSON only.)",
    ]
    return random.choice(variants)


async def main():
    ok = 0
    fail = 0

    # Make 50 sequential calls (simple & deterministic).
    # Later you can parallelize if you want load testing.
    for i in range(50):
        user_prompt = build_user(i)

        try:
            result = await call_and_parse_json(SYSTEM, user_prompt, SCHEMA_HINT)

            # Basic structural sanity check
            if not isinstance(result, dict):
                raise ValueError("Result is not a JSON object")
            if "ok" not in result:
                raise ValueError("Missing key: ok")

            ok += 1

        except Exception as e:
            # If this fails, you can print the raw model output by
            # adding logging inside json_repair.call_and_parse_json.
            print(f"[FAIL] idx={i}: {e}")
            fail += 1

    print("\n==== JSON PIPELINE TEST REPORT ====")
    print("Total: 50")
    print(f"OK   : {ok}")
    print(f"FAIL : {fail}")

    # Exit non-zero so CI can catch failures
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
