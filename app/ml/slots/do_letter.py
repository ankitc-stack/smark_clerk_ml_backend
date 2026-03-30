"""
app/ml/slots/do_letter.py

Fixes:
- Prevent signature-like outputs ("Sd/-", "Yours faithfully", etc.)
- Prevent placeholder copying ("<paragraph 1>", "string", "...")
- One fallback retry with a few-shot example
"""

from __future__ import annotations
import re
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.ml.slots.common import run_slot


class SalutationOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    salutation: str = ""


class BodyOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paras: List[str] = Field(default_factory=list)


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "that", "this", "these",
    "those", "it", "its", "not", "no", "all", "any", "some", "as", "if",
    "letter", "write", "dear", "sir", "regards", "kindly", "please",
    "demi", "official", "request", "about", "above", "subject", "matter",
    "action", "taken", "earliest", "draw", "attention", "under",
    "necessary", "sanction", "grateful", "further", "delay",
})


def _is_off_topic(paras: List[str], prompt: str) -> bool:
    """Return True if generated paragraphs contain none of the significant words from prompt.

    Detects generic boilerplate that ignores the REQUEST entirely.
    Uses word-boundary matching to avoid substring false-positives
    (e.g. "ration" inside "consideration").
    """
    prompt_words = {
        w.lower()
        for w in re.findall(r"\b[a-zA-Z]{4,}\b", prompt or "")
        if w.lower() not in _STOPWORDS
    }
    if not prompt_words:
        return False  # nothing to check against
    combined = " ".join(paras).lower()
    pattern = r"\b(?:" + "|".join(re.escape(w) for w in prompt_words) + r")\b"
    return not bool(re.search(pattern, combined))


def _looks_placeholder(s: str) -> bool:
    t = (s or "").strip().lower()
    if not t:
        return True
    # obvious placeholders
    if "..." in t or "<" in t or ">" in t:
        return True
    if "[" in t and "]" in t:   # [specific topic], [RECEIVER NAME], etc.
        return True
    if t in {"string", "paragraph", "<paragraph 1>", "<paragraph 2>"}:
        return True
    if re.fullmatch(r"para(?:graph)?\s*(one|two|three|1|2|3)\.?", t):
        return True
    if (t.startswith("{") and t.endswith("}")) or "'text'" in t or '"text"' in t:
        return True
    # Ordinal-only single word (e.g. "Firstly", "Secondly", "Thirdly") — no real content
    if re.fullmatch(r"(?:first|second|third|fourth|fifth)ly\.?", t):
        return True
    # Schema-hint template text that LLMs echo verbatim
    if re.search(r"(?:first|second|third)\s+substantive\s+paragraph", t):
        return True
    if re.search(r"substantive\s+paragraph\s+with\s+\d", t):
        return True
    # Rulebook OCR artifacts — "acknowledgement of PAGE"
    if "acknowledgement of page" in t or "of ... pages" in t:
        return True
    # signature markers we must never output in salutation/body slots
    if "sd/-" in t or "yours faithfully" in t or "yours sincerely" in t:
        return True
    if "signatory" in t or "under secretary" in t:
        return True
    return False


async def generate_salutation(db: Session, prompt: str) -> str:
    schema_hint = """
Return STRICT JSON with ALL keys present:
{
  "salutation": "My dear Brig Sharma,"
}
Rules:
- salutation must be ONE line ending with a comma.
- Must start with "My dear".
- Use addressee's rank and surname from REQUEST when available.
  Examples: "My dear Brig Rao,", "My dear Lt Col Kumar,", "My dear Sir," (fallback).
- NEVER output "Sd/-" or any signature text.
- NEVER output placeholders like "string", "<...>", "...".
"""

    task = (
        "Write ONLY the salutation line for a Demi-Official (DO) letter.\n"
        "Format: 'My dear [Rank Surname],' — use addressee's rank and surname from REQUEST.\n"
        "If no specific addressee name is mentioned, use 'My dear Sir,'\n"
        "Examples:\n"
        "  REQUEST mentions 'to Brig RS Rao' → 'My dear Brig Rao,'\n"
        "  REQUEST mentions 'to Lt Gen Anil Kumar' → 'My dear Anil Kumar,'\n"
        "  REQUEST mentions 'to Under Secretary' → 'My dear Sir,'\n"
        "Do NOT include any signature.\n\n"
        f"REQUEST:\n{(prompt or '').strip()}\n"
    )

    parsed = await run_slot(
        db=db,
        doctype="DO_LETTER",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="demi official letter salutation my dear rules",
        k_rules=6,
    )

    out = SalutationOut.model_validate(parsed)
    sal = out.salutation.strip()

    if _looks_placeholder(sal) or not sal.lower().startswith("my dear") or not sal.endswith(","):
        task2 = (
            "DO NOT use placeholders. DO NOT write signature.\n"
            "Examples: {\"salutation\":\"My dear Brig Rao,\"} or {\"salutation\":\"My dear Sir,\"}\n\n"
            + task
        )
        parsed = await run_slot(
            db=db,
            doctype="DO_LETTER",
            task=task2,
            schema_hint=schema_hint,
            retrieval_query="demi official letter salutation my dear rules",
            k_rules=6,
        )
        out = SalutationOut.model_validate(parsed)
        sal = out.salutation.strip()

    return sal


async def draft_body_paras(db: Session, prompt: str, min_paras: int = 2, max_paras: int = 3) -> List[str]:
    schema_hint = f"""
Return STRICT JSON — exactly this structure:
{{
  "paras": [
    "I write to bring to your personal notice that the matter concerns ...",
    "Additional details and context ...",
    "I shall be grateful if you could take the necessary action at the earliest."
  ]
}}
- Provide {min_paras} to {max_paras} paragraphs with REAL content from REQUEST.
- NEVER output generic filler like 'the matter under consideration' or 'the matter pertaining to the above subject'.
- NO signature, NO "Sd/-", NO designation lines.
"""

    _req = (prompt or "").strip()
    task = (
        "Draft ONLY the body paragraphs for a Demi-Official (DO) letter.\n"
        "Style: personal-official — writer addresses recipient directly as a peer.\n"
        "Para 1 MUST open with 'I write to bring to your personal notice that' followed by\n"
        "        the SPECIFIC subject from REQUEST (include unit, date, person, or quantity if given).\n"
        "Structure:\n"
        "  Para 1 — 'I write to bring to your personal notice that [SPECIFIC TOPIC FROM REQUEST].'\n"
        "            Add 1-2 sentences of context, background, or urgency from REQUEST.\n"
        "  Para 2 (if applicable) — Additional specifics, constraints, or implications from REQUEST.\n"
        "  Final para — Concrete request: state what action you need the recipient to take.\n"
        "IMPORTANT: This is an ORIGINAL letter, NOT a reply.\n"
        "Do NOT start with 'Reference your letter No' or any reference preamble.\n"
        "Do NOT include salutation, signature, designation, phone, enclosure, or copy-to.\n\n"
        "Reference example (structure only — use REQUEST content, NOT this text):\n"
        '{"paras":['
        '"I write to bring to your personal notice that the Annual Sports Meet scheduled for 15 April 2026 at Ambala Cantonment requires deputation of two officers from your unit for coordination duties. The nominated officers must report to the Sports Directorate, HQ Western Command, by 10 April 2026 along with their equipment manifest.",'
        '"The coordination team will be responsible for managing track events and liaisoning with the civilian administration for crowd management. Prior experience in event management will be an added advantage for the nominated officers.",'
        '"I shall be grateful if you could forward the names and contact details of the two nominated officers to this office by 05 April 2026 so that pre-event briefings may be arranged in time."'
        "]}\n\n"
        f"REQUEST:\n{_req}\n\n"
        f"Write {min_paras} to {max_paras} paragraphs about the REQUEST topic above."
    )

    # k_rules=0: skip army rulebook RAG for DO letter body content.
    # Army tactical/operational OCR chunks contaminate DO letter paragraphs for
    # civilian-adjacent topics (rations, electricity, tree cutting, etc.).
    parsed = await run_slot(
        db=db,
        doctype="DO_LETTER",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="demi official letter body tone rules",
        k_rules=0,
    )
    out = BodyOut.model_validate(parsed)
    paras = [p.strip() for p in out.paras if isinstance(p, str) and p.strip()]

    # Retry if: placeholder detected OR all paragraphs are off-topic (no REQUEST keywords)
    bad = (not paras) or any(_looks_placeholder(p) for p in paras) or _is_off_topic(paras, _req)
    if bad:
        _topic_hint = _req[:150]
        task2 = (
            f"Write {min_paras} to {max_paras} paragraphs for a DO letter about: {_topic_hint}\n\n"
            "Para 1 MUST start with 'I write to bring to your personal notice that' and MUST reference the topic above.\n"
            "Do NOT write about a different topic. Do NOT use generic filler sentences.\n"
            "Return ONLY JSON: {\"paras\": [\"para 1...\", \"para 2...\"]}\n"
        )
        parsed = await run_slot(
            db=db,
            doctype="DO_LETTER",
            task=task2,
            schema_hint=schema_hint,
            retrieval_query="demi official letter body tone rules",
            k_rules=0,
        )
        out = BodyOut.model_validate(parsed)
        paras = [p.strip() for p in out.paras if isinstance(p, str) and p.strip()]

    # Number paragraphs: "1.    text" (tab after number for hanging indent look)
    paras = [f"{i+1}.\t\t\t\t{p.lstrip('0123456789. ')}" for i, p in enumerate(paras)]

    # Final safety: if model still returned placeholder/empty content, derive a minimal
    # paragraph from the raw prompt so the document is never completely blank.
    if (not paras) or all(_looks_placeholder(p) for p in paras):
        core = (prompt or "").strip().rstrip(".")
        # Strip "do/goi letter to/about/for..." boilerplate so we get just the topic
        core = re.sub(
            r"^(?:write\s+(?:a\s+)?)?(?:do|goi|demi[\s-]official|government\s+of\s+india)\s+letter\s*(?:for|about|to|on|regarding)?\s*",
            "", core, flags=re.IGNORECASE,
        ).strip()
        # Strip addressee line "to [Rank] [Name] [, Designation]"
        core = re.sub(
            r"^to\s+(?:Lt\s+Col|Col|Brig|Maj\s+Gen|Lt\s+Gen|Gen|Maj|Capt|Lt)\s+[A-Za-z .]+?(?:,\s*[A-Za-z ,]+?)?\s+",
            "", core, flags=re.IGNORECASE,
        ).strip()
        # Strip trailing date fragment
        core = re.sub(r"\s*(?:dated?|on)\s+\d{1,2}[A-Za-z ]+\d{4}.*$", "", core, flags=re.IGNORECASE).strip()
        core = core.strip().rstrip(",").strip() or "the matter under reference"
        paras = [
            f"I request your kind consideration regarding the following matter: {core}.",
            "I shall be grateful if appropriate action is taken at the earliest.",
        ]

    # Pad to min_paras if LLM returned fewer than requested
    _STUB_CLOSING = "I shall be grateful if appropriate action is taken at the earliest."
    while len(paras) < min_paras:
        paras.append(_STUB_CLOSING)

    return paras[:max_paras]


# ── DO_LETTER: regex slot extractor ──────────────────────────────────────────

DO_KEYS = [
    "file_reference_number", "date", "event_date",
    "addressee_1", "addressee_2",
    "salutation", "subject",
    "signatory_name", "signatory_designation", "signatory_dept",
]


def _empty_do_fields() -> Dict[str, Any]:
    return {k: "" for k in DO_KEYS}


def _regex_fallback_do(prompt: str) -> Dict[str, Any]:
    """
    Fast regex extraction for DO letter metadata fields.
    Handles informal prompts like:
      "do letter to Lt Col NK Bedi, Under Secretary, about Army Day on 01 Mar 2026"
    And formal prompts like:
      "DO No 12345/Admin dated 01 Mar 2026 to Brig RS Rao ..."
    """
    p = prompt or ""
    out = _empty_do_fields()

    _date = r"[0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+[0-9]{4}"

    # ── Reference / DO number ─────────────────────────────────────────────────
    m = re.search(r"\bDO\s*No\.?\s*([A-Z0-9/_-]{3,30})", p, flags=re.IGNORECASE)
    if m:
        out["file_reference_number"] = m.group(1).strip()
    if not out["file_reference_number"]:
        m = re.search(r"\bRef(?:erence)?\s*No\.?\s*([A-Z0-9/_-]{3,30})", p, flags=re.IGNORECASE)
        if m:
            out["file_reference_number"] = m.group(1).strip()

    # ── Date ──────────────────────────────────────────────────────────────────
    m = re.search(rf"\bdated?\s*[:\-]?\s*({_date})\b", p, flags=re.IGNORECASE)
    if m:
        out["date"] = m.group(1).strip()
    # NOTE: "on [date]" is intentionally NOT used as a letter date fallback —
    # it matches event dates in the prompt (e.g. "celebration on 15 Jan 2026")
    # rather than the letter's own date.  Today's date is filled by main.py.
    # But we capture the event date so main.py can compute letter_date = event_date - 7 days.
    if not out["date"]:
        m_ev = re.search(rf"\bon\s+({_date})\b", p, flags=re.IGNORECASE)
        if m_ev:
            out["event_date"] = m_ev.group(1).strip()

    # ── Sender: "from Rank Name [Designation/Unit]" → signatory fields ─────────
    _ranks = r"(?:Lt\s+Col|Col|Brig|Maj\s+Gen|Lt\s+Gen|Gen|Maj|Capt|Lt|Hav|Sep|Sub|Nb\s+Sub|Sub\s+Maj)"
    _stop = r"(?:\s+to\s+(?:" + _ranks + r")|,\s*(?:about|on|dated|for|subject|regarding)|$)"
    m = re.search(
        rf"\bfrom\s+(.+?)(?=\s+to\s+(?:{_ranks})|\s+dated?\b|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        sender_full = m.group(1).strip().rstrip(",")
        # Reject if the captured text starts with a digit — that's a date/quantity,
        # not a signatory name (e.g. "from 15 May 2026" → don't set signatory_name)
        if sender_full and sender_full[0].isdigit():
            sender_full = ""
        # Split "Rank Name" from trailing designation (e.g. "Lt Gen Anil Kumar GOC 1 CORPS")
        name_m = re.match(rf"({_ranks}\s+(?:[A-Z][a-z]+\s*){{1,4}})(.*)", sender_full)
        if name_m:
            out["signatory_name"] = name_m.group(1).strip()
            desig = name_m.group(2).strip()
            if desig:
                out["signatory_designation"] = desig
        else:
            out["signatory_name"] = sender_full

    # ── Addressee: "to Rank Name [Designation/Unit]" ─────────────────────────
    m = re.search(
        rf"\bto\s+({_ranks}\s+[A-Za-z][A-Za-z0-9 .]+?)(?:\s+(?:about|on|dated|for|subject|re\b|regarding\b|requesting\b)|,\s*(?:Department|Ministry|Directorate|HQ)|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        addr_full = m.group(1).strip().rstrip(",")
        # Split "Rank Name" from trailing designation
        name_m2 = re.match(rf"({_ranks}\s+(?:[A-Z][a-z]+\s*){{1,4}})(.*)", addr_full)
        if name_m2:
            out["addressee_1"] = name_m2.group(1).strip()
            desig2 = name_m2.group(2).strip()
            # Reject designation if it looks like a verb phrase (starts lowercase,
            # or is a gerund like "requesting ...", "for ...", etc.) — not a real designation
            if desig2 and desig2[0].isupper() and not re.match(
                r"(?:requesting|regarding|for|about|on|to|and|who|which)\b", desig2, re.IGNORECASE
            ):
                out["addressee_2"] = desig2
        else:
            out["addressee_1"] = addr_full

    # Fallback: "to the [Organization/Dept]" — only if candidate starts uppercase
    if not out["addressee_1"]:
        m = re.search(
            r"\bto\s+(?:the\s+)?([A-Za-z ,()]{3,60}?)(?:\s+(?:about|on|for|re\b)|,\s*[A-Z]|$)",
            p,  # no IGNORECASE so [A-Z] only matches uppercase
        )
        if m:
            candidate = m.group(1).strip().rstrip(",")
            # Only take if first char is uppercase (proper noun / organization)
            if candidate and candidate[0].isupper():
                if not re.match(r"(?:all|unit|all\s+unit|be\s+held|held|signal|the\s+unit)\b", candidate, re.IGNORECASE):
                    out["addressee_1"] = candidate

    # ── Subject: several fallback patterns ────────────────────────────────────
    # 1a. "subject ALL_CAPS TEXT" — keyword followed by uppercase subject (no colon required)
    #     Only match uppercase text to avoid capturing body sentences after "subject" keyword.
    m = re.search(r"\bsubject\s+([A-Z][A-Z0-9 ,'-]{3,}?)(?:[,.]|\s+dated?|\s*$)", p)
    if m:
        out["subject"] = m.group(1).strip().rstrip(",")
    # 1b. Explicit "subject: any text" (with colon/dash separator)
    if not out["subject"]:
        m = re.search(r"\bsubject\s*[:\-]\s*(.+?)(?:\s+(?:dated?|to\s+[A-Z])|$)", p, flags=re.IGNORECASE)
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 2. "about X"
    if not out["subject"]:
        m = re.search(r"\babout\s+([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+on\s+|\s+dated?\s+|$)", p, flags=re.IGNORECASE)
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 3. "invitation/request for X" — stops only at "to/at" or "on [digit]" (a date);
    #    "on the occasion of" is part of the subject and must NOT be truncated.
    if not out["subject"]:
        m = re.search(
            r"\b(?:invitation|request)\s+for\s+([A-Za-z0-9][A-Za-z0-9 ,]+?)(?:\s+to\b|\s+at\b|\s+on\s+\d|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 4. "requesting X in/for/of Y" — gerund form
    if not out["subject"]:
        m = re.search(
            r"\brequesting\s+([A-Za-z][A-Za-z0-9 ]+?(?:\s+in\s+[A-Za-z0-9 ]+?)?)(?:\s+(?:raised|dated?|for|at|to)\b|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 5. General: extract core topic after "do letter" — skip if next word is "to" (addressee follows)
    if not out["subject"]:
        m = re.search(
            r"\bdo\s+letter\s+(?:for\s+|re\s+|regarding\s+)?(?!to\b)([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+to\b|\s+dated?\b|\s+on\b|\s+at\b|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()

    # ── Salutation — default "My dear Sir," ───────────────────────────────────
    m = re.search(r"\bMy\s+dear\s+([A-Za-z ,]+?)(?:\.|,|$)", p, flags=re.IGNORECASE)
    if m:
        out["salutation"] = f"My dear {m.group(1).strip().rstrip(',')},"
    else:
        out["salutation"] = "My dear Sir,"

    # ── Signatory: "signed by Name, Rank/Title" ───────────────────────────────
    m = re.search(
        r"\bsigned\s+by\s+([A-Za-z][A-Za-z .]+?)(?:,\s*([A-Za-z /]+?))?(?:\.|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["signatory_name"] = m.group(1).strip()
        if m.group(2):
            out["signatory_designation"] = m.group(2).strip()

    return out
