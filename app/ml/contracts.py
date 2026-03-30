"""
app/ml/contracts.py

Purpose:
- Define the "contracts" (schemas) for all ML outputs.
- This ensures the rest of the system (backend + frontend) can depend on stable structures.
- We keep contracts independent of any specific backend config or database model,
- so you can merge this module later without refactors.

How it is used:
- After you parse JSON from the LLM, you validate it against these contracts.
- If validation fails, you either:
  1) run a repair prompt (Step 1), or
  2) return a safe fallback (UNKNOWN / empty ops / ask for clarification).

Note:
- We use Pydantic because it's already common in FastAPI projects and provides
  strong runtime validation.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, ConfigDict


# -----------------------------
# Shared enums / constants
# -----------------------------

class DocType(str, Enum):
    """
    Allowed document types for Month-1 scope.
    Keep these stable across the project.
    """
    DO_LETTER = "DO_LETTER"
    GOI_LETTER = "GOI_LETTER"
    LEAVE_CERTIFICATE = "LEAVE_CERTIFICATE"
    MOVEMENT_ORDER = "MOVEMENT_ORDER"
    UNKNOWN = "UNKNOWN"


class MlKind(str, Enum):
    """
    High-level ML response kind.
    Helpful for routing:
    - classification
    - section-text generation
    - edit-to-ops
    """
    CLASSIFY = "CLASSIFY"
    SECTION_TEXT = "SECTION_TEXT"
    PATCH_OPS = "PATCH_OPS"


# -----------------------------
# Contract: DocType classification
# -----------------------------

class DocTypeResult(BaseModel):
    """
    Output contract for doctype classifier.
    """
    model_config = ConfigDict(extra="ignore")

    doc_type: DocType
    confidence: float = Field(ge=0.0, le=1.0)

    # Optional: human-readable short reasons (debugging + UI explainability)
    reasons: List[str] = Field(default_factory=list)

    # Optional: extracted entities (keep this lightweight; don't chase perfection)
    entities: Dict[str, Any] = Field(default_factory=dict)


# -----------------------------
# Contract: Text rewrite / generation for a section
# -----------------------------

class SectionTextResult(BaseModel):
    """
    Output contract for text generation or rewrite for a single section.
    The LLM must NOT change layout; only text.
    """
    model_config = ConfigDict(extra="ignore")

    section_id: str
    text: str


class ParagraphListResult(BaseModel):
    """
    Output contract for slots that generate multiple paragraphs,
    e.g., GOI numbered paragraphs or Movement Order paragraphs.
    """
    model_config = ConfigDict(extra="ignore")

    paras: List[str] = Field(default_factory=list)


class LeaveFieldsResult(BaseModel):
    """
    Output contract for LEAVE_CERTIFICATE extraction.
    This is structured fields (NOT free prose).
    """
    model_config = ConfigDict(extra="ignore")

    fields: Dict[str, Any] = Field(default_factory=dict)


# -----------------------------
# Contract: PatchOps (edit prompt -> deterministic operations)
# -----------------------------

PatchOpName = Literal[
    "update_section_text",
    "set_field",
    "insert_section",
    "delete_section",
    "move_section",
]

class PatchOp(BaseModel):
    """
    A single deterministic operation that the backend engine can apply to DocState.

    Important:
    - We keep the set small for Month 1.
    - The backend engine must implement these ops exactly.
    """
    model_config = ConfigDict(extra="ignore")

    op: PatchOpName

    # Common fields used by ops
    section_id: Optional[str] = None
    after_section_id: Optional[str] = None

    # For update_section_text
    text: Optional[str] = None

    # For set_field: path like "receiver.name" or "meta.date"
    path: Optional[str] = None
    value: Optional[Any] = None

    # For insert_section: full section payload (type + content)
    section: Optional[Dict[str, Any]] = None


class PatchOpsResult(BaseModel):
    """
    Output contract for edit-to-ops generator.
    """
    model_config = ConfigDict(extra="ignore")

    ops: List[PatchOp] = Field(default_factory=list)

    # Optional message if model couldn't decide / needs clarification
    message: Optional[str] = None


# -----------------------------
# Contract: SectionTexts
# The single handoff type between the LLM layer and the Document Engine.
# -----------------------------

class SectionTexts(BaseModel):
    """
    The ONLY output contract the LLM layer produces for document creation.

    Design:
        - LLM generates plain text values mapped to well-known field names.
        - These field names are STABLE across all document types.
        - The Document Engine (or sandbox fill_adapter) maps them to section IDs.
        - The backend wraps each value in Lexical JSON via lexical_wrapper.py.

    Field name conventions:
        subject       → maps to sec_subject_* section
        paras         → maps to sec_body_* (numbered_paragraphs section)
        salutation    → maps to sec_salutation_* (DO letter only)
        signee_name   → maps to sec_signee_* (all types)
        fields        → arbitrary key-value pairs for structured types
                        (e.g. LEAVE_CERTIFICATE: rank, name, dates)

    Integration note for backend developer:
        When Document Engine is ready, your integration point is:

            section_texts = SectionTexts(subject=..., paras=[...])
            for section_id in blueprint.fillable_sections(doc_type):
                text = section_texts.get_for_section(section_id)
                if text:
                    lexical = text_to_lexical_node(text, doc.style_defaults)
                    engine.update_section(section_id, lexical)

        The sandbox currently handles this mapping in fill_adapter.py.
        That file is TEMPORARY and will be deleted after integration.
    """
    model_config = ConfigDict(extra="ignore")

    # Core text fields (present in most document types)
    subject: str = ""
    paras: List[str] = Field(default_factory=list)

    # DO letter specific
    salutation: str = ""

    # Structured field types (LEAVE_CERTIFICATE, etc.)
    fields: Dict[str, Any] = Field(default_factory=dict)

    # Optional: distribution lines (MOVEMENT_ORDER)
    distribution_lines: List[str] = Field(default_factory=list)

    def get_for_section_type(self, section_type: str) -> Any:
        """
        Convenience lookup by section type string (matches skeleton section.type values).
        Returns the appropriate text value or list for the given section type.
        Returns None if this section type is not LLM-fillable.
        """
        _map = {
            "subject": self.subject,
            "salutation": self.salutation,
            "numbered_paragraphs": self.paras,
            "distribution": self.distribution_lines,
        }
        return _map.get(section_type)  # returns None for locked/non-fillable types
