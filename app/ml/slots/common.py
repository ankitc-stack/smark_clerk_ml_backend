"""
app/ml/slots/common.py

Purpose:
- Shared helper to run a "slot" generation call with:
  1) doctype-specific rules pulled from RAG (rulebook chunks)
  2) strict JSON output enforced
  3) one repair attempt using Step 1 pipeline

Why:
- Keeps slot prompts consistent
- Central place to change prompt policy later
"""

from __future__ import annotations
from typing import Any, Dict
from sqlalchemy.orm import Session

from app.ml.json_repair import call_and_parse_json
from app.ml.rag_context import build_rules_context
from app.services.prompt_library import load_system_prompt

# Fallback used ONLY if the prompt file is missing (should not happen in production).
# WHAT: Fallback string used ONLY if the prompt file is missing.
# WHY:  Prevents a hard crash at startup if a dev hasn't copied the prompt library.
#       Should never fire in production — monitored via the load_system_prompt()
#       None-return path (which logs a warning).

_FALLBACK_BASE_SYSTEM = (
    "You draft content for official documents.\n"
    "Return STRICT JSON only. No markdown. No extra text.\n"
    "Do NOT provide layout/margins/spacing instructions.\n"
    "Do NOT invent names, dates, phone numbers unless provided.\n"
)


def _base_system_prompt(doctype: str) -> str:
    """
    WHAT: Load the shared generation system prompt from the prompt library file.
          Appends the document type so the model knows the document context.

    WHY FILE-BASED instead of an inline string:
        1. The Golden Rule ("LLM generates text only, never structure") must be
           enforced consistently across all slot calls — one file, one source of truth.
        2. Domain experts (Army writing advisors) can tune the military writing rules
           without a code deployment.
        3. Version suffix (_v1) lets you ship a new prompt version alongside the old
           one for A/B testing without breaking existing slot tests.

    File: data/prompt_library/system_prompts/content_generation_v2.txt
    """
    # WHAT: Load from file, fall back to inline string if file is absent.
    # WHY:  or-fallback pattern keeps the slot functions working in
    #       environments where the prompt library hasn't been deployed yet.
    base = load_system_prompt("content_generation_v2") or _FALLBACK_BASE_SYSTEM
    
    # WHAT: Append doctype to the base prompt.
    # WHY:  Doctype provides context (GOI_LETTER vs MOVEMENT_ORDER) without
    #       letting the LLM change structure — structure is controlled by the
    #       skeleton JSON, not this prompt.
    return f"{base}\nDocument type: {doctype}\n"


async def run_slot(
    db: Session,
    doctype: str,
    task: str,
    schema_hint: str,
    retrieval_query: str,
    k_rules: int = 6,
) -> Dict[str, Any]:
    """
    Generic slot runner.

    Steps:
    1) Retrieve rulebook chunks for doctype + query (RAG)
    2) Construct system + user prompts
    3) Call the model via call_and_parse_json (includes repair once)
    4) Return parsed JSON dict (caller validates with Pydantic)
    """
    # Pull relevant rules (doctype filtered internally by search_rules)
    ctx = build_rules_context(db, doctype, retrieval_query, k=k_rules)
    rules_context = ctx["rules_context"]

    system = _base_system_prompt(doctype)

    # Important: Put rules first, then task.
    # The model tends to follow the most recent instruction, so we keep "Return JSON only" at end.
    user = (
        "RULES (must follow):\n"
        f"{rules_context}\n\n"
        "TASK:\n"
        f"{task}\n\n"
        "Return JSON only."
    )

    parsed = await call_and_parse_json(system=system, user=user, schema_hint=schema_hint)

    # Defensive: slots expect dict outputs
    if not isinstance(parsed, dict):
        return {}

    return parsed
