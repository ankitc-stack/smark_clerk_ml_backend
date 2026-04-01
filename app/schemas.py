from pydantic import BaseModel, ConfigDict, Field, model_validator, field_serializer
from typing import Optional, Any, Dict, List, Literal, Union
from datetime import datetime, timezone, timedelta
from enum import Enum

# Indian Standard Time (UTC+5:30)
_IST = timezone(timedelta(hours=5, minutes=30))

def _to_ist_str(dt: datetime | None) -> str | None:
    """Convert a naive-UTC datetime to an IST ISO-8601 string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).isoformat()

class TemplateRegisterResponse(BaseModel):
    template_id: int
    name: str
    doc_type: str

class TemplateOut(BaseModel):
    id: str                          # str to accommodate both int DOCX ids and user-saved string ids
    name: str
    doc_type: str
    version: str
    zones_json: Dict[str, Any]
    is_user_saved: bool = False      # True for user-saved templates

class UploadResponse(BaseModel):
    document_id: str
    version_id: int
    detected_doc_type: str
    docx_url: str


class VersionHistoryItem(BaseModel):
    version_id: int
    thread_version: int
    created_at: datetime
    prompt_preview: str
    docx_url: str
    pdf_url: Optional[str] = None
    preview_url: Optional[str] = None  # inline PDF URL for iframe preview
    data: Optional[dict] = None        # sections + style_defaults for frontend preview

    @field_serializer("created_at")
    def serialize_created_at(self, dt: datetime) -> str | None:
        return _to_ist_str(dt)


class DocumentSummaryOut(BaseModel):
    document_id: str
    doc_type: str
    current_version_id: Optional[int] = None
    template_id: Optional[int] = None
    docengine_version: Optional[int] = None
    doc_version: Optional[int] = None  # 0-based per-document revision (generate=0, edit1=1, …)
    data: Optional[dict] = None        # current sections + style_defaults for preview
    preview_url: Optional[str] = None  # inline PDF URL for iframe preview


class DocumentListItem(BaseModel):
    document_id: str
    title: str
    doc_type: str
    created_at: datetime
    current_version_id: Optional[int] = None
    preview_url: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("created_at")
    def serialize_created_at(self, dt: datetime) -> str | None:
        return _to_ist_str(dt)


class CommandInputType(str, Enum):
    text = "text"
    voice = "voice"


class CommandScope(str, Enum):
    PARAGRAPH = "PARAGRAPH"
    SECTION = "SECTION"
    SELECTION = "SELECTION"
    DOCUMENT = "DOCUMENT"


class CommandAction(str, Enum):
    REWRITE_CONTENT = "REWRITE_CONTENT"
    EXPAND_CONTENT = "EXPAND_CONTENT"
    SHORTEN_CONTENT = "SHORTEN_CONTENT"
    CHANGE_TONE = "CHANGE_TONE"
    REPLACE_TEXT = "REPLACE_TEXT"
    INSERT_TEXT = "INSERT_TEXT"
    DELETE_TEXT = "DELETE_TEXT"
    INSERT_SECTION = "INSERT_SECTION"
    DELETE_SECTION = "DELETE_SECTION"
    MOVE_SECTION = "MOVE_SECTION"
    ADD_PARAGRAPH = "ADD_PARAGRAPH"
    REMOVE_PARAGRAPH = "REMOVE_PARAGRAPH"
    SET_FORMAT = "SET_FORMAT"   # bold / italic / underline / highlight / font / color / size
    UNDO = "UNDO"               # revert document to previous version
    FIX_GRAMMAR = "FIX_GRAMMAR" # fix grammar, spelling, and punctuation errors


class ToneValue(str, Enum):
    formal = "formal"
    concise = "concise"
    neutral = "neutral"


class CommandContext(BaseModel):
    # Required even when value is null because resolver logic depends on explicit key presence.
    current_section_id: str | None = Field(...)
    selected_section_ids: List[str] = Field(default_factory=list)
    cursor_position: int | None = Field(...)


class CommandInput(BaseModel):
    type: CommandInputType
    value: str | None = None
    audio_base64: str | None = None
    mime_type: str | None = None

    @model_validator(mode="after")
    def validate_variant(self):
        # Enforce mutually exclusive payload shapes so downstream command pipeline can be deterministic.
        if self.type == CommandInputType.text:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("input.value is required for text input")
        if self.type == CommandInputType.voice:
            if not isinstance(self.audio_base64, str) or not self.audio_base64.strip():
                raise ValueError("input.audio_base64 is required for voice input")
            if not isinstance(self.mime_type, str) or not self.mime_type.strip():
                raise ValueError("input.mime_type is required for voice input")
        return self


class CommandRequest(BaseModel):
    version: int = Field(ge=0)
    auto_retry: bool = False
    context: CommandContext
    input: CommandInput


# Task #2 compatibility alias:
# Keep an explicit text-only input model name so callers/tests can reference it
# while the endpoint still accepts the broader CommandInput variant.
class CommandInputText(BaseModel):
    type: Literal["text"]
    value: str


class ActionTarget(BaseModel):
    section_id: str | None = None
    para_id: str | None = None
    para_index: int | None = Field(default=None, ge=0)


class ActionParams(BaseModel):
    tone: ToneValue | None = None
    preserve_numbering: bool = True
    preserve_style: bool = True
    style_params: dict | None = None   # carries {"bold": True} etc. for SET_FORMAT actions


class ClarificationOption(BaseModel):
    label: str
    token: str


class ActionClarification(BaseModel):
    question: str
    options: List[ClarificationOption] = Field(default_factory=list)


class ActionObject(BaseModel):
    action: CommandAction
    scope: CommandScope
    target: ActionTarget
    params: ActionParams
    content: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool = False
    clarification: ActionClarification | None = None

    @model_validator(mode="after")
    def validate_constraints(self):
        # These constraints mirror the v1 contract so planner code can trust object invariants.
        # Clarification objects don't have fully-resolved params — skip param constraints.
        if self.needs_clarification:
            return self
        if self.action == CommandAction.CHANGE_TONE and self.params.tone is None:
            raise ValueError("CHANGE_TONE requires params.tone")
        if self.action in {CommandAction.REPLACE_TEXT, CommandAction.INSERT_TEXT} and self.content is None:
            raise ValueError("REPLACE_TEXT and INSERT_TEXT require non-null content")
        if self.action == CommandAction.DELETE_TEXT and self.content is not None:
            raise ValueError("DELETE_TEXT requires content to be null")
        if self.needs_clarification and self.clarification is None:
            raise ValueError("clarification is required when needs_clarification=true")
        return self


class CommandLatencyMeta(BaseModel):
    # Detailed breakdown keeps request timing auditable in logs and metrics.
    stt_ms: int = Field(default=0, ge=0)
    intent_ms: int = Field(default=0, ge=0)
    transform_ms: int = Field(default=0, ge=0)
    apply_ms: int = Field(default=0, ge=0)
    total_ms: int = Field(default=0, ge=0)


class CommandAppliedMeta(BaseModel):
    intent_confidence: float = Field(ge=0.0, le=1.0)
    intent_source: Literal["llm", "fallback_rule"] = "fallback_rule"
    repair_applied: bool = False
    prompt_version: str = "intent_extraction_v1"
    input_source: Literal["text", "voice"] = "text"
    transcript: str | None = None
    stt_model: str | None = None
    stt_device: str | None = None
    stt_compute_type: str | None = None
    stt_language_detected: str | None = None
    stt_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    stt_latency_ms: int | None = Field(default=None, ge=0)
    auto_retried: bool = False
    # v1 safety rail: backend performs at most one auto-retry for pure version drift.
    retry_count: int = Field(default=0, ge=0, le=1)
    base_version: int | None = Field(default=None, ge=0)
    # request_id/trace_id let ops correlate one API response to one structured log line.
    request_id: str | None = None
    trace_id: str | None = None
    # Transform observability helps separate intent failures from rewrite failures in debugging.
    transform_source: Literal["llm", "stub", "deterministic"] | None = None
    transform_prompt_version: str | None = None
    transform_repair_applied: bool | None = None
    latency_ms: CommandLatencyMeta


class CommandClarification(BaseModel):
    question: str
    options: List[ClarificationOption] = Field(default_factory=list)
    clarification_token: str
    # Stable machine-readable reason for analytics/frontend handling.
    reason_code: str | None = None


class CommandErrorBody(BaseModel):
    code: Literal[
        "version_conflict",
        "invalid_command_payload",
        "intent_parse_error",
        "patch_apply_failed",
        "could_not_understand_audio",
        "unsupported_action",
        "llm_failure",
    ]
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class CommandAppliedResponse(BaseModel):
    status: Literal["applied"]
    version: int
    updates: List[Dict[str, Any]] = Field(default_factory=list)
    meta: CommandAppliedMeta


class CommandNeedsClarificationResponse(BaseModel):
    status: Literal["needs_clarification"]
    version: int
    clarification: CommandClarification
    meta: CommandAppliedMeta | None = None


class CommandErrorResponse(BaseModel):
    status: Literal["error"]
    version: int
    error: CommandErrorBody


# Task #2 compatibility aliases:
# These names mirror the requested stub deliverable while reusing the same payload contract.
class CommandResponseApplied(CommandAppliedResponse):
    pass


class CommandResponseClarification(CommandNeedsClarificationResponse):
    pass


# ------------------------------------------------------------------
# Doc-Engine microservice integration schemas
# ------------------------------------------------------------------

class GenerateDocRequest(BaseModel):
    """Request body for POST /documents/generate — create a blueprint-driven document.

    template_id accepts either:
      - an integer (ML pipeline templates.id PK, e.g. 35) — used by the production frontend
      - a doc-engine string ID (e.g. "tmpl_goi_001") — used by the test UI
    The endpoint resolves integers to doc-engine IDs automatically.
    """
    template_id: Union[int, str]
    user_id: str = "local-user"
    inputs: Dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""          # natural language prompt → LLM fills sections
    input_type: str = "text"  # "text" | "voice"
    audio_base64: str | None = None   # base64-encoded audio when input_type="voice"
    mime_type: str | None = None      # e.g. "audio/wav", "audio/webm"


class GenerateDocResponse(BaseModel):
    """Response from POST /documents/generate."""
    document_id: str           # ML pipeline document id (UUID)
    docengine_doc_id: str      # Doc-engine document id (UUID)
    version: int               # ML pipeline current_version_id — send this to /command
    docengine_version: int     # Doc-engine version — send this to PATCH /sections/{id}
    doc_type: str
    blueprint_id: str
    filled: Dict[str, Any] = Field(default_factory=dict)
    # filled: named slot fields matching the rendered DOCX (army_no, rank, subject, …).
    # Populated for slot-based letters (leave_certificate, goi_letter, do_letter, etc.).
    # Empty dict for generic/uploaded documents.
    data: Dict[str, Any]       # Full DocumentData from doc-engine (sections, style_defaults, …)
    render_warning: str | None = None   # Set if DOCX render was skipped or failed


# Keep legacy aliases for internal compatibility during transition
BlueprintDocRequest = GenerateDocRequest
BlueprintDocResponse = GenerateDocResponse


class SectionPatchRequest(BaseModel):
    """Request body for PATCH /documents/{doc_id}/sections/{section_id}."""
    version: int = Field(ge=0)
    content: Dict[str, Any]    # { "richtext": { "format": "lexical", "state": {...} } }
    alignment: Optional[str] = None  # "left" | "center" | "right" — per-section override


class SectionPatchResponse(BaseModel):
    """Response from PATCH /documents/{doc_id}/sections/{section_id}."""
    ok: bool = True
    version: int               # New doc-engine version after the patch


class RevertDocumentRequest(BaseModel):
    target_version_id: int | None = None  # None = revert to previous version
    version: int = Field(..., ge=0)        # optimistic concurrency


class RevertDocumentResponse(BaseModel):
    document_id: str
    version: int
    reverted_from_version_id: int
    reverted_to_version_id: int


# ------------------------------------------------------------------
# Phase 7 — Letter Template Store
# ------------------------------------------------------------------

class SaveAsTemplateRequest(BaseModel):
    letter_type: str | None = None  # "service_letter", "goi_letter", etc. — if omitted, inferred from doc.doc_type
    display_name: str = ""          # shown in dropdown; defaults to letter_type title


class SaveAsTemplateResponse(BaseModel):
    template_id: str
    letter_type: str
    display_name: str
    section_count: int


class TemplateStoreItem(BaseModel):
    template_id: str
    letter_type: str
    display_name: str
    saved_at: str
    section_count: int


# ------------------------------------------------------------------
# Auth schemas
# ------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    full_name: str | None = None


class MeResponse(BaseModel):
    user_id: str
    email: str
    full_name: str | None = None
    is_active: bool


class FeedbackRequest(BaseModel):
    version_id: int
    rating: str              # "up" or "down"
    field: str = "overall"   # "overall"|"subject"|"paragraph_1"|"paragraph_2"|"salutation"|"signee"
    correction: str = ""     # corrected text (optional)


class FeedbackResponse(BaseModel):
    feedback_id: str
    saved: bool
