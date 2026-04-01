from __future__ import annotations

import re
from typing import List

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.ml.slots.common import run_slot

_RE_CERT = re.compile(
    r"\b(?:certificate|certif[yi]|bonafide|bona\s*fide|NOC|no[-\s]objection|clearance|attestation)\b",
    re.IGNORECASE,
)


def needs_to_whomsoever(prompt: str) -> bool:
    return bool(_RE_CERT.search(prompt))


class _BodyOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paras: List[str] = Field(default_factory=list)


async def draft_body_paras_general(
    db: Session,
    prompt: str,
    is_certificate: bool = False,
    min_paras: int = 2,
    max_paras: int = 3,
) -> List[str]:
    if is_certificate:
        task = (
            f"Write {min_paras}-{max_paras} paragraphs for a CERTIFICATE / NOC letter. "
            f"Para 1 MUST start with 'This is to certify that' or 'This office has no objection to'. "
            f"Use ONLY facts from the REQUEST — no invented names, dates, or ranks.\n\nREQUEST: {prompt}"
        )
    else:
        task = (
            f"Write {min_paras}-{max_paras} paragraphs for a formal general letter. "
            f"Para 1 MUST state the purpose clearly in the first sentence. "
            f"Use ONLY facts from the REQUEST — do NOT invent names, ranks, or numbers.\n\nREQUEST: {prompt}"
        )

    schema_hint = '{"paras": ["paragraph text...", "paragraph text..."]}'
    raw = await run_slot(db, "GENERAL_LETTER", task, schema_hint,
                         retrieval_query=prompt, k_rules=0)

    paras: List[str] = []
    for item in (raw.get("paras") or []):
        text = str(item).strip()
        if text and not text.startswith("<") and "..." not in text:
            paras.append(text)

    if not paras:
        paras = (
            [
                f"This is to certify that {prompt.strip()}.",
                "This certificate is issued for official purposes.",
            ]
            if is_certificate
            else [
                f"I write to bring to your kind notice that {prompt.strip()}.",
                "Necessary action may kindly be taken in the matter.",
            ]
        )

    return paras[:max_paras]
