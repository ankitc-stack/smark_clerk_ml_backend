from __future__ import annotations

import json
from pathlib import Path


DOC_TYPES = ["GOI_LETTER", "DO_LETTER", "MOVEMENT_ORDER", "LEAVE_CERTIFICATE"]


def build_intent_cases() -> list[dict]:
    applied_patterns = [
        ("Make paragraph {n} formal", "CHANGE_TONE"),
        ("Make para {n} formal", "CHANGE_TONE"),
        ("Rewrite paragraph {n}", "REWRITE_CONTENT"),
        ("Rephrase paragraph {n}", "REWRITE_CONTENT"),
        ("Expand paragraph {n}", "EXPAND_CONTENT"),
        ("Expand para {n}", "EXPAND_CONTENT"),
        ("Shorten paragraph {n}", "SHORTEN_CONTENT"),
        ("Make paragraph {n} concise", "SHORTEN_CONTENT"),
    ]
    clarify_patterns = [
        ("Make paragraph formal", "CHANGE_TONE"),
        ("Rewrite paragraph", "REWRITE_CONTENT"),
        ("Expand paragraph", "EXPAND_CONTENT"),
        ("Shorten paragraph", "SHORTEN_CONTENT"),
        ("Make this formal", "CHANGE_TONE"),
        ("Make here concise", "SHORTEN_CONTENT"),
        ("Rewrite this", "REWRITE_CONTENT"),
        ("Expand this", "EXPAND_CONTENT"),
        ("Shorten this", "SHORTEN_CONTENT"),
        ("Make it formal", "CHANGE_TONE"),
    ]
    adversarial_cases = [
        {
            "prompt": "Make it formal",
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "expected": {"action": "CHANGE_TONE", "clarify": True},
        },
        {
            "prompt": "Make it formal please",
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "expected": {"action": "CHANGE_TONE", "clarify": True},
        },
        {
            "prompt": "दूसरा paragraph concise करो",
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 2,
            },
            "expected": {
                "action": "SHORTEN_CONTENT",
                "clarify": False,
                "target_para_id": "p2",
            },
        },
        {
            "prompt": "Shorten para 2 to two lines",
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 2,
            },
            "expected": {
                "action": "SHORTEN_CONTENT",
                "clarify": False,
                "target_para_id": "p2",
            },
        },
        {
            "prompt": "Rewrite para 2 but keep meaning",
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 2,
            },
            "expected": {
                "action": "REWRITE_CONTENT",
                "clarify": False,
                "target_para_id": "p2",
            },
        },
        {
            "prompt": "Expand p3 with one more line",
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 3,
            },
            "expected": {
                "action": "EXPAND_CONTENT",
                "clarify": False,
                "target_para_id": "p3",
            },
        },
    ]

    cases: list[dict] = []
    for doc_type in DOC_TYPES:
        # 40 deterministic applied cases per doc type.
        for idx in range(40):
            pattern, action = applied_patterns[idx % len(applied_patterns)]
            n = (idx % 3) + 1
            cases.append(
                {
                    "doc_type": doc_type,
                    "prompt": pattern.format(n=n),
                    "context": {
                        "current_section_id": "sec_body_001",
                        "selected_section_ids": [],
                        "cursor_position": idx % 4,
                    },
                    "expected": {
                        "action": action,
                        "clarify": False,
                        "target_para_id": f"p{n}",
                    },
                }
            )

        # 10 deterministic clarify cases per doc type.
        for pattern, action in clarify_patterns:
            cases.append(
                {
                    "doc_type": doc_type,
                    "prompt": pattern,
                    "context": {
                        "current_section_id": "sec_body_001",
                        "selected_section_ids": [],
                        "cursor_position": None,
                    },
                    "expected": {
                        "action": action,
                        "clarify": True,
                    },
                }
            )

        # 6 adversarial/realistic cases per doc type.
        for case in adversarial_cases:
            cases.append(
                {
                    "doc_type": doc_type,
                    "prompt": case["prompt"],
                    "context": case["context"],
                    "expected": case["expected"],
                }
            )

    assert len(cases) == 224
    return cases


def build_structural_intent_cases() -> list[dict]:
    applied_templates = [
        (
            "Add paragraph after paragrph 1 with Additional compliance sentence.",
            {
                "action": "ADD_PARAGRAPH",
                "clarify": False,
                "scope": "PARAGRAPH",
                "target_section_id": "sec_body_001",
                "target_para_id": "p1",
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Add another paragrph",
            {
                "action": "ADD_PARAGRAPH",
                "clarify": False,
                "scope": "PARAGRAPH",
                "target_section_id": "sec_body_001",
                "target_para_id": "p3",
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Remove paragrph 2",
            {
                "action": "REMOVE_PARAGRAPH",
                "clarify": False,
                "scope": "PARAGRAPH",
                "target_section_id": "sec_body_001",
                "target_para_id": "p2",
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Add seciton after body 1",
            {
                "action": "INSERT_SECTION",
                "clarify": False,
                "scope": "SECTION",
                "target_section_id": "sec_body_001",
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Delete seciton body 2",
            {
                "action": "DELETE_SECTION",
                "clarify": False,
                "scope": "SECTION",
                "target_section_id": "sec_body_002",
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Move seciton body 2 befor body 1",
            {
                "action": "MOVE_SECTION",
                "clarify": False,
                "scope": "SECTION",
                "target_section_id": "sec_body_002",
                "target_anchor_section_id": "sec_body_001",
                "target_position_idx": 0,
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Move seciton body 1 aftr body 2",
            {
                "action": "MOVE_SECTION",
                "clarify": False,
                "scope": "SECTION",
                "target_section_id": "sec_body_001",
                "target_anchor_section_id": "sec_body_002",
                "target_position_idx": 1,
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Move seciton body 2",
            {
                "action": "MOVE_SECTION",
                "clarify": False,
                "scope": "SECTION",
                "target_section_id": "sec_body_002",
                "target_anchor_section_id": "sec_body_001",
                "target_position_idx": 1,
            },
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
    ]
    clarify_templates = [
        (
            "Move section",
            {"action": "MOVE_SECTION", "clarify": True, "scope": "SECTION"},
            {"current_section_id": "sec_body_001", "selected_section_ids": [], "cursor_position": None},
        ),
        (
            "Delete section",
            {"action": "DELETE_SECTION", "clarify": True, "scope": "SECTION"},
            {"current_section_id": None, "selected_section_ids": [], "cursor_position": None},
        ),
    ]

    cases: list[dict] = []
    for doc_type in DOC_TYPES:
        for prompt, expected, context in applied_templates:
            cases.append(
                {
                    "doc_type": doc_type,
                    "prompt": prompt,
                    "context": context,
                    "expected": expected,
                }
            )
        for prompt, expected, context in clarify_templates:
            cases.append(
                {
                    "doc_type": doc_type,
                    "prompt": prompt,
                    "context": context,
                    "expected": expected,
                }
            )
    assert len(cases) == 40
    return cases


def build_voice_cases() -> list[dict]:
    applied_patterns = [
        ("Make paragraph {n} formal", "CHANGE_TONE", "en"),
        ("Make para {n} formal", "CHANGE_TONE", "en"),
        ("paragraph {n} formal karo", "CHANGE_TONE", "hinglish"),
        ("para {n} ko formal karo", "CHANGE_TONE", "hinglish"),
        ("Rewrite paragraph {n}", "REWRITE_CONTENT", "en"),
        ("paragraph {n} rewrite karo", "REWRITE_CONTENT", "hinglish"),
        ("Expand paragraph {n}", "EXPAND_CONTENT", "en"),
        ("paragraph {n} expand karo", "EXPAND_CONTENT", "hinglish"),
        ("Shorten paragraph {n}", "SHORTEN_CONTENT", "en"),
        ("paragraph {n} shorten karo", "SHORTEN_CONTENT", "hinglish"),
    ]

    cases: list[dict] = []

    # 78 applied cases.
    for idx in range(78):
        pattern, action, lang = applied_patterns[idx % len(applied_patterns)]
        n = (idx % 3) + 1
        cases.append(
            {
                "id": f"vc{idx + 1:03d}",
                "lang": lang,
                "transcript": pattern.format(n=n),
                "expected": {
                    "status": "applied",
                    "action": action,
                    "target_para_id": f"p{n}",
                },
            }
        )

    # 10 low-confidence recoverable clarifications.
    for i in range(10):
        n = (i % 3) + 1
        cases.append(
            {
                "id": f"vc{len(cases) + 1:03d}",
                "lang": "hinglish" if i % 2 else "en",
                "transcript": f"make paragraph {n} formal",
                "simulate_stt_error": "low_confidence",
                "expected": {"status": "needs_clarification"},
            }
        )

    # 4 STT hard failures.
    for _ in range(4):
        cases.append(
            {
                "id": f"vc{len(cases) + 1:03d}",
                "lang": "en",
                "transcript": "",
                "simulate_stt_error": "transcription_failure",
                "expected": {"status": "error", "error_code": "could_not_understand_audio"},
            }
        )

    # 4 unsupported-action clarifications (safe fallback path).
    unsupported_prompts = [
        "Make paragraph 2 neutral",
        "Insert this line",
        "Delete paragraph 1",
        "Replace paragraph 2 with approved text",
    ]
    for prompt in unsupported_prompts:
        cases.append(
            {
                "id": f"vc{len(cases) + 1:03d}",
                "lang": "en",
                "transcript": prompt,
                "expected": {"status": "needs_clarification"},
            }
        )

    # 4 version conflicts.
    for i in range(4):
        n = (i % 3) + 1
        cases.append(
            {
                "id": f"vc{len(cases) + 1:03d}",
                "lang": "en",
                "transcript": f"Make paragraph {n} formal",
                "simulate_version_conflict": True,
                "expected": {"status": "error", "error_code": "version_conflict"},
            }
        )

    assert len(cases) == 100
    return cases


def main() -> None:
    intent_cases = {"cases": build_intent_cases()}
    structural_intent_cases = {"cases": build_structural_intent_cases()}
    voice_cases = {"cases": build_voice_cases()}

    intent_path = Path("tests/ml/intent_cases.json")
    structural_intent_path = Path("tests/ml/intent_cases_structural.json")
    voice_path = Path("tests/ml/voice_cases.json")
    intent_path.parent.mkdir(parents=True, exist_ok=True)

    intent_path.write_text(json.dumps(intent_cases, indent=2), encoding="utf-8")
    structural_intent_path.write_text(json.dumps(structural_intent_cases, indent=2), encoding="utf-8")
    voice_path.write_text(json.dumps(voice_cases, indent=2), encoding="utf-8")

    print(f"Wrote {len(intent_cases['cases'])} intent cases -> {intent_path}")
    print(
        f"Wrote {len(structural_intent_cases['cases'])} structural intent cases -> "
        f"{structural_intent_path}"
    )
    print(f"Wrote {len(voice_cases['cases'])} voice cases -> {voice_path}")


if __name__ == "__main__":
    main()
