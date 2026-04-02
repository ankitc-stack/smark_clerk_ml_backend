"""
app/ml/slots/leave_certificate.py

Extracts leave-certificate fields for both granting certificates (spare chit)
and leave-request letters.
"""

from __future__ import annotations
import re
from datetime import datetime, timedelta
from typing import Any, Dict

from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.ml.slots.common import run_slot

class LeaveFieldsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fields: Dict[str, Any] = Field(default_factory=dict)


# All extractable fields.
# army_no  : army number  e.g. 10522580P or TJ-7122M
# att_unit : attached unit e.g. "Territorial Army Innovation Cell (TAIC)"
# prefix_date / suffix_date : optional prefix/suffix holiday dates
# leave_vill/teh/dist/state/pin : structured leave address components
KEYS = [
    "army_no",
    "person_name", "rank", "unit", "att_unit",
    "leave_type", "from_date", "to_date", "days",
    "prefix_date", "suffix_date",
    "leave_vill", "leave_teh", "leave_dist", "leave_state", "leave_pin",
    "contact_no", "station",
    "date", "signatory_name", "signatory_designation",
]


def _empty_fields() -> Dict[str, str]:
    return {k: "" for k in KEYS}


def _likely_all_empty(fields: Dict[str, Any]) -> bool:
    core = ("person_name", "rank", "unit", "leave_type", "from_date", "to_date", "days")
    return all(not str(fields.get(k, "")).strip() for k in core)


_MONTH_ABBR: dict = {
    "january": "Jan", "february": "Feb", "march": "Mar",
    "april": "Apr", "may": "May", "june": "Jun",
    "july": "Jul", "august": "Aug", "september": "Sep",
    "october": "Oct", "november": "Nov", "december": "Dec",
    "jan": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr",
    "jun": "Jun", "jul": "Jul", "aug": "Aug",
    "sep": "Sep", "oct": "Oct", "nov": "Nov", "dec": "Dec",
}


def _normalize_date(s: str) -> str:
    """Normalize date strings to 'DD Mon YYYY' format.
    Handles: '08 Sep 2025', '1st March 2026', '01 march 2026'.
    """
    s = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", s, flags=re.IGNORECASE).strip()
    parts = s.split()
    if len(parts) == 3:
        day, month, year = parts
        abbr = _MONTH_ABBR.get(month.lower())
        if abbr and year.isdigit() and day.isdigit():
            return f"{int(day):02d} {abbr} {year}"
    return s


def _regex_fallback(prompt: str) -> Dict[str, str]:
    """
    Fast deterministic extraction — no LLM call.
    Handles both:
      - Leave certificate / spare chit (formal):
          "No 10522580P Rank Hav Name Raj Kumar of 153 Inf Bn (TA) DOGRA
           att with TAIC is hereby spare ... 13 days PAL wef 08 Sep 2025 to 20 Sep 2025
           with permission to prefix on 07 Sep 2025 and suffix on 21 Sep 2025"
      - Leave request letter:
          "I request 30 days AL from 01 Mar 2026 to 30 Mar 2026 for Sepoy Ramesh Kumar"
      - Informal/short prompt:
          "generate leave certificate for tj-7122m nb sub vk swamy of 172 inf bn madras
           with taic for 56 AL wef 1st march 2026"
    """
    p = prompt or ""
    out = _empty_fields()

    # Flexible date pattern: handles DD Mon YYYY, D Month YYYY, 1st March 2026
    _date = r"[0-9]{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+[0-9]{4}"

    # ── Army Number: "No 10522580P" / "No TJ-7122M" ────────────────────────
    m = re.search(r"\bNo\.?\s+([A-Z]{0,3}\d{5,10}[A-Z]?)\b", p, flags=re.IGNORECASE)
    if m:
        out["army_no"] = m.group(1).strip().upper()
    if not out["army_no"]:
        # Letter-dash-digit format without "No" prefix: TJ-7122M
        m = re.search(r"\b([A-Z]{1,3}-\d{4,8}[A-Z]?)\b", p, flags=re.IGNORECASE)
        if m:
            out["army_no"] = m.group(1).strip().upper()
    if not out["army_no"]:
        # Pure digit army number without "No" prefix: 10522580P
        m = re.search(r"\b(\d{7,10}[A-Z])\b", p, flags=re.IGNORECASE)
        if m:
            out["army_no"] = m.group(1).strip().upper()

    # ── Person Name: "Name Raj Kumar" or "for Ramesh Kumar from/days" ──────
    m = re.search(r"\bName[:\s]+([A-Z][A-Za-z .]+?)(?:\s+of\b|\s+is\b|,|$)", p)
    if m:
        out["person_name"] = m.group(1).strip()
    else:
        m = re.search(
            r"\bfor\s+([A-Za-z][A-Za-z .]+?)(?:,|\b)\s*(\d+\s+days|\bfrom\b)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["person_name"] = m.group(1).strip()

    # ── Rank ──────────────────────────────────────────────────────────────
    # Try "Rank Hav" / "Rank Nb Sub" label first (longest match wins)
    m_rank = re.search(r"\bRank\s+([A-Za-z/\s]+?)(?:\s+Name\b|\s+of\b|,|$)", p)
    if m_rank:
        out["rank"] = m_rank.group(1).strip()
    else:
        rank_list = [
            "Nb Sub", "Sub Maj", "Sub", "Hav", "Nk", "L/Nk",
            "Sep", "Rfn", "Gnr", "Sigmn", "Spr",
            "Capt", "Maj", "Lt Col", "Col", "Brig", "Gen",
            "Lt", "Flt Lt", "Sgt", "Cpl", "WO", "JCO",
        ]
        for rnk in rank_list:
            if re.search(rf"\b{re.escape(rnk)}\b", p, flags=re.IGNORECASE):
                out["rank"] = rnk
                break

    # ── Person name: "for Sgt Ramesh Kumar No/of ..." (informal prompt) ────
    if not out["person_name"] and out["rank"]:
        rnk_pat = re.escape(out["rank"])
        m = re.search(
            rf"\bfor\s+{rnk_pat}\s+([A-Za-z][A-Za-z .]+?)(?:\s+No\b|\s+of\b|\s+att\b|,|\.|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["person_name"] = m.group(1).strip()

    # ── Person name from rank context (informal: "Nb Sub VK Swamy of ...") ─
    if not out["person_name"] and out["rank"]:
        rnk_pat = re.escape(out["rank"])
        m = re.search(
            rf"\b{rnk_pat}\s+([A-Za-z][A-Za-z .]+?)(?:\s+of\b|\s+att\b|\s+is\b|,|\.|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            out["person_name"] = m.group(1).strip()

    # ── Strip rank prefix if captured as part of person_name ─────────────
    if out["person_name"] and out["rank"]:
        pname_low = out["person_name"].lower()
        if pname_low.startswith(out["rank"].lower()):
            out["person_name"] = out["person_name"][len(out["rank"]):].strip()

    # ── Unit: "of 153 Inf Bn (TA) DOGRA" / "of 1 CORPS" ─────────────────
    # Stops at: att/with/is/for/hereby, a standalone digit (start of days count),
    # or end of string.
    m = re.search(
        r"\bof\s+(\d+\s+[A-Za-z][A-Za-z\s()/]*?)(?=\s+(?:att|with|is|for|hereby)\b|\s+\d+\s+days|\s+\d+\s+(?:AL|EL|CL|PAL|ML|SL)\b|\s+(?:AL|EL|CL|PAL|ML|SL)\b|,|\.|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["unit"] = m.group(1).strip()

    # ── Attached unit: "att with TAIC" or informal "with TAIC for/is" ─────
    m = re.search(
        r"\batt(?:\s+with)?\s+([A-Za-z][A-Za-z\s()]+?)(?:\s+is\b|\s+hereby\b|,|\.|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["att_unit"] = m.group(1).strip()
    if not out["att_unit"]:
        # Informal: "with TAIC for N days" — "with" after unit but no "att" keyword
        m = re.search(
            r"\bwith\s+([A-Z][A-Za-z\s()]{1,30}?)(?:\s+for\b|\s+is\b|\s+hereby\b|,|\.|$)",
            p, flags=re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip()
            if not re.match(
                r"(?:permission|effect|wef|from|ref)\b", candidate, flags=re.IGNORECASE
            ):
                out["att_unit"] = candidate

    # ── Leave type ─────────────────────────────────────────────────────────
    m = re.search(r"\b(PAL|EL|CL|AL|ML|SL|CTC|LTC)\b", p, flags=re.IGNORECASE)
    if m:
        out["leave_type"] = m.group(1).upper()
    if not out["leave_type"]:
        _leave_words = {
            "annual": "AL", "earned": "EL", "privilege": "PAL",
            "casual": "CL", "medical": "ML", "sick": "SL",
        }
        m = re.search(r"\b(annual|earned|privilege|casual|medical|sick)\b", p, flags=re.IGNORECASE)
        if m:
            out["leave_type"] = _leave_words[m.group(1).lower()]

    # ── Days ───────────────────────────────────────────────────────────────
    m = re.search(r"(\d+)\s*days", p, flags=re.IGNORECASE)
    if m:
        out["days"] = m.group(1)
    if not out["days"] and out["leave_type"]:
        # Informal: "56 AL wef" — number directly before leave type, no "days" keyword
        lt = re.escape(out["leave_type"])
        m = re.search(rf"(\d+)\s+{lt}\b", p, flags=re.IGNORECASE)
        if m:
            out["days"] = m.group(1)

    # ── Date range: "from/wef DD Mon YYYY to DD Mon YYYY" ─────────────────
    m = re.search(
        rf"\b(?:from|wef)\s+({_date})\s+to\s+({_date})\b",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["from_date"] = _normalize_date(m.group(1))
        out["to_date"] = _normalize_date(m.group(2))
    if not out["from_date"]:
        # Single date after "wef"/"from" when no "to" date is given
        m = re.search(rf"\b(?:from|wef)\s+({_date})\b", p, flags=re.IGNORECASE)
        if m:
            out["from_date"] = _normalize_date(m.group(1))

    # ── Prefix / suffix dates ──────────────────────────────────────────────
    m = re.search(rf"\bprefix\s+on\s+({_date})\b", p, flags=re.IGNORECASE)
    if m:
        out["prefix_date"] = _normalize_date(m.group(1))
    m = re.search(rf"\bsuffix\s+on\s+({_date})\b", p, flags=re.IGNORECASE)
    if m:
        out["suffix_date"] = _normalize_date(m.group(1))

    # ── Structured leave address fields ────────────────────────────────────
    # Accept both abbreviated+separator form ("Vill - Rampur") and
    # full-word+space form ("village Rampur", "tehsil Moradabad", etc.).
    # Lookahead stops capture before the next address keyword.
    _addr_stop = r"(?=\s*(?:teh(?:sil)?|dist(?:rict)?|state|pin|contact|$))"
    m = re.search(
        r"\b(?:vill(?:age)?(?:\s*&\s*po)?)\s*[-:\s]\s*(.+?)" + _addr_stop,
        p, flags=re.IGNORECASE,
    )
    if m:
        out["leave_vill"] = m.group(1).strip().rstrip(",")

    _teh_stop = r"(?=\s*(?:dist(?:rict)?|state|pin|contact|$))"
    m = re.search(
        r"\b(?:teh(?:sil)?)\s*[-:\s]\s*(.+?)" + _teh_stop,
        p, flags=re.IGNORECASE,
    )
    if m:
        out["leave_teh"] = m.group(1).strip().rstrip(",")

    _dist_stop = r"(?=\s*(?:state|pin|contact|$))"
    m = re.search(
        r"\b(?:dist(?:rict)?)\s*[-:\s]\s*(.+?)" + _dist_stop,
        p, flags=re.IGNORECASE,
    )
    if m:
        out["leave_dist"] = m.group(1).strip().rstrip(",")

    _state_stop = r"(?=\s*(?:pin|contact|$))"
    m = re.search(
        r"\bstate\s*[-:\s]\s*(.+?)" + _state_stop,
        p, flags=re.IGNORECASE,
    )
    if m:
        out["leave_state"] = m.group(1).strip().rstrip(",")

    m = re.search(r"\bpin\s*[-:\s]\s*(\d{6})\b", p, flags=re.IGNORECASE)
    if m:
        out["leave_pin"] = m.group(1)

    # ── Contact (10-digit mobile) ──────────────────────────────────────────
    m = re.search(r"\b(\d{10})\b", p)
    if m:
        out["contact_no"] = m.group(1)

    # ── Station: "Station: c/o 56 APO" ────────────────────────────────────
    m = re.search(r"\bStation\s*[:\-]\s*([^\n.]+)", p, flags=re.IGNORECASE)
    if m:
        out["station"] = m.group(1).strip()
    else:
        m = re.search(r"\bc/o\s+\d+\s+APO\b", p, flags=re.IGNORECASE)
        if m:
            out["station"] = m.group(0).strip()

    # ── Certificate date: "Dated DD Mon YYYY" ─────────────────────────────
    m = re.search(rf"\bDated\s*[:\-]?\s*({_date})\b", p, flags=re.IGNORECASE)
    if m:
        out["date"] = _normalize_date(m.group(1))

    # ── Signatory: "Signed by Name, Rank, Appointment" ────────────────────
    m = re.search(
        r"\bSigned\s+by\s+([A-Za-z][A-Za-z .]+?)(?:,\s*([A-Za-z /]+?))?(?:,\s*([A-Za-z /]+?))?(?:\.|$)",
        p, flags=re.IGNORECASE,
    )
    if m:
        out["signatory_name"] = m.group(1).strip()
        if m.group(2):
            out["signatory_designation"] = m.group(2).strip()
            if m.group(3):
                out["signatory_designation"] += f" {m.group(3).strip()}"

    # ── Infer to_date from from_date + days (inclusive) if to_date missing ─
    if out["from_date"] and not out["to_date"] and out["days"]:
        try:
            dt = datetime.strptime(out["from_date"], "%d %b %Y")
            end_dt = dt + timedelta(days=int(out["days"]) - 1)
            out["to_date"] = end_dt.strftime("%d %b %Y")
        except (ValueError, TypeError):
            pass

    return out


async def extract_fields(db: Session, prompt: str) -> Dict[str, Any]:
    schema_hint = """
Return STRICT JSON with ALL keys present:
{
  "fields": {
    "army_no": "",
    "person_name": "",
    "rank": "",
    "unit": "",
    "att_unit": "",
    "leave_type": "",
    "from_date": "",
    "to_date": "",
    "days": "",
    "prefix_date": "",
    "suffix_date": "",
    "leave_vill": "",
    "leave_teh": "",
    "leave_dist": "",
    "leave_state": "",
    "leave_pin": "",
    "contact_no": "",
    "station": ""
  }
}
Rules:
- Extract values ONLY if present in REQUEST; otherwise keep empty string.
- DO NOT invent values.
- For dates, keep exactly as written in REQUEST.
"""

    task = (
        "Extract leave certificate fields from REQUEST.\n"
        "IMPORTANT: If REQUEST contains a value, you MUST copy it into the correct field.\n"
        "Do NOT leave fields empty if values are present.\n\n"
        f"REQUEST:\n{(prompt or '').strip()}\n"
    )

    parsed = await run_slot(
        db=db,
        doctype="LEAVE_CERTIFICATE",
        task=task,
        schema_hint=schema_hint,
        retrieval_query="leave certificate extract fields rules",
        k_rules=6,
    )

    out = LeaveFieldsOut.model_validate(parsed)
    fields = out.fields if isinstance(out.fields, dict) else {}
    for k in KEYS:
        fields.setdefault(k, "")

    if _likely_all_empty(fields):
        fields = _regex_fallback(prompt)

    return fields
