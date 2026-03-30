"""
app/ml/slots/movement_order.py

Slot extraction for Movement Orders (IAFT-1759 format).
- _regex_fallback_mo : fast deterministic field extraction (no LLM)
- draft_numbered_paras / draft_distribution_lines : legacy LLM-based helpers
"""

from __future__ import annotations
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.ml.slots.common import run_slot
# Reuse date helpers from leave_certificate
from app.ml.slots.leave_certificate import _MONTH_ABBR, _normalize_date


MO_KEYS = [
    "army_no", "rank", "person_name", "unit", "att_unit",
    "destination", "departure_date", "departure_time",
    "route", "destination_desc", "remarks", "distr_unit",
    "signatory_name", "signatory_designation", "signatory_dept",
]


def _empty_mo_fields() -> Dict[str, str]:
    return {k: "" for k in MO_KEYS}


def _regex_fallback_mo(prompt: str) -> Dict[str, str]:
    """
    Fast regex extraction for movement order fields.
    Handles formal prompts:
        "No 10525911F Rank Sep Name Surendra Singh of 153 Inf Bn (TA) DOGRA
         att with TAIC, proceeding on temp duty to DG INF on 25 Sept 2024"
    And informal prompts:
        "movement order for sep raj kumar of 153 inf bn to dg inf on 25 mar 2026"
    """
    p = prompt or ""
    out = _empty_mo_fields()

    # Flexible date: DD Mon YYYY, 1st March 2026, 25 Sept 2024
    _date = r"[0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+[0-9]{4}"

    # ── Army Number ────────────────────────────────────────────────────────
    m = re.search(r"\bNo\.?\s+([A-Z]{0,3}\d{5,10}[A-Z]?)\b", p, flags=re.IGNORECASE)
    if m:
        out["army_no"] = m.group(1).strip().upper()
    if not out["army_no"]:
        m = re.search(r"\b([A-Z]{1,3}-\d{4,8}[A-Z]?)\b", p, flags=re.IGNORECASE)
        if m:
            out["army_no"] = m.group(1).strip().upper()
    if not out["army_no"]:
        m = re.search(r"\b(\d{7,10}[A-Z])\b", p, flags=re.IGNORECASE)
        if m:
            out["army_no"] = m.group(1).strip().upper()

    # ── Rank ──────────────────────────────────────────────────────────────
    m = re.search(r"\bRank\s+([A-Za-z/\s]+?)(?:\s+Name\b|\s+of\b|,|$)", p)
    if m:
        out["rank"] = m.group(1).strip()
    else:
        rank_list = [
            "Nb Sub", "Sub Maj", "Sub", "Hav", "Nk", "L/Nk",
            "Sep", "Rfn", "Gnr", "Sigmn", "Spr",
            "Capt", "Maj", "Lt Col", "Col", "Brig", "Gen",
            "Lt", "Flt Lt",
        ]
        for rnk in rank_list:
            if re.search(rf"\b{re.escape(rnk)}\b", p, flags=re.IGNORECASE):
                out["rank"] = rnk
                break

    # ── Person Name ───────────────────────────────────────────────────────
    # 1. Formal "Name: XYZ" pattern
    m = re.search(r"\bName[:\s]+([A-Z][A-Za-z .]+?)(?:\s+of\b|\s+is\b|,|$)", p)
    if m:
        out["person_name"] = m.group(1).strip()

    # 2. Rank-context extraction (preferred when rank is known — avoids capturing
    #    function phrases like "temporary duty" from "for ... of" pattern).
    #    Lookahead stops at: army-number digits, "of", "No.", "att", "is", comma, dot, end.
    if not out["person_name"] and out["rank"]:
        rnk_pat = re.escape(out["rank"])
        m = re.search(
            rf"\b{rnk_pat}\s+([A-Za-z][A-Za-z .]+?)(?=\s+(?:No\.?\b|\bof\b|\batt\b|\bis\b|\d)|[,.]|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["person_name"] = m.group(1).strip()

    # 3. Fallback: "for XYZ of/to/from" — only when rank gave no result.
    #    Excludes duty/function phrases (e.g. "for temporary duty of").
    #    Stop capture at army-number digit sequence before "of".
    if not out["person_name"]:
        m = re.search(
            r"\bfor\s+([A-Za-z][A-Za-z .]+?)(?=\s+(?:\d|\bof\b|\bto\b|\bfrom\b)|[,.]|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip()
            if not re.match(r"(?:temporary|temp|permanent|official|duty)\b", candidate, re.IGNORECASE):
                out["person_name"] = candidate

    # Strip rank prefix if still present in captured name
    if out["person_name"] and out["rank"]:
        pname_low = out["person_name"].lower()
        if pname_low.startswith(out["rank"].lower()):
            out["person_name"] = out["person_name"][len(out["rank"]):].strip()

    # ── Unit ──────────────────────────────────────────────────────────────
    m = re.search(
        r"\bof\s+(\d+\s+[A-Za-z][A-Za-z\s()/]*?)(?:\s+att\b|\s+with\b|\s+is\b|\s+to\b|,|\.|$)", p
    )
    if m:
        out["unit"] = m.group(1).strip()

    # ── Attached Unit ─────────────────────────────────────────────────────
    m = re.search(
        r"\batt(?:\s+with)?\s+([A-Za-z][A-Za-z\s()]+?)(?:\s+(?:is|will|proceed(?:ing)?|report|on\s+temp|on\s+duty)\b|,|\.|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["att_unit"] = m.group(1).strip()
    if not out["att_unit"]:
        m = re.search(
            r"\bwith\s+([A-Z][A-Za-z\s()]{1,30}?)(?:\s+(?:is|will|for|to|proceed(?:ing)?)\b|,|\.|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip()
            if not re.match(r"(?:permission|effect|wef|from|ref)\b", candidate, re.IGNORECASE):
                out["att_unit"] = candidate

    # ── Destination ────────────────────────────────────────────────────────
    # Stop words: departure, on, wef, from, for, comma, period
    _dest_stop = r"(?:\s+(?:departure|depart|on|wef|from|for|via|route)\b|,|\.|$)"
    m = re.search(
        r"\b(?:proceed(?:ing)?(?:\s+on\s+(?:temp\s+)?duty)?\s+to|duty\s+at|report\s+to)\s+"
        r"([A-Za-z][A-Za-z0-9\s()/-]{1,40}?)" + _dest_stop,
        p, flags=re.IGNORECASE,
    )
    if m:
        out["destination"] = m.group(1).strip()
    if not out["destination"]:
        m = re.search(
            r"\bto\s+([A-Za-z][A-Za-z0-9\s()/-]{1,35}?)" + _dest_stop,
            p, flags=re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip()
            if not re.match(r"(?:the|a|an|this|that|proceed|report)\b", candidate, re.IGNORECASE):
                out["destination"] = candidate

    # ── Departure Date ────────────────────────────────────────────────────
    # "Departure date and time : 25 Sept 2024" / "departure 15 Mar 2026" / "on/wef/from DD Mon YYYY"
    m = re.search(
        rf"\bDeparture\s+(?:date(?:\s+and\s+time)?\s*[:\-]?\s*)({_date})\b",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["departure_date"] = _normalize_date(m.group(1))
    if not out["departure_date"]:
        m = re.search(rf"\b(?:departure|depart|on|wef|from|dated)\s+({_date})\b", p, flags=re.IGNORECASE)
        if m:
            out["departure_date"] = _normalize_date(m.group(1))
    if not out["departure_date"]:
        m = re.search(rf"({_date})\s*$", p.strip(), flags=re.IGNORECASE)
        if m:
            out["departure_date"] = _normalize_date(m.group(1))

    # ── Departure Time: 0600H / FN / AN / Forenoon / Afternoon ───────────
    m = re.search(r"\b(\d{4})\s*[Hh]\b", p)   # e.g. 0600H, 1430H
    if m:
        out["departure_time"] = m.group(1) + "H"
    if not out["departure_time"]:
        m = re.search(r"\b(FN|AN|forenoon|afternoon)\b", p, flags=re.IGNORECASE)
        if m:
            abbr = {"forenoon": "FN", "afternoon": "AN"}.get(m.group(1).lower(), m.group(1).upper())
            out["departure_time"] = abbr

    # ── Route (default MR unless specified) ──────────────────────────────
    m = re.search(r"\b(?:route|via)\s*[:\-]?\s*([A-Za-z]+)\b", p, flags=re.IGNORECASE)
    if m:
        out["route"] = m.group(1).upper()
    else:
        out["route"] = "MR"

    # ── Destination description (para 3, default "Known to the indl") ─────
    m = re.search(r"\bKnown\s+to\s+the\s+indl\b", p, flags=re.IGNORECASE)
    if m:
        out["destination_desc"] = m.group(0)
    else:
        out["destination_desc"] = "Known to the indl"

    # ── Remarks (default "Proceeding on temp duty") ───────────────────────
    m = re.search(r"\bRemarks?\s*[:\-]\s*([^\n.]+)", p, flags=re.IGNORECASE)
    if m:
        out["remarks"] = m.group(1).strip()
    else:
        out["remarks"] = "Proceeding on temp duty"

    # ── Distribution unit (same as parent unit) ───────────────────────────
    out["distr_unit"] = out["unit"] or ""

    # ── Signatory: "signed by Maj RS Bhalia OIC TAIC" ─────────────────────
    # Use a strict rank pattern so only the rank abbreviation is captured,
    # not the first word of the name ("RS" in "Maj RS Bhalia").
    _RANK_PAT = (
        r"(?:Gen|Lt\s+Gen|Maj\s+Gen|Brig|Col|Lt\s+Col|Maj|Capt|Lt|"
        r"Sub|Nb\s+Sub|Hav|L/Hav|Nk|L/Nk|Sep|Rfn|Gnr|Spr|Ptr|Sgt|"
        r"WO[12]?|Nb\s+Sub|JCO|NCO)"
    )
    m = re.search(
        r"\bsign(?:ed)?\s+by\s+"
        r"(?:(" + _RANK_PAT + r")\s+)?"               # group 1: optional rank
        r"([A-Za-z][A-Za-z.]+(?:\s+[A-Za-z]+)?)"      # group 2: name (1-2 words)
        r"(?:\s+([A-Za-z][A-Za-z\s/]{1,40}))?$",      # group 3: appointment/dept
        p, flags=re.IGNORECASE,
    )
    if m:
        out["signatory_designation"] = (m.group(1) or "").strip()
        out["signatory_name"]        = (m.group(2) or "").strip()
        out["signatory_dept"]        = (m.group(3) or "").strip()

    return out


class ParasOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paras: List[str] = Field(default_factory=list)


class DistOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lines: List[str] = Field(default_factory=list)


_MO_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "that", "this", "these",
    "those", "it", "its", "not", "no", "all", "any", "some", "as", "if",
    "individual", "proceed", "report", "duty", "unit", "order", "comply",
    "standing", "orders", "applicable", "station", "return", "resume",
    "duties", "journey", "performed", "route", "there", "arrival",
})


def _bad_para(p: str, prompt: str) -> bool:
    t = (p or "").strip().lower()
    if not t:
        return True
    if "..." in t or "<" in t or ">" in t:
        return True
    if "[" in t and "]" in t:
        return True
    if "first instruction" in t or "second instruction" in t:
        return True

    # Require at least one significant word from prompt to appear in the paragraph.
    # Catches generic boilerplate that ignores the REQUEST topic (rations, exercise, etc.).
    # Uses word-boundary matching to avoid substring false-positives
    # (e.g. "ration" inside "consideration").
    prompt_words = {
        w.lower()
        for w in re.findall(r"\b[a-zA-Z]{4,}\b", prompt or "")
        if w.lower() not in _MO_STOPWORDS
    }
    if prompt_words:
        pattern = r"\b(?:" + "|".join(re.escape(w) for w in prompt_words) + r")\b"
        if not re.search(pattern, t):
            return True

    return False


async def draft_numbered_paras(db: Session, prompt: str, min_paras: int = 2, max_paras: int = 4) -> List[str]:
    schema_hint = f"""
Return STRICT JSON with ALL keys present:
{{
  "paras": [
    "1. [Rank] [Name] of [Unit] will proceed on temporary duty to [Destination] on [Date] (FN/AN). [Purpose/duty as stated in REQUEST.]",
    "2. The individual will report to [authority/unit] at [destination] on arrival and comply with all standing orders applicable there.",
    "3. The individual will return to [unit] and resume duties by [return date]. The journey will be performed by [mode / MR route]."
  ]
}}
Rules:
- Provide {min_paras} to {max_paras} numbered paragraphs.
- Each paragraph MUST include at least one concrete detail from REQUEST (rank, name, unit, place, date).
- Para 1: travel instruction — who is proceeding, destination, date, purpose (all from REQUEST).
- Para 2: reporting/duty instructions at destination (from REQUEST if provided).
- Final para: return date and route (MR if not specified).
- NEVER output placeholders like "1. ...", "First instruction", "<...>", "...".
- No signature block.
"""

    task = (
        "Draft the numbered instructions for a Movement Order.\n"
        "Structure:\n"
        "  Para 1 — State who is proceeding (rank, name, unit from REQUEST), destination,\n"
        "            departure date, and purpose. Include FN/AN if specified in REQUEST.\n"
        "  Para 2 — Reporting instructions at destination or specific duty details from REQUEST.\n"
        "  Final  — Return date and route. Use MR if route not specified in REQUEST.\n"
        "IMPORTANT: Each paragraph MUST use at least one concrete detail from REQUEST.\n"
        "Do NOT add any information not present in REQUEST.\n\n"
        f"REQUEST:\n{(prompt or '').strip()}\n\n"
        f"Provide between {min_paras} and {max_paras} numbered paragraphs."
    )

    parsed = await run_slot(
        db=db,
        doctype="MOVEMENT_ORDER",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="movement order proceed to reporting instructions rules",
        k_rules=6,
    )
    out = ParasOut.model_validate(parsed)
    paras = [p.strip() for p in out.paras if isinstance(p, str) and p.strip()]

    if (not paras) or any(_bad_para(p, prompt) for p in paras):
        task2 = (
            "DO NOT use placeholders. Use the specific details from REQUEST.\n"
            "Example structure (replace with REQUEST content):\n"
            '{"paras":['
            '"1. Sep Raj Kumar, No 10525911F, of 153 Inf Bn (TA) DOGRA, attached with TAIC, will proceed on temporary duty to DG INF, New Delhi on 25 Mar 2026 (FN) for administrative duties.",'
            '"2. The individual will report to the duty officer at DG INF Directorate on arrival and comply with all standing orders of the station.",'
            '"3. The individual will return to 153 Inf Bn and resume duties by 30 Mar 2026. The journey will be performed by MR."'
            "]}\n\n"
            + task
        )
        parsed = await run_slot(
            db=db,
            doctype="MOVEMENT_ORDER",
            task=task2,
            schema_hint=schema_hint,
            retrieval_query="movement order proceed to reporting instructions rules",
            k_rules=6,
        )
        out = ParasOut.model_validate(parsed)
        paras = [p.strip() for p in out.paras if isinstance(p, str) and p.strip()]

    # Normalize: replace single/multiple spaces after number with 4 tabs for editor visual spacing.
    _TAB5 = "\t\t\t\t"
    paras = [re.sub(r'^(\d+\.)\s+', rf'\1{_TAB5}', p) for p in paras]
    return paras[:max_paras]


async def draft_distribution_lines(db: Session, prompt: str, max_lines: int = 6) -> List[str]:
    schema_hint = """
Return STRICT JSON with ALL keys present:
{
  "lines": ["(1) Dir (Ops)", "(2) AAO"]
}
Rules:
- If REQUEST does not mention copy/distribution, return {"lines": []}.
- Use REAL entries from REQUEST if present.
- NEVER output placeholders like "<...>", "...".
"""

    task = (
        "If the request explicitly asks for copy-to/distribution, return the final list.\n"
        "Otherwise return an empty list.\n\n"
        f"REQUEST:\n{(prompt or '').strip()}\n"
    )

    parsed = await run_slot(
        db=db,
        doctype="MOVEMENT_ORDER",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="movement order copy to distribution list rules",
        k_rules=6,
    )
    out = DistOut.model_validate(parsed)
    lines = [l.strip() for l in out.lines if isinstance(l, str) and l.strip()]

    # If placeholders leaked, safest is empty
    if any("..." in l or "<" in l or ">" in l for l in lines):
        return []
    return lines[:max_lines]
