from __future__ import annotations

"""
End-to-end smoke checks for Task #4 content transform flow.

Usage:
- Stub mode (deterministic): python scripts/test_transform_flow.py --transform-mode stub
- LLM mode (if Ollama available): python scripts/test_transform_flow.py --transform-mode llm
"""

import argparse
import os
import sys
from pathlib import Path

# Parse mode early so env flags are set before importing app modules/settings.
_BOOTSTRAP_PARSER = argparse.ArgumentParser(add_help=False)
_BOOTSTRAP_PARSER.add_argument("--transform-mode", choices=["stub", "llm"], default="stub")
_BOOTSTRAP_ARGS, _ = _BOOTSTRAP_PARSER.parse_known_args()

os.environ.setdefault("AUTO_BOOTSTRAP", "false")
# Command API is safe-off by default; transform flow tests need it enabled.
os.environ.setdefault("ENABLE_COMMAND_API", "true")
os.environ.setdefault("COMMAND_INTENT_USE_LLM", "false")
os.environ["COMMAND_TRANSFORM_USE_LLM"] = "true" if _BOOTSTRAP_ARGS.transform_mode == "llm" else "false"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from app import crud
from app.db import SessionLocal
from app.main import app
from app.models import Document, DocumentVersion
from app.services.content_transform import postprocess_text, prepare_input_text


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


def _para_text(structured: dict, section_id: str, para_id: str) -> str:
    for sec in structured.get("sections", []) or []:
        if not isinstance(sec, dict) or sec.get("id") != section_id:
            continue
        items = ((sec.get("content") or {}).get("items")) or []
        for item in items:
            if isinstance(item, dict) and item.get("id") == para_id:
                return str(item.get("text") or "")
    return ""


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
                                {"id": "p1", "text": "1. The unit has initiated the review process."},
                                {
                                    "id": "p2",
                                    "text": (
                                        "2. Personnel are instructed to report by 1700 hours with all relevant "
                                        "supporting documents for verification and onward processing."
                                    ),
                                },
                                {"id": "p3", "text": "3. Further directions will be issued separately."},
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
            change_log={"action": "seed"},
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


def test_numbering_preservation_unit() -> None:
    source = "2. Personnel are instructed to report by 1700 hours."

    prepared, prep_meta = prepare_input_text(source, preserve_numbering=True)
    assert not prepared.startswith("2. ")
    assert prep_meta["numbering_prefix"] == "2. "
    restored = postprocess_text("Personnel are to report by 1700 hours.", prep_meta)
    assert restored.startswith("2. ")

    prepared_no, prep_meta_no = prepare_input_text(source, preserve_numbering=False)
    assert prepared_no.startswith("2. ")
    restored_no = postprocess_text("Personnel are to report by 1700 hours.", prep_meta_no)
    assert not restored_no.startswith("2. ")


def test_command_transform_flow(client: TestClient) -> None:
    doc_id, version = _seed_structured_doc()
    commands = [
        "Make paragraph 2 formal",
        "Shorten paragraph 2",
        "Expand paragraph 2",
        # Keep rewrite prompt unambiguous so this gate only validates transform plumbing.
        "Rewrite paragraph 2",
    ]
    try:
        for prompt in commands:
            before_structured = _current_structured(doc_id)
            before_signature = _structure_signature(before_structured)
            before_para = _para_text(before_structured, "sec_body_001", "p2")

            payload = {
                "version": version,
                "context": {
                    "current_section_id": "sec_body_001",
                    "selected_section_ids": [],
                    "cursor_position": 15,
                },
                "input": {
                    "type": "text",
                    "value": prompt,
                },
            }
            resp = client.post(f"/documents/{doc_id}/command", json=payload)
            body = resp.json()
            assert resp.status_code == 200, body
            assert body["status"] == "applied", body
            assert len(body["updates"]) == 1
            assert body["updates"][0]["op"] == "replace_para_text"
            assert body["updates"][0]["target"]["para_id"] == "p2"

            # Intent + transform meta must be present for debugging/evaluation.
            assert body["meta"]["intent_source"] in {"llm", "fallback_rule"}
            assert body["meta"]["prompt_version"]
            assert body["meta"]["transform_source"] in {"llm", "stub"}
            assert body["meta"]["transform_prompt_version"]
            assert body["meta"]["transform_repair_applied"] in {True, False}

            version = body["version"]
            after_structured = _current_structured(doc_id)
            after_signature = _structure_signature(after_structured)
            after_para = _para_text(after_structured, "sec_body_001", "p2")

            # Structure should not drift for replace-only transform flow.
            assert before_signature == after_signature
            assert after_para.startswith("2. ")

            if prompt.lower().startswith("shorten"):
                assert len(after_para) <= len(before_para)
            if prompt.lower().startswith("expand"):
                assert len(after_para) >= len(before_para)
    finally:
        _cleanup_doc(doc_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Task #4 transform flow checks")
    parser.add_argument("--transform-mode", choices=["stub", "llm"], default=_BOOTSTRAP_ARGS.transform_mode)
    args = parser.parse_args()

    test_numbering_preservation_unit()
    client = TestClient(app)
    test_command_transform_flow(client)
    print(f"transform flow checks: PASS (mode={args.transform_mode})")


if __name__ == "__main__":
    main()

