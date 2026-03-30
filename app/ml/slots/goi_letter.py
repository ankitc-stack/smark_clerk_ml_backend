"""
app/ml/slots/goi_letter.py

GOI letter slots (Month-1 scope):
1) generate_subject() -> returns a single-line subject string (no "Subject:" prefix)
2) draft_numbered_paras() -> returns list of numbered paragraphs ["1. ...", "2. ..."]

Rules:
- Content only (renderer handles layout)
- Paras must start with "1.", "2.", etc.
"""

from __future__ import annotations
import ast
import re
from typing import List

from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.ml.slots.common import run_slot


# ---------------------------------------------------------------------------
# Saka (Indian National Calendar) conversion
# ---------------------------------------------------------------------------

_SAKA_MONTHS = [
    "Chaitra", "Vaisakha", "Jyaistha", "Asadha", "Sravana", "Bhadra",
    "Asvina", "Kartika", "Agrahayana", "Pausa", "Magha", "Phalguna",
]


def _parse_date_str(s: str):
    """Parse date strings like '28 Feb 2026', '1st March 2026' → datetime.date or None."""
    import datetime
    s = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", s).strip()
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _to_saka_date(dt) -> str:
    """Convert a Gregorian date (datetime.date or string) to Indian National Calendar (Saka) string.

    Returns e.g. '26 Phalguna 1947 Saka'.
    Falls back silently to empty string if convertdate is not installed or date unparseable.
    """
    try:
        import datetime
        if isinstance(dt, str):
            dt = _parse_date_str(dt)
        if dt is None:
            return ""
        from convertdate import indian_civil
        y, m, d = indian_civil.from_gregorian(dt.year, dt.month, dt.day)
        return f"{d} {_SAKA_MONTHS[m - 1]} {y} Saka"
    except Exception:  # pragma: no cover
        return ""


# --- Output contracts for these slots ---
class SubjectOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject: str = ""


class ParasOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paras: List[str] = Field(default_factory=list)


def _is_placeholder_para(value: str) -> bool:
    s = (value or "").strip().lower()
    if not s:
        return True
    if s in {"1. ...", "2. ...", "...", "<para 1>", "<para 2>", "<para 3>"}:
        return True
    if s.endswith("..."):
        return True
    if s.startswith("<para") or ("<" in s and ">" in s):
        return True
    return False


# ── GOI_LETTER: regex slot extractor ─────────────────────────────────────────

GOI_KEYS = [
    "file_reference_number", "date_gregorian", "date_indian",
    "ministry_name", "address_line_1", "address_line_2",
    "telephone", "email",
    "addressee_1", "addressee_2",
    "subject",
    "signatory_designation", "signatory_dept",
]


def _regex_fallback_goi(prompt: str) -> dict:
    """
    Fast regex extraction for GOI letter metadata fields.
    Handles prompts like:
      "GOI letter to Secy MoD about ACR for 2025-26, dated 28 Feb 2026, Ref No B/45678/AG/2026"
      "GOI letter requesting inputs for Parliament Session by 15 Mar 2026, Ref No AG/12345/Parl/2026"
    """
    p = prompt or ""
    out = {k: "" for k in GOI_KEYS}

    _date = r"[0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+[0-9]{4}"

    # ── Reference number ─────────────────────────────────────────────────────
    m = re.search(r"\bRef(?:erence)?\s*No\.?\s*([A-Za-z0-9/_-]{3,30})", p, flags=re.IGNORECASE)
    if m:
        out["file_reference_number"] = m.group(1).strip()

    # ── Date ─────────────────────────────────────────────────────────────────
    m = re.search(rf"\bdated?\s*[:\-]?\s*({_date})\b", p, flags=re.IGNORECASE)
    if m:
        d = m.group(1).strip()
        out["date_gregorian"] = d
        out["date_indian"] = _to_saka_date(d) or d
    # NOTE: "on [date]" skipped — matches event dates, not the letter date.
    # Today's date is auto-filled by main.py when date_gregorian is empty.
    if not out["date_gregorian"]:
        m = re.search(rf"\bby\s+({_date})\b", p, flags=re.IGNORECASE)
        if m:
            d = m.group(1).strip()
            out["date_gregorian"] = d
            out["date_indian"] = _to_saka_date(d) or d

    # ── Addressee: "to [Title] [Name/Org], [Dept]" ───────────────────────────
    _titles = (
        r"(?:Secy|Secretary|Jt\s+Secy|Joint\s+Secretary|JS|US|Under\s+Secretary|"
        r"Additional\s+Secretary|Addl\s+Secy|AS|Director\s+General|DG|ADG|"
        r"Lt\s+Col|Col|Brig|Maj\s+Gen|Lt\s+Gen|Gen)"
    )
    m = re.search(
        rf"\bto\s+({_titles}(?:\s+[A-Za-z][A-Za-z .]+?)?)(?:,\s*([A-Za-z][A-Za-z ,()]+?))?(?:\s+(?:about|on|dated|for|subject|re\b|regarding)|,\s*(?:Ministry|Department|Directorate|MoD|HQ)|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["addressee_1"] = m.group(1).strip().rstrip(",")
        if m.group(2):
            out["addressee_2"] = m.group(2).strip().rstrip(",")

    # Fallback: "to [Organization]" — only if starts uppercase (proper noun)
    if not out["addressee_1"]:
        m = re.search(
            r"\bto\s+(?:the\s+)?([A-Za-z ,()]{3,60}?)(?:\s+(?:about|on|for|re\b|regarding)|,\s*[A-Z]|$)",
            p,  # no IGNORECASE so [A-Z] only matches uppercase
        )
        if m:
            candidate = m.group(1).strip().rstrip(",")
            if candidate and candidate[0].isupper():
                if not re.match(r"(?:all|unit|be\s+held|held|signal)\b", candidate, re.IGNORECASE):
                    out["addressee_1"] = candidate

    # ── Subject ───────────────────────────────────────────────────────────────
    # 1. Explicit "subject: X" or "subject- X"
    m = re.search(r"\bsubject\s*[:\-]\s*(.+?)(?:\s+(?:dated?|to\s+[A-Z])|$)", p, flags=re.IGNORECASE)
    if m:
        out["subject"] = m.group(1).strip().upper()
    # 2. "about X" — stop before "on/dated/for/by"
    if not out["subject"]:
        m = re.search(r"\babout\s+([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+(?:on|dated?|for|by|to)\b|,\s*[A-Z]|$)", p, flags=re.IGNORECASE)
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 3. "regarding X"
    if not out["subject"]:
        # "regarding X" — stop at ", to", "to <UPPER>", "dated", "on <digit>", "by <digit>"
        # Do NOT stop at "for" — subjects like "regarding budget allocation for modernization" need "for"
        m = re.search(
            r"\bregarding\s+([A-Za-z][A-Za-z0-9 ,/-]+?)(?=\s*,\s*to\b|\s+to\s+[A-Z]|\s+dated?\b|\s+on\s+\d|\s+by\s+\d|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 4. "requesting X from/by" — do NOT stop at "for" (subject can include "for X")
    if not out["subject"]:
        m = re.search(
            r"\brequesting\s+([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+from\b|\s+by\s+\d|\s+by\s+[A-Z]{2}|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 5. General: after "GOI letter [for/re/regarding]" — skip "to" word
    if not out["subject"]:
        m = re.search(
            r"\bGOI\s+letter\s+(?:for\s+|re\s+|regarding\s+)?(?!to\b)([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+to\b|\s+dated?\b|\s+on\b|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 6. "[type] letter for/about X to Y" — invitation/request/circular letter
    if not out["subject"]:
        m = re.search(
            r"\b(?:invitation|request|reminder|circular|notification)\s+letter\s+(?:for\s+|about\s+)([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+to\b|\s+at\b|\s+dated?\b|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()
    # 7. Bare "invitation for X" / "invitation to X" when no "letter" keyword
    if not out["subject"]:
        m = re.search(
            r"\binvitation\s+(?:for\s+|to\s+attend\s+)([A-Za-z][A-Za-z0-9 ,]+?)(?:\s+to\b|\s+at\b|\s+dated?\b|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["subject"] = m.group(1).strip().upper()

    # ── Ministry / sender department ──────────────────────────────────────────
    m = re.search(
        r"\bfrom\s+(?:the\s+)?([A-Za-z][A-Za-z &()/-]{3,60}?)(?:\s+(?:ref|dated?|to\b|regarding|about|on\b)|,|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip().rstrip(",")
        # Exclude month names
        _is_month = re.match(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", candidate, re.IGNORECASE)
        # Exclude single-word proper nouns that appear in "from X to Y" travel routes
        # (e.g. "from Pathankot to Leh" — Pathankot is a station, not a ministry)
        _is_route_place = (
            " " not in candidate
            and bool(re.search(rf"\bfrom\s+{re.escape(candidate)}\s+to\b", p, re.IGNORECASE))
        )
        if not _is_month and not _is_route_place:
            out["ministry_name"] = candidate

    # ── Signatory ─────────────────────────────────────────────────────────────
    m = re.search(
        r"\bsigned\s+by\s+([A-Za-z][A-Za-z .]+?)(?:,\s*([A-Za-z /]+?))?(?:\.|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["signatory_designation"] = m.group(1).strip()
        if m.group(2):
            out["signatory_dept"] = m.group(2).strip()

    # Auto-populate signatory_dept from ministry when no explicit signatory given
    if not out["signatory_dept"] and out["ministry_name"]:
        out["signatory_dept"] = out["ministry_name"]

    return out


async def generate_subject(db: Session, prompt: str) -> str:
    """
    Generate a GOI-letter-style subject line.

    Output:
    - a single line subject string in ALL CAPS (no "Subject:" prefix)
    """
    schema_hint = """
Return STRICT JSON with required keys:
{
  "subject": "SUBMISSION OF ANNUAL PERFORMANCE REPORTS FOR 2025-26"
}
Rules:
- subject must be a SINGLE LINE in ALL CAPITALS.
- Must be a specific noun phrase (6-12 words) derived from the actual REQUEST.
- Do NOT include "Subject:" prefix.
- Do NOT output generic placeholders like "SUBJECT OF THE LETTER" or "string".
"""

    task = (
        "Write the SUBJECT line for a Government of India (GOI) official letter.\n"
        "Rules:\n"
        "- ALL CAPITALS, noun phrase, specific, 6-12 words.\n"
        "- Derived directly from the actual topic in REQUEST — not generic text.\n"
        "- Examples of correct subjects:\n"
        "  'GRANT OF SPECIAL CASUAL LEAVE — ARMY DAY PARADE 2026'\n"
        "  'SUBMISSION OF ANNUAL CONFIDENTIAL REPORTS FOR YEAR 2025-26'\n"
        "  'BUDGET ALLOCATION FOR MODERNISATION OF SIGNALS INFRASTRUCTURE'\n"
        "  'REQUEST FOR INPUTS FOR FORTHCOMING PARLIAMENT SESSION'\n\n"
        f"REQUEST:\n{prompt}\n"
    )

    parsed = await run_slot(
        db=db,
        doctype="GOI_LETTER",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="GOI letter subject format rules",
        k_rules=6,
    )

    out = SubjectOut.model_validate(parsed)

    subj = out.subject.strip()
    if not subj or subj.lower() in {"string", "<one-line subject text>"} or "..." in subj:
        parsed = await run_slot(
            db=db,
            doctype="GOI_LETTER",
            task=("DO NOT use placeholders. Write the REAL subject in ALL CAPS.\n\n" + task),
            schema_hint=schema_hint,
            retrieval_query="GOI letter subject format rules",
            k_rules=6,
        )
        out = SubjectOut.model_validate(parsed)
        subj = out.subject.strip()

    # Ensure ALL CAPS, no trailing full stop (JSSD Ch.10: no full stop after titles/headings)
    return subj.upper().rstrip(".") if subj else subj


def _extract_paras(parsed: dict) -> List[str]:
    """
    Extract para strings from the raw LLM JSON dict.
    Handles four LLM output formats:
      1. {"paras": ["1. text...", "2. text..."]}          — list of strings (correct)
      2. {"paras": [{"number": 1, "text": "..."}, ...]}   — list of objects (hallucination)
      3. {"paras": ["{'number': 1, 'text': '...'}", ...]} — str(dict) repr from schema coercion
      4. {"paras": ["1. {'text': '...'}", ...]}           — numbered dict-repr (normalization prefix)
    """
    raw = (parsed or {}).get("paras") or []
    result: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            # Format 2: actual dict
            text = str(item.get("text") or item.get("content") or item.get("paragraph") or "").strip()
            if text:
                num = item.get("number") or ""
                # Strip any "N. " prefix the LLM baked into text (military rules tell it to).
                # Without this, f"{num}. {text}" produces "1. 1. The Ministry..." (double prefix).
                text_body = re.sub(r"^\d+\.\s+", "", text).strip()
                result.append(f"{num}. {text_body}" if num else text_body)
        elif isinstance(item, str):
            s = item.strip()

            # Format 3/4: dict-repr string, possibly with a leading "N. " prefix added
            # by a previous normalization pass.
            # Match: optional "N. " + "{ ... 'text' ... }"
            dm = re.match(r"^(\d+\.\s+)?(\{.+)", s, re.DOTALL)
            if dm and ("'text'" in s or '"text"' in s):
                num_prefix = (dm.group(1) or "").strip().rstrip(".")
                s_dict = dm.group(2)
                extracted = None

                # Try ast.literal_eval first (handles most cases)
                try:
                    d = ast.literal_eval(s_dict)
                    if isinstance(d, dict):
                        text = str(d.get("text") or d.get("content") or d.get("paragraph") or "").strip()
                        if text:
                            num = str(d.get("number") or num_prefix or "")
                            text_body = re.sub(r"^\d+\.\s+", "", text).strip()
                            extracted = f"{num}. {text_body}" if num else text_body
                except Exception:
                    pass

                # Regex fallback when literal_eval fails (mixed quotes, truncation, etc.)
                if extracted is None:
                    m2 = re.search(
                        r"""['"]text['"]\s*:\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")""",
                        s_dict,
                    )
                    if m2:
                        text = (m2.group(1) or m2.group(2) or "").strip()
                        if text:
                            num = num_prefix or ""
                            text_body = re.sub(r"^\d+\.\s+", "", text).strip()
                            extracted = f"{num}. {text_body}" if num else text_body

                if extracted is not None:
                    result.append(extracted)
                    continue

            # Format 1: normal string
            result.append(s)
    return [p for p in result if p]


async def draft_numbered_paras(db: Session, prompt: str, min_paras: int = 2, max_paras: int = 4) -> List[str]:
    """
    Draft numbered paragraphs for GOI letter body.

    Output:
    - list like ["1. ...", "2. ..."] (already numbered)
    """
    schema_hint = f"""
Return STRICT JSON with ALL keys present:
{{
  "paras": [
    "1. I am directed to convey that [specific matter from REQUEST with who/what/when details]. [Supporting context sentence. Deadline or requirement if applicable.]",
    "2. [Elaboration: compliance requirements, stakeholders, timeline, or additional specifics from REQUEST.]",
    "3. It is requested that necessary action may be taken at the earliest and compliance intimated to this Ministry/HQ by [date if known]."
  ]
}}
Rules:
- Provide {min_paras} to {max_paras} numbered paragraphs, each 2-4 substantive sentences.
- Para 1 MUST open with "I am directed to convey that" followed by specific content from REQUEST.
- Each paragraph must include concrete details from REQUEST (names, dates, units, deadlines).
- Final paragraph must close with a specific compliance/action request.
- NEVER output placeholders like "<para 1>", "1. ...", "paragraph 1", "...".
- NO signature block, NO "Yours faithfully", NO "Sd/-".
"""

    task = (
        "Draft ONLY the numbered body paragraphs for a Government of India (GOI) official letter.\n"
        "Tone: formal, authoritative, official — as written by a senior government officer.\n"
        "Structure:\n"
        "  Para 1 — Open with 'I am directed to convey that' + state the core matter from REQUEST\n"
        "            with specific details (who is affected, what is required, when by).\n"
        "            Add 1-2 more sentences of context or background from REQUEST.\n"
        "  Para 2 (if needed) — Elaborate: compliance requirements, timeline, stakeholders,\n"
        "                        or procedural details drawn from REQUEST.\n"
        "  Final para — 'It is requested that necessary action may be taken at the earliest\n"
        "               and compliance intimated to this Ministry/HQ [by date if known].'\n"
        "IMPORTANT: This is an ORIGINAL letter, NOT a reply. "
        "Do NOT start with 'Reference your letter No' or any reference preamble. "
        "Do NOT include salutation, signature, or enclosure.\n\n"
        "Reference example (structure only — use REQUEST content, not this text):\n"
        '{"paras":['
        '"1. I am directed to convey that all Heads of Departments are requested to submit the Annual Performance Reports for the year 2025-26 by 31 March 2026. Reports received after the due date will not be considered for assessment purposes.",'
        '"2. Departments are also directed to ensure that all pending entries from the previous financial year are reconciled and cleared prior to submission. Incomplete submissions will be returned for rectification without processing.",'
        '"3. It is requested that necessary action may be taken at the earliest and compliance intimated to this Ministry by the stipulated date."'
        "]}\n\n"
        f"REQUEST:\n{prompt}\n\n"
        f"Return between {min_paras} and {max_paras} numbered paragraphs, each 2-4 substantive sentences."
    )

    # k_rules=0: skip army rulebook RAG for body content.
    # Army tactical/operational OCR chunks contaminate GOI letter paragraphs.
    # The task prompt alone gives the model enough context.
    parsed = await run_slot(
        db=db,
        doctype="GOI_LETTER",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="GOI letter body numbered paragraphs rules",
        k_rules=0,
    )

    paras = _extract_paras(parsed)
    # Only retry if ALL paras are empty/placeholders.
    # Un-numbered real paragraphs are fine — normalization below will add "1.", "2.", etc.
    all_bad = (not paras) or all(_is_placeholder_para(p) for p in paras)
    if all_bad:
        parsed = await run_slot(
            db=db,
            doctype="GOI_LETTER",
            task=("DO NOT use placeholders. Write REAL substantive paragraphs (2-4 sentences each).\n\n" + task),
            schema_hint=schema_hint,
            retrieval_query="GOI letter body numbered paragraphs rules",
            k_rules=0,
        )
        paras = _extract_paras(parsed)

    # Final normalization after single fallback retry.
    # Use 4 tabs between number and text for consistent editor visual spacing.
    _TAB5 = "\t\t\t\t"
    normalized: List[str] = []
    for idx, p in enumerate(paras, start=1):
        if _is_placeholder_para(p):
            continue
        if re.match(r"^\d+\.\s+\S", p):
            normalized.append(re.sub(r'^(\d+\.)\s+', rf'\1{_TAB5}', p))
        else:
            normalized.append(f"{idx}.{_TAB5}{p}")

    # Final safety: if model still returned no usable content, derive minimal paragraphs
    # from the raw prompt so the document is never blank.
    if not normalized:
        import re as _re
        core = (prompt or "").strip().rstrip(".")
        # Strip "goi/do letter to/about/for..." boilerplate so we embed just the topic
        core = _re.sub(
            r"^(?:write\s+(?:a\s+)?)?(?:do|goi|demi[\s-]official|government\s+of\s+india)\s+letter\s*(?:for|about|to|on|regarding)?\s*",
            "", core, flags=_re.IGNORECASE,
        ).strip()
        core = _re.sub(r"\s*(?:dated?|on)\s+\d{1,2}[A-Za-z ]+\d{4}.*$", "", core, flags=_re.IGNORECASE).strip()
        core = core.strip().rstrip(",").strip() or "the matter under reference"
        normalized = [
            f"1.{_TAB5}This is with reference to {core}.",
            f"2.{_TAB5}It is requested that necessary action may be taken expeditiously and compliance intimated to this office.",
        ]

    return normalized[:max_paras]
