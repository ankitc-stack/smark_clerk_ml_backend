from __future__ import annotations

"""
Smoke checks for /documents/{document_id}/command contract.

Why this script exists:
- Verifies the v1 interface behavior without needing frontend integration.
- Validates deterministic status handling: applied / needs_clarification / error.
"""

import base64
import io
import os
import struct
import sys
import wave
from pathlib import Path

# Keep startup lightweight when importing FastAPI app during tests.
os.environ.setdefault("AUTO_BOOTSTRAP", "false")
# Command API/voice are disabled by default in release-safe config, so tests opt in explicitly.
os.environ.setdefault("ENABLE_COMMAND_API", "true")
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
from app.schemas import CommandContext
from app.services.command_contract import resolve_action_object_from_request


def _structure_signature(structured: dict) -> list[tuple[str, str, list[str], list[list[str]]]]:
    signature: list[tuple[str, str, list[str], list[list[str]]]] = []
    for section in structured.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        items = ((section.get("content") or {}).get("items")) or []
        item_ids = [str(item.get("id") or "") for item in items if isinstance(item, dict)]
        item_keys = [sorted(list(item.keys())) for item in items if isinstance(item, dict)]
        signature.append((str(section.get("id") or ""), str(section.get("type") or ""), item_ids, item_keys))
    return signature


def _current_structured(doc_id: str) -> dict:
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or not doc.current_version_id:
            return {}
        version = db.get(DocumentVersion, doc.current_version_id)
        return ((version.doc_state or {}).get("structured")) if version else {}
    finally:
        db.close()


def _para_text(structured: dict, section_id: str, para_id: str) -> str:
    for sec in structured.get("sections", []) or []:
        if not isinstance(sec, dict) or sec.get("id") != section_id:
            continue
        items = ((sec.get("content") or {}).get("items")) or []
        for item in items:
            if isinstance(item, dict) and item.get("id") == para_id:
                return str(item.get("text") or "")
    return ""


def _para_ids(structured: dict, section_id: str) -> list[str]:
    for sec in structured.get("sections", []) or []:
        if not isinstance(sec, dict) or sec.get("id") != section_id:
            continue
        items = ((sec.get("content") or {}).get("items")) or []
        return [str(item.get("id") or "") for item in items if isinstance(item, dict)]
    return []


def _section_ids(structured: dict) -> list[str]:
    return [
        str(sec.get("id") or "")
        for sec in (structured.get("sections", []) or [])
        if isinstance(sec, dict)
    ]


def _dummy_wav_base64(duration_ms: int = 450, sample_rate: int = 16000) -> str:
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
        doc = crud.create_document(db, user_id="test-user", doc_type="GOI_LETTER", template_id=None)
        state = {
            "structured": {
                "sections": [
                    {
                        "id": "sec_body_001",
                        "type": "numbered_paragraphs",
                        "content": {
                            "items": [
                                {"id": "p1", "text": "1. Paragraph one text."},
                                {"id": "p2", "text": "2. Paragraph two text for transform tests."},
                            ]
                        },
                    },
                    {
                        "id": "sec_body_002",
                        "type": "numbered_paragraphs",
                        "content": {
                            "items": [
                                {"id": "p1", "text": "1. Second section paragraph."},
                            ]
                        },
                    }
                ]
            },
            "render": {"doc_type": "GOI_LETTER", "fields": {}, "blocks": {}, "lists": {}},
        }
        v = crud.add_version(
            db=db,
            doc_id=doc.id,
            doc_state=state,
            change_log={"action": "seed"},
            docx_path="tmp",
        )
        return doc.id, v.id
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


def test_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        before_structured = _current_structured(doc_id)
        before_signature = _structure_signature(before_structured)
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Make paragraph 2 formal",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["version"] > version
        assert body["updates"][0]["op"] == "replace_para_text"
        assert body["updates"][0]["target"]["para_id"] == "p2"
        assert body["meta"]["intent_source"] in {"llm", "fallback_rule"}
        assert body["meta"]["transform_source"] in {"llm", "stub"}
        assert body["meta"]["transform_prompt_version"]
        assert body["meta"]["retry_count"] == 0

        after_structured = _current_structured(doc_id)
        after_signature = _structure_signature(after_structured)
        assert before_signature == after_signature
        assert _para_text(after_structured, "sec_body_001", "p2").startswith("2. ")
    finally:
        _cleanup_doc(doc_id)


def test_shorten_typo_parser():
    doc_id, version = _seed_structured_doc()
    try:
        structured = _current_structured(doc_id)
        context = CommandContext.model_validate(
            {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            }
        )
        obj = resolve_action_object_from_request("shroten paragrph 2", context, structured)
        assert obj.action.value == "SHORTEN_CONTENT"
        assert obj.target.section_id == "sec_body_001"
        assert obj.target.para_id == "p2"
        assert obj.needs_clarification is False
    finally:
        _cleanup_doc(doc_id)


def test_needs_clarification(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Make paragraph formal",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "needs_clarification"
        assert body["version"] == version
        assert body["clarification"]["question"]
        assert body["clarification"]["options"]
        assert body["clarification"]["reason_code"] == "intent_needs_clarification"
    finally:
        _cleanup_doc(doc_id)


def test_version_conflict(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": max(0, version - 1),
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 0,
            },
            "input": {
                "type": "text",
                "value": "Make paragraph 2 formal",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 409
        assert body["status"] == "error"
        assert body["error"]["code"] == "version_conflict"
        assert body["error"]["details"]["server_version"] == version
        assert "latest_preview" in body["error"]["details"]
        assert body["error"]["details"]["hint"] == "Document changed, please retry"
    finally:
        _cleanup_doc(doc_id)


def test_version_conflict_auto_retry(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": max(0, version - 1),
            "auto_retry": True,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 0,
            },
            "input": {
                "type": "text",
                "value": "Make paragraph 2 formal",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["meta"]["auto_retried"] is True
        assert body["meta"]["retry_count"] == 1
        assert body["meta"]["base_version"] == max(0, version - 1)
    finally:
        _cleanup_doc(doc_id)


def test_could_not_understand_audio(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 0,
            },
            "input": {
                "type": "voice",
                "audio_base64": "not-valid-base64",
                "mime_type": "audio/wav",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 400
        assert body["status"] == "error"
        assert body["error"]["code"] == "could_not_understand_audio"
    finally:
        _cleanup_doc(doc_id)


def test_voice_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    original_transcribe = main_module.transcribe
    try:
        def fake_transcribe(audio_bytes: bytes, mime_type: str):
            return "Make paragraph 2 formal", {
                "stt_model": "faster-whisper-test-double",
                "stt_device": "cpu",
                "stt_compute_type": "int8",
                "stt_language_detected": "en",
                "stt_confidence": 0.9,
                "stt_latency_ms": 8,
            }

        main_module.transcribe = fake_transcribe
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 0,
            },
            "input": {
                "type": "voice",
                "audio_base64": _dummy_wav_base64(),
                "mime_type": "audio/wav",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["meta"]["input_source"] == "voice"
        assert body["meta"]["transcript"] == "Make paragraph 2 formal"
        assert body["meta"]["stt_model"] == "faster-whisper-test-double"
        assert body["updates"][0]["op"] == "replace_para_text"
    finally:
        main_module.transcribe = original_transcribe
        _cleanup_doc(doc_id)


def test_intent_parse_error(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 0,
            },
            "input": {
                "type": "text",
                "value": "insert",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 500
        assert body["status"] == "error"
        assert body["error"]["code"] == "intent_parse_error"
    finally:
        _cleanup_doc(doc_id)


def test_unsupported_action_reason_code(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": 1,
            },
            "input": {
                "type": "text",
                "value": "Insert this line in paragraph 1",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "needs_clarification"
        assert body["clarification"]["reason_code"] == "unsupported_action"
    finally:
        _cleanup_doc(doc_id)


def test_add_paragraph_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Add paragraph after paragraph 1 with Additional compliance sentence.",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "insert_para_after"
        assert body["updates"][0]["target"]["after_para_id"] == "p1"

        after_structured = _current_structured(doc_id)
        para_ids = _para_ids(after_structured, "sec_body_001")
        assert len(para_ids) == 3
        assert para_ids[1] == "p3"
        assert _para_text(after_structured, "sec_body_001", "p3") == "Additional compliance sentence."
    finally:
        _cleanup_doc(doc_id)


def test_add_paragraph_typo_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Add another paragrph",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "insert_para_after"
        assert body["updates"][0]["target"]["after_para_id"] == "p2"

        after_structured = _current_structured(doc_id)
        para_ids = _para_ids(after_structured, "sec_body_001")
        assert para_ids == ["p1", "p2", "p3"]
    finally:
        _cleanup_doc(doc_id)


def test_remove_paragraph_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Remove paragrph 2",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "delete_para"
        assert body["updates"][0]["target"]["para_id"] == "p2"

        after_structured = _current_structured(doc_id)
        assert _para_ids(after_structured, "sec_body_001") == ["p1"]
    finally:
        _cleanup_doc(doc_id)


def test_insert_section_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Add section after section sec_body_001",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "insert_section_after"
        assert body["updates"][0]["target"]["after_section_id"] == "sec_body_001"

        after_structured = _current_structured(doc_id)
        section_ids = _section_ids(after_structured)
        assert len(section_ids) == 3
        assert section_ids[1].startswith("sec_cmd_")
    finally:
        _cleanup_doc(doc_id)


def test_delete_section_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Delete seciton body 2",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "delete_section"
        assert body["updates"][0]["target"]["section_id"] == "sec_body_002"

        after_structured = _current_structured(doc_id)
        assert _section_ids(after_structured) == ["sec_body_001"]
    finally:
        _cleanup_doc(doc_id)


def test_move_section_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Move section sec_body_002 before section sec_body_001",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "move_section"
        assert body["updates"][0]["target"]["position"] == "before"

        after_structured = _current_structured(doc_id)
        assert _section_ids(after_structured) == ["sec_body_002", "sec_body_001"]
    finally:
        _cleanup_doc(doc_id)


def test_move_section_alias_applied(client: TestClient):
    doc_id, version = _seed_structured_doc()
    try:
        payload = {
            "version": version,
            "context": {
                "current_section_id": "sec_body_001",
                "selected_section_ids": [],
                "cursor_position": None,
            },
            "input": {
                "type": "text",
                "value": "Move seciton body 1 aftr body 2",
            },
        }
        resp = client.post(f"/documents/{doc_id}/command", json=payload)
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "applied"
        assert body["updates"][0]["op"] == "move_section"
        assert body["updates"][0]["target"]["section_id"] == "sec_body_001"
        assert body["updates"][0]["target"]["anchor_section_id"] == "sec_body_002"
        assert body["updates"][0]["target"]["position"] == "after"

        after_structured = _current_structured(doc_id)
        assert _section_ids(after_structured) == ["sec_body_002", "sec_body_001"]
    finally:
        _cleanup_doc(doc_id)


def main():
    client = TestClient(app)
    test_applied(client)
    test_shorten_typo_parser()
    test_needs_clarification(client)
    test_version_conflict(client)
    test_version_conflict_auto_retry(client)
    test_voice_applied(client)
    test_could_not_understand_audio(client)
    test_intent_parse_error(client)
    test_unsupported_action_reason_code(client)
    test_add_paragraph_applied(client)
    test_add_paragraph_typo_applied(client)
    test_remove_paragraph_applied(client)
    test_insert_section_applied(client)
    test_delete_section_applied(client)
    test_move_section_applied(client)
    test_move_section_alias_applied(client)
    print("command contract checks: PASS")


if __name__ == "__main__":
    main()

