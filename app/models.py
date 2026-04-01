from __future__ import annotations
from datetime import datetime
import uuid
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, JSON, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from app.db import Base
from app.config import settings

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    doc_type: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32), default="v1")
    docx_path: Mapped[str] = mapped_column(String(512))
    zones_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class RuleChunk(Base):
    __tablename__ = "rule_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(128), default="rulebook_pdf")
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(settings.EMBEDDING_DIM))

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    doc_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id"), nullable=True)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Doc-Engine integration: UUID of the document managed by the doc-engine microservice.
    # Set only for blueprint-driven documents; null for legacy skeleton documents.
    docengine_doc_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    doc_state: Mapped[dict] = mapped_column(JSON)
    change_log: Mapped[dict] = mapped_column(JSON, default=dict)
    docx_path: Mapped[str] = mapped_column(String(512))
    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document = relationship("Document", backref="versions")


# ── Logging / observability tables ────────────────────────────────────────────

class RequestLog(Base):
    __tablename__ = "request_logs"
    id          = mapped_column(String(40), primary_key=True, default=lambda: uuid.uuid4().hex)
    endpoint    = mapped_column(String(128), nullable=False, index=True)
    user_id     = mapped_column(String(80),  nullable=True,  index=True)
    doc_id      = mapped_column(String(40),  nullable=True,  index=True)
    doc_type    = mapped_column(String(64),  nullable=True)
    prompt      = mapped_column(Text,        nullable=True)
    status      = mapped_column(String(32),  nullable=True)   # applied|error|needs_clarification
    status_code = mapped_column(Integer,     nullable=True)
    latency_ms  = mapped_column(Integer,     nullable=True)
    extra       = mapped_column(JSON,        nullable=True)
    created_at  = mapped_column(DateTime,    default=datetime.utcnow, index=True)


class SttLog(Base):
    __tablename__ = "stt_logs"
    id         = mapped_column(String(40), primary_key=True, default=lambda: uuid.uuid4().hex)
    request_id = mapped_column(String(40), nullable=True, index=True)
    user_id    = mapped_column(String(80), nullable=True, index=True)
    doc_id     = mapped_column(String(40), nullable=True)
    source     = mapped_column(String(32), nullable=False)   # command_api|stt_endpoint
    transcript = mapped_column(Text,       nullable=True)
    mime_type  = mapped_column(String(64), nullable=True)
    stt_model  = mapped_column(String(64), nullable=True)
    confidence = mapped_column(Float,      nullable=True)
    latency_ms = mapped_column(Integer,    nullable=True)
    succeeded  = mapped_column(Boolean,    nullable=False, default=True)
    created_at = mapped_column(DateTime,   default=datetime.utcnow, index=True)


class WakeWordLog(Base):
    __tablename__ = "wake_word_logs"
    id         = mapped_column(String(40), primary_key=True, default=lambda: uuid.uuid4().hex)
    event      = mapped_column(String(16), nullable=False, index=True)  # activate|deactivate
    method     = mapped_column(String(16), nullable=True)               # whisper|oww
    score      = mapped_column(Float,      nullable=True)
    transcript = mapped_column(Text,       nullable=True)
    created_at = mapped_column(DateTime,   default=datetime.utcnow, index=True)


class ErrorLog(Base):
    __tablename__ = "error_logs"
    id         = mapped_column(String(40), primary_key=True, default=lambda: uuid.uuid4().hex)
    endpoint   = mapped_column(String(128), nullable=True, index=True)
    error_type = mapped_column(String(64),  nullable=True, index=True)
    error_code = mapped_column(String(64),  nullable=True)
    message    = mapped_column(Text,        nullable=True)
    user_id    = mapped_column(String(80),  nullable=True)
    doc_id     = mapped_column(String(40),  nullable=True)
    request_id = mapped_column(String(40),  nullable=True)
    created_at = mapped_column(DateTime,    default=datetime.utcnow, index=True)
