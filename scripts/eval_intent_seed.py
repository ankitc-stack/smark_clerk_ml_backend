from __future__ import annotations

"""
Seed evaluator for command intent extraction.

Why this script exists:
- Provides a fast regression gate (40 curated cases) before larger eval sets.
- Runs offline by forcing rule fallback, so it is stable in sandbox environments.
"""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# Force deterministic fallback for repeatable seed evaluation.
os.environ["COMMAND_INTENT_USE_LLM"] = "false"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas import ActionObject, CommandContext
from app.services.intent_extractor import extract_action_object_with_meta

def _structured_fixture(doc_type: str) -> dict:
    return {
        "meta": {"document_type": doc_type},
        "sections": [
            {
                "id": "sec_body_001",
                "type": "numbered_paragraphs",
                "content": {
                    "items": [
                        {"id": "p1", "text": "First paragraph"},
                        {"id": "p2", "text": "Second paragraph"},
                        {"id": "p3", "text": "Third paragraph"},
                    ]
                },
            },
            {
                "id": "sec_body_002",
                "type": "numbered_paragraphs",
                "content": {
                    "items": [
                        {"id": "p1", "text": "Auxiliary section paragraph one"},
                    ]
                },
            },
        ],
    }


def _pct(numerator: int, denominator: int) -> float:
    return (numerator / denominator) * 100.0 if denominator else 0.0


def _is_valid_action_json(obj: ActionObject) -> bool:
    try:
        # Round-trip through JSON to verify contract-serializable output.
        serialized = json.dumps(obj.model_dump(mode="json"), ensure_ascii=False)
        ActionObject.model_validate_json(serialized)
        return True
    except Exception:
        return False


def _target_checks(expected: dict, obj: ActionObject) -> list[tuple[str, object, object]]:
    checks: list[tuple[str, object, object]] = []
    if "target_para_id" in expected:
        checks.append(("target_para_id", obj.target.para_id, expected["target_para_id"]))
    if "target_section_id" in expected:
        checks.append(("target_section_id", obj.target.section_id, expected["target_section_id"]))
    if "target_anchor_section_id" in expected:
        # For MOVE_SECTION, anchor section is carried in ActionTarget.para_id.
        checks.append(("target_anchor_section_id", obj.target.para_id, expected["target_anchor_section_id"]))
    if "target_position_idx" in expected:
        checks.append(("target_position_idx", obj.target.para_index, expected["target_position_idx"]))
    return checks


async def main():
    parser = argparse.ArgumentParser(description="Evaluate command intent cases")
    parser.add_argument("--cases", default="tests/ml/intent_cases_seed.json", help="Path to intent case JSON")
    args = parser.parse_args()

    cases_path = Path(args.cases)
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = raw.get("cases", [])
    total = 0
    passed = 0
    valid_json_count = 0
    action_correct = 0
    target_total = 0
    target_correct = 0
    clarify_tp = 0
    clarify_fp = 0
    clarify_fn = 0
    wrong_edit_count = 0
    failures: list[str] = []

    for idx, case in enumerate(cases, start=1):
        total += 1
        prompt = case["prompt"]
        expected = case["expected"]
        context = CommandContext.model_validate(case["context"])
        structured = _structured_fixture(case["doc_type"])

        try:
            result = await extract_action_object_with_meta(prompt, context, structured)
            obj = result.action_object
        except Exception as ex:
            if not expected["clarify"]:
                wrong_edit_count += 1
            failures.append(
                f"#{idx} prompt='{prompt}' expected={expected} extraction_error={type(ex).__name__}: {ex}"
            )
            continue

        valid_json = _is_valid_action_json(obj)
        if valid_json:
            valid_json_count += 1

        if obj.action.value == expected["action"]:
            action_correct += 1

        expected_clarify = bool(expected["clarify"])
        pred_clarify = bool(obj.needs_clarification)
        expected_scope = expected.get("scope")
        if pred_clarify and expected_clarify:
            clarify_tp += 1
        elif pred_clarify and not expected_clarify:
            clarify_fp += 1
        elif (not pred_clarify) and expected_clarify:
            clarify_fn += 1

        target_checks = _target_checks(expected, obj)
        if not expected_clarify and target_checks:
            target_total += 1
            if all(actual == exp for _, actual, exp in target_checks):
                target_correct += 1

        ok = True
        if not valid_json:
            ok = False
        if obj.action.value != expected["action"]:
            ok = False
        if pred_clarify != expected_clarify:
            ok = False
        if expected_scope and obj.scope.value != expected_scope:
            ok = False
        if not expected_clarify and target_checks:
            if any(actual != exp for _, actual, exp in target_checks):
                ok = False

        if ok:
            passed += 1
        else:
            if not expected_clarify:
                wrong_edit_count += 1
            target_debug = {
                "section_id": obj.target.section_id,
                "para_id": obj.target.para_id,
                "para_index": obj.target.para_index,
            }
            failures.append(
                f"#{idx} prompt='{prompt}' expected={expected} got={{'action': '{obj.action.value}', 'scope': '{obj.scope.value}', 'clarify': {obj.needs_clarification}, 'target': {target_debug}, 'valid_json': {valid_json}}}"
            )

    accuracy = _pct(passed, total)
    wrong_edit_rate = _pct(wrong_edit_count, total)
    valid_json_rate = _pct(valid_json_count, total)
    action_accuracy = _pct(action_correct, total)
    target_accuracy = _pct(target_correct, target_total)
    clarify_precision = _pct(clarify_tp, clarify_tp + clarify_fp)
    clarify_recall = _pct(clarify_tp, clarify_tp + clarify_fn)

    print(f"Cases file: {cases_path}")
    print(f"Total: {total}")
    print(f"Passed: {passed}")
    print(f"Accuracy: {accuracy:.2f}%")
    print(f"Valid JSON rate: {valid_json_rate:.2f}%")
    print(f"Action accuracy: {action_accuracy:.2f}%")
    print(f"Target accuracy: {target_accuracy:.2f}% ({target_correct}/{target_total})")
    print(
        f"Clarify precision/recall: {clarify_precision:.2f}% / {clarify_recall:.2f}% "
        f"(tp={clarify_tp}, fp={clarify_fp}, fn={clarify_fn})"
    )
    print(f"Wrong edit rate: {wrong_edit_rate:.2f}%")

    if failures:
        print("\nFailures:")
        for line in failures[:20]:
            print(line)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
