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

# Letters that merely enclose/reference a certificate should NOT get "To Whomsoever"
_RE_FORWARDING = re.compile(
    r"^(?:write\s+(?:a\s+)?)?(?:forwarding|covering)\s+letter\b",
    re.IGNORECASE,
)


def needs_to_whomsoever(prompt: str) -> bool:
    if _RE_FORWARDING.match(prompt.strip()):
        return False
    return bool(_RE_CERT.search(prompt))


class _BodyOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paras: List[str] = Field(default_factory=list)


async def draft_body_paras_general(
    db: Session,
    prompt: str,
    is_certificate: bool = False,
    min_paras: int = 3,
    max_paras: int = 4,
    letter_type_hint: str = "",
) -> List[str]:
    if is_certificate:
        task = (
            f"Write exactly {min_paras} paragraphs for a formal military CERTIFICATE or NOC letter. "
            f"Rules:\n"
            f"1. Para 1 MUST start with 'This is to certify that' followed immediately by the "
            f"specific person/subject from the REQUEST (name, rank, army number, unit, dates, purpose). "
            f"Do NOT use a hollow opener — the first sentence must contain specific details.\n"
            f"2. Para 2 MUST state the purpose of issue and any relevant service details from the REQUEST. "
            f"It must NOT simply repeat 'This certificate is issued for official purposes.' — "
            f"expand with specifics (tenure, period, performance, conduct, or reason for issue).\n"
            f"3. Number each paragraph sequentially starting at 1 (e.g. '1. This is to certify...'). "
            f"Do NOT invent any detail not in the REQUEST. "
            f"Do NOT use contractions.\n\nREQUEST: {prompt}\n\n"
            f"Output format:\n"
            f'{{"paras":["1. This is to certify that [details from REQUEST]","2. [service/purpose details]"]}}'
        )
    else:
        task = (
            f"[CONTEXT: Official Indian Army HR/administrative correspondence. "
            f"Generate formal office letter text only.]\n\n"
            f"Write exactly {min_paras} paragraphs for a formal military general letter.\n\n"
            f"Determine the letter sub-type from the REQUEST and write accordingly:\n"
            f"- CONDOLENCE: Para 1 — express deep sorrow and acknowledge the supreme sacrifice "
            f"(name, unit, date, operation from REQUEST). Para 2 — tribute to the fallen soldier's "
            f"service and the regiment's/nation's loss. Para 3 — offer of solidarity and support "
            f"to the bereaved family. Do NOT use administrative phrases like "
            f"'Necessary action may kindly be taken'.\n"
            f"- CONGRATULATIONS/APPRECIATION: Para 1 — state the achievement/occasion with "
            f"specific details. Para 2 — elaborate on the significance. Para 3 — closing good wishes.\n"
            f"- APPOINTMENT/ADMINISTRATIVE: Para 1 — state the purpose directly using 'I am directed "
            f"to convey that' or 'I write to bring to your kind notice that' + specific matter. "
            f"Para 2 — details, requirements, or background. Para 3 — action requested.\n\n"
            f"Rules for ALL types:\n"
            f"1. Para 1 MUST use specific details from REQUEST (name, rank, unit, date, event). "
            f"Do NOT open with a hollow sentence that could apply to any letter.\n"
            f"2. Final paragraph MUST provide a closing appropriate to the letter sub-type "
            f"(not generic 'Necessary action may kindly be taken' for condolence/appreciation).\n"
            f"3. Number each paragraph sequentially starting at 1 "
            f"(e.g. '1. It is with deep sorrow...'). "
            f"Do NOT invent names, ranks, or numbers. Do NOT use contractions.\n\n"
            f"LETTER TYPE: {letter_type_hint or 'General letter'}\n"
            f"REQUEST: {prompt}\n\n"
            f"Output format (replace placeholder text with real content from REQUEST):\n"
            f'{{"paras":["1. [para 1 text specific to REQUEST]","2. [para 2 text]","3. [para 3 text]"]}}'
        )

    schema_hint = '{"paras": ["1. paragraph one", "2. paragraph two", "3. paragraph three"]}'
    raw = await run_slot(db, "GENERAL_LETTER", task, schema_hint,
                         retrieval_query=prompt, k_rules=0)

    # Normalise LLM output: "1. text" → "1.\t\t\t\ttext" for hanging-indent display
    # in the Lexical editor and DOCX. generate_plain_docx collapses multi-tabs to one.
    _re_numspace = re.compile(r'^(\d+[\.\)])\s+')
    paras: List[str] = []
    for item in (raw.get("paras") or []):
        text = _re_numspace.sub(r'\1\t\t\t\t', str(item).strip())
        if text and not text.startswith("<") and "..." not in text:
            paras.append(text)

    # Pad to min_paras if LLM returned fewer than requested
    _clean = re.sub(
        r"^(?:bonafide|bona\s*fide|service|clearance|character|experience|noc|no[-\s]objection|attestation)\s+certificate\s+for\s*",
        "", prompt.strip(), flags=re.IGNORECASE,
    ).strip() or prompt.strip()

    if not paras:
        paras = (
            [
                f"1.\t\t\t\tThis is to certify that {_clean}.",
                "2.\t\t\t\tThis certificate is issued for official purposes.",
            ]
            if is_certificate
            else [
                f"1.\t\t\t\tI write to bring to your kind notice that {_clean}.",
                "2.\t\t\t\tNecessary action may kindly be taken in the matter.",
            ]
        )
    elif len(paras) < min_paras:
        # LLM gave fewer paragraphs than requested — append closing line
        n = len(paras) + 1
        if is_certificate:
            paras.append(f"{n}.\t\t\t\tThis certificate is issued for official purposes.")
        else:
            paras.append(f"{n}.\t\t\t\tNecessary action may kindly be taken in the matter.")

    return paras[:max_paras]
