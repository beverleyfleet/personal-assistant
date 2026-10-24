"""Parse CTIO / ExpressLaneSafety emails (support@expresslanesafetyco.com).

Pure functions only, no I/O, so they are easy to unit test.

Known email kinds (from the site's own wording):
  * "Notice of Civil Penalty"         -> status "notice"
  * "Answer Received" / "Dispute Acknowledged" -> "answer_received"
  * "Dispute (Answer) Reviewed"       -> "answer_reviewed"  (may contain a hearing-request deadline)
  * "Dismissed"                       -> "dismissed"
  * hearing scheduled / requested     -> "hearing"
  * payment receipt                   -> "paid"
"""

from __future__ import annotations

import re
from datetime import datetime

from ctio_mcp.vehicles import vehicle_for_plate

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December"
_DATE = rf"(?:(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})"
_MONEY = r"\$\s?[\d,]+(?:\.\d{2})?"

# Order matters: the first match wins.
_STATUS_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("dismissed", re.compile(r"\bdismiss(?:ed|al)\b", re.I)),
    ("paid", re.compile(r"\b(?:payment (?:has been )?received|thank you for your payment|payment receipt|paid in full)\b", re.I)),
    ("hearing", re.compile(r"\bhearing (?:has been )?(?:scheduled|requested|request received)\b|\bnotice of hearing\b", re.I)),
    ("answer_reviewed", re.compile(r"\b(?:answer|dispute)\s*(?:\((?:answer|dispute)\)\s*)?(?:has been\s+)?reviewed\b", re.I)),
    ("answer_received", re.compile(r"\b(?:answer|dispute)\s*(?:\((?:answer|dispute)\)\s*)?(?:has been\s+)?(?:received|acknowledged)\b", re.I)),
    ("notice", re.compile(r"\bnotice of civil penalty\b|\bcivil penalty notice\b", re.I)),
]


def _field(text: str, *labels: str) -> str | None:
    for label in labels:
        m = re.search(rf"{label}\s*(?:#|NO\.?|NUMBER)?\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9\- ]*?)\s*(?:$|\n|\r|,|;|\|)", text, re.I | re.M)
        if m:
            return m.group(1).strip()
    return None


def _to_iso(date_str: str | None) -> str | None:
    if not date_str:
        return None
    s = re.sub(r"\s+", " ", date_str.replace(",", ", ")).replace(" ,", ",").strip()
    s = s.replace("Sept ", "Sep ").replace(".", "")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(re.sub(r",\s+", ", ", s), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _money(s: str | None) -> float | None:
    if not s:
        return None
    return float(s.replace("$", "").replace(",", "").strip())


def parse_ctio_email(text: str, subject: str | None = None) -> dict:
    """Extract structured data from a CTIO email body (and optional subject).

    Returns a dict; fields that are not present are None. Never raises on odd input.
    """
    body = text or ""
    hay = f"{subject or ''}\n{body}"

    plate = _field(body, r"LICENSE\s+PLATE(?:\s+NUMBER)?", r"PLATE")
    answer_number = _field(body, r"ANSWER\s+NUMBER", r"ANSWER", r"DISPUTE\s+NUMBER", r"DISPUTE")
    notice_number = None
    m = re.search(r"NOTICE\s+(?:NUMBER|NO\.?|#)\s*[:#]?\s*([0-9X][0-9X\-]{6,})", body, re.I)
    if m:
        notice_number = m.group(1).strip("-")
    plate_state = None
    m = re.search(r"(?:PLATE\s+)?STATE\s*:\s*([A-Z]{2})\b", body)
    if m:
        plate_state = m.group(1)

    amount = due = None
    m = re.search(rf"(?:balance|amount)\s+(?:due|owed)\D{{0,20}}?({_MONEY})(?:[^.\n]{{0,40}}?\bby\s+({_DATE}))?", body, re.I)
    if m:
        amount = _money(m.group(1))
        due = m.group(2)
    if amount is None:
        m = re.search(rf"(?:total|amount)\s*(?:due)?\s*:?\s*({_MONEY})", body, re.I)
        if m:
            amount = _money(m.group(1))
    if due is None:
        m = re.search(rf"(?:due(?:\s+date)?|pay(?:ment)?\s+by|on or before)\s*:?\s*({_DATE})", body, re.I)
        if m:
            due = m.group(1)

    hearing_deadline = None
    m = re.search(rf"request\s+(?:a\s+)?hearing[^.]{{0,80}}?\bby\s+({_DATE})", body, re.I)
    if m:
        hearing_deadline = _to_iso(m.group(1))

    status = "unknown"
    for name, rx in _STATUS_RULES:
        if rx.search(hay):
            status = name
            break

    amounts = [_money(a) for a in re.findall(_MONEY, body)]
    vehicle = vehicle_for_plate(plate) if plate else None

    return {
        "status": status,
        "plate": plate.upper() if plate else None,
        "plate_state": plate_state,
        "answer_number": answer_number,
        "notice_number": notice_number,
        "amount_due": amount,
        "due_date": _to_iso(due),
        "due_date_raw": due,
        "hearing_request_deadline": hearing_deadline,
        "all_amounts": amounts,
        "vehicle": vehicle["label"] if vehicle else None,
    }
