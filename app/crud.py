from __future__ import annotations
import os, uuid
from sqlalchemy.orm import Session
from app.models import Template, Document, DocumentVersion
from app.config import settings

def new_doc_id() -> str:
    return uuid.uuid4().hex

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def storage_path(doc_id: str, version_id: int, ext: str) -> str:
    return os.path.join(settings.STORAGE_DIR, doc_id, f"{doc_id}_v{version_id}.{ext}")

def create_document(db: Session, user_id: str, doc_type: str, template_id: int | None):
    doc = Document(id=new_doc_id(), user_id=user_id, doc_type=doc_type, template_id=template_id)
    db.add(doc)
    db.commit()
    return doc

def add_version(db: Session, doc_id: str, doc_state: dict, change_log: dict, docx_path: str):
    v = DocumentVersion(document_id=doc_id, doc_state=doc_state, change_log=change_log, docx_path=docx_path)
    db.add(v)
    db.flush()
    doc = db.get(Document, doc_id)
    doc.current_version_id = v.id
    db.commit()
    return v

def list_templates(db: Session, doc_type: str | None = None):
    q = db.query(Template)
    if doc_type:
        q = q.filter(Template.doc_type == doc_type)
    return q.order_by(Template.id.desc()).all()
