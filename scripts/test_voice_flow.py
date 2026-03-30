from __future__ import annotations

"""
Task #5 voice flow smoke/eval runner.

What this script validates:
- /documents/{id}/command accepts voice payloads.
- Voice requests flow through the same intent/planner/transform/apply pipeline.
- STT and command error/status mappings remain machine-readable.
- Basic eval metrics are printed for regression tracking.
"""

import base64
import argparse
import io
import json
import os
import struct
import sys
import wave
from pathlib import Path
import re

# Keep startup deterministic and local for regression checks.
os.environ.setdefault("AUTO_BOOTSTRAP", "false")
# Command API is safe-off by default in release config; voice flow tests opt in explicitly.
os.environ.setdefault("ENABLE_COMMAND_API", "true")
os.environ.setdefault("COMMAND_INTENT_USE_LLM", "false")
os.environ.setdefault("COMMAND_TRANSFORM_USE_LLM", "false")
os.environ.setdefault("ENABLE_VOICE_INPUT", "true")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

import app.main as main_module
from app import crud
from app.db import SessionLocal
from app.main import app
from app.models import Document, DocumentVersion
from app.services.stt import STTTranscriptionError


DEFAULT_CASES_PATH = Path("tests/ml/voice_cases_seed.json")


def _make_wav_base64(duration_ms: int = 450, sample_rate: int = 16000) -> str:
    """
    Build a short valid WAV payload so voice request shape mirrors real clients.
    """
    frames = max(1, int(sample_rate * (duration_ms / 1000.0)))
    silence = struct.pack("<h", 0) * frames
    with io.BytesIO() as bio:
        with wave.open(bio, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(silence)
        return base64.b64encode(bio.getvalue()).decode("utf-8")


def _seed_structured_doc() -> tuple[str, int]:
    db = SessionLocal()
    try:
        doc = crud.create_document(db, user_id="voice-test-user", doc_type="GOI_LETTER", template_id=None)
        state = {
            "structured": {
                "sections": [
                    {
                        "id": "sec_body_001",
                        "type": "numbered_paragraphs",
                        "content": {
                            "items": [
                                {"id": "p1", "text": "1. First paragraph content."},
                                {"id": "p2", "text": "2. Second paragraph content."},
                                {"id": "p3", "text": "3. Third paragraph content."},
                            ]
                        },
                    }
                ]
            },
            "render": {"doc_type": "GOI_LETTER", "fields": {}, "blocks": {}, "lists": {}},
        }
        version = crud.add_version(
            db=db,
            doc_id=doc.id,
            doc_state=state,
            change_log={"action": "seed_voice"},
            docx_path="tmp",
        )
        return doc.id, version.id
    finally:
        db.close()


def _cleanup_doc(doc_id: str) -> None:
    db = SessionLocal()
    try:
        db.query(DocumentVersion).filter(DocumentVersion.document_id == doc_id).delete()
        db.query(Document).filter(Document.id == doc_id).delete()
        db.commit()
    finally:
        db.close()


def _change_log(version_id: int) -> dict:
    db = SessionLocal()
    try:
        row = db.get(DocumentVersion, version_id)
        return dict(row.change_log or {}) if row else {}
    finally:
        db.close()


def _current_structured(doc_id: str) -> dict:
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or not doc.current_version_id:
            return {}
        row = db.get(DocumentVersion, doc.current_version_id)
        return ((row.doc_state or {}).get("structured")) if row else {}
    finally:
        db.close()


def _para_text(structured: dict, para_id: str | None, section_id: str | None = None) -> str:
    if not para_id:
        return ""
    sections = structured.get("sections", []) or []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if section_id and sec.get("id") != section_id:
            continue
        items = ((sec.get("content") or {}).get("items")) or []
        for item in items:
            if isinstance(item, dict) and str(item.get("id") or "") == para_id:
                return str(item.get("text") or "")
    return ""


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_numbering(text: str) -> str:
    return re.sub(r"^\s*\d+\.\s+", "", _normalize_text(text))


def _direction_violation(expected_action: str | None, before_text: str, after_text: str) -> bool:
    before_core = _strip_numbering(before_text)
    after_core = _strip_numbering(after_text)
    if expected_action == "SHORTEN_CONTENT":
        return len(after_core) >= len(before_core)
    if expected_action == "EXPAND_CONTENT":
        return len(after_core) <= len(before_core)
    return False


def _expects_material_edit(expected_action: str | None) -> bool:
    # Keep this conservative: only actions that should always modify the text.
    return expected_action in {
        "SHORTEN_CONTENT",
        "EXPAND_CONTENT",
        "REPLACE_TEXT",
        "INSERT_TEXT",
        "DELETE_TEXT",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate voice command flow")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="Path to voice case JSON")
    parser.add_argument("--enforce-targets", action="store_true", help="Fail if KPI targets are not met")
    args = parser.parse_args()

    cases_path = Path(args.cases)
    cases = json.loads(cases_path.read_text(encoding="utf-8")).get("cases", [])
    if not cases:
        raise SystemExit("voice case set is empty")

    audio_payload = _make_wav_base64()
    current_case: dict | None = None
    original_transcribe = main_module.transcribe

    def fake_transcribe(audio_bytes: bytes, mime_type: str):
        """
        Deterministic test double so voice pipeline can be evaluated without external STT runtime.
        """
        assert current_case is not None
        simulated_error = current_case.get("simulate_stt_error")
        if simulated_error == "low_confidence":
            transcript = str(current_case.get("transcript") or "").strip() or "make paragraph 1 formal"
            return transcript, {
                "stt_model": "faster-whisper-test-double",
                "stt_device": "cpu",
                "stt_compute_type": "int8",
                "stt_language_detected": current_case.get("lang"),
                "stt_confidence": 0.30,
                "stt_latency_ms": 8,
            }
        if simulated_error == "transcription_failure":
            raise STTTranscriptionError("No speech content detected in audio")
        transcript = str(current_case.get("transcript") or "").strip()
        if not transcript:
            raise STTTranscriptionError("No speech content detected in audio")
        return transcript, {
            "stt_model": "faster-whisper-test-double",
            "stt_device": "cpu",
            "stt_compute_type": "int8",
            "stt_language_detected": current_case.get("lang"),
            "stt_confidence": 0.91,
            "stt_latency_ms": 8,
        }

    main_module.transcribe = fake_transcribe
    client = TestClient(app)

    doc_id, version = _seed_structured_doc()
    try:
        total = len(cases)
        transcription_success = 0
        intent_total = 0
        intent_success = 0
        clarify_count = 0
        apply_count = 0
        expected_match = 0
        wrong_edit_count = 0
        buckets = {
            "stt_failed": 0,
            "intent_failed": 0,
            "clarification_returned": 0,
            "patch_apply_failed": 0,
            "version_conflict": 0,
            "no_op_but_expected_edit": 0,
            "partial_fulfillment": 0,
        }

        for case in cases:
            current_case = case
            req_version = version
            if case.get("simulate_version_conflict"):
                req_version = max(0, version - 1)
            payload = {
                "version": req_version,
                "context": {
                    "current_section_id": "sec_body_001",
                    "selected_section_ids": [],
                    "cursor_position": 1,
                },
                "input": {
                    "type": "voice",
                    "audio_base64": audio_payload,
                    "mime_type": "audio/wav",
                },
            }
            expected = case.get("expected", {})
            expected_status = expected.get("status")
            expected_target = expected.get("target_para_id")
            before_structured = _current_structured(doc_id)
            before_text = _para_text(before_structured, expected_target, "sec_body_001")

            resp = client.post(f"/documents/{doc_id}/command", json=payload)
            body = resp.json()
            status = body.get("status")
            err_code = (body.get("error") or {}).get("code")

            is_stt_error = status == "error" and (body.get("error") or {}).get("code") == "could_not_understand_audio"
            if not is_stt_error:
                transcription_success += 1

            if status == "needs_clarification":
                clarify_count += 1
                buckets["clarification_returned"] += 1
            if status == "applied":
                apply_count += 1
                version = int(body["version"])
            if status == "error":
                if err_code == "could_not_understand_audio":
                    buckets["stt_failed"] += 1
                elif err_code == "intent_parse_error":
                    buckets["intent_failed"] += 1
                elif err_code == "patch_apply_failed":
                    buckets["patch_apply_failed"] += 1
                elif err_code == "version_conflict":
                    buckets["version_conflict"] += 1

            matched = False

            if expected_status in {"applied", "needs_clarification"}:
                intent_total += 1
                if expected_status == "applied" and status == "applied":
                    log = _change_log(int(body["version"]))
                    action_obj = log.get("action_object") or {}
                    target_obj = action_obj.get("target") or {}
                    target_section_id = target_obj.get("section_id")
                    target_para_id = target_obj.get("para_id")
                    after_structured = _current_structured(doc_id)
                    matched = (
                        action_obj.get("action") == expected.get("action")
                        and (target_para_id == expected_target)
                    )
                    if not matched:
                        wrong_edit_count += 1
                    compare_para_id = expected_target or target_para_id
                    if not before_text and compare_para_id:
                        before_text = _para_text(before_structured, compare_para_id, target_section_id)
                    after_text = _para_text(after_structured, compare_para_id, target_section_id)
                    if (
                        compare_para_id
                        and _expects_material_edit(expected.get("action"))
                        and _normalize_text(before_text) == _normalize_text(after_text)
                    ):
                        buckets["no_op_but_expected_edit"] += 1
                    elif _direction_violation(expected.get("action"), before_text, after_text):
                        buckets["partial_fulfillment"] += 1
                elif expected_status == "needs_clarification" and status == "needs_clarification":
                    matched = True
                if matched:
                    intent_success += 1
            elif expected_status == "error":
                matched = status == "error" and (body.get("error") or {}).get("code") == expected.get("error_code")

            if matched:
                expected_match += 1
            else:
                print(f"[voice-case-fail] id={case.get('id')} expected={expected} got={body}")

        transcription_success_rate = (transcription_success / total) * 100.0 if total else 0.0
        intent_success_rate = (intent_success / intent_total) * 100.0 if intent_total else 0.0
        clarify_rate = (clarify_count / total) * 100.0 if total else 0.0
        apply_rate = (apply_count / total) * 100.0 if total else 0.0
        expected_match_rate = (expected_match / total) * 100.0 if total else 0.0
        wrong_edit_rate = (wrong_edit_count / total) * 100.0 if total else 0.0

        print(f"Cases file: {cases_path}")
        print(f"Total cases: {total}")
        print(f"Transcription success rate: {transcription_success_rate:.2f}%")
        print(f"Intent success rate: {intent_success_rate:.2f}%")
        print(f"Clarify rate: {clarify_rate:.2f}%")
        print(f"End-to-end apply rate: {apply_rate:.2f}%")
        print(f"Expected outcome match rate: {expected_match_rate:.2f}%")
        print(f"Wrong edit rate: {wrong_edit_rate:.2f}%")
        print("Failure buckets:")
        print(f"  stt_failed: {buckets['stt_failed']}")
        print(f"  intent_failed: {buckets['intent_failed']}")
        print(f"  clarification_returned: {buckets['clarification_returned']}")
        print(f"  patch_apply_failed: {buckets['patch_apply_failed']}")
        print(f"  version_conflict: {buckets['version_conflict']}")
        print(f"  no_op_but_expected_edit: {buckets['no_op_but_expected_edit']}")
        print(f"  partial_fulfillment: {buckets['partial_fulfillment']}")

        if expected_match != total:
            raise SystemExit(1)
        if args.enforce_targets:
            if transcription_success_rate < 92.0:
                raise SystemExit(1)
            if clarify_rate > 15.0:
                raise SystemExit(1)
            if apply_rate < 75.0:
                raise SystemExit(1)
            if wrong_edit_rate > 0.0:
                raise SystemExit(1)
    finally:
        main_module.transcribe = original_transcribe
        _cleanup_doc(doc_id)


if __name__ == "__main__":
    main()

