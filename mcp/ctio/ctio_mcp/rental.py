"""'Vehicle was rented' Declaration of Non-Liability (CTIO Answer reason "Rental").

Site mechanics (from the Angular bundle, see recon/NOTES.md; NOT exercised live because no
real full notice number was available):

  notice login -> /violations/Safetyviolations -> #disputeLinkId -> /violations/disputes
  (loads GET account/GetDisputeViolations/<guid>: Details[] with TrxnNum, TransactionDate,
  Lane, Description, StatusCode, TollAmount, Disputable)
  -> expand notice row -> tick child-row checkbox per civil penalty (opens a detail modal)
  -> #DisCivPenReasonSelect = "Rental" -> contact Email/Phone -> app-rental form
  -> file input #file (selecting a file immediately POSTs support/uploadAttachment/<guid>)
  -> #declaration-agree (e-communication) + #declaration-submit (penalty of perjury)
  -> #RentalSubmitId -> POST support/Ticket -> "Dispute Acknowledged" modal (ticket id).

prepare_* stops before ticking #declaration-submit and before #RentalSubmitId.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path

ALLOWED_EXT = (".pdf", ".jpg", ".jpeg", ".png")  # site: validatefileExtention (case-sensitive!)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # site helper text: "Maximum file size is 5mb"
MAXLEN = {"first": 30, "last": 30, "street": 60, "apt": 30, "city": 30, "zip": 10, "email": 40, "phone": 14, "details": 500}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Selectors for the Answer page + app-rental component (recorded from bundle 216.e009d76d86d338e6.js).
RSEL = {
    "reason_select": "#DisCivPenReasonSelect",
    "reason_value": "Rental",
    "contact_email": 'input[aria-label="Please Enter Email"]',
    "contact_phone": 'input[aria-label="Please Enter Phone"]',
    "start_date": 'input[aria-label="Please Enter Rental Agreement Start Date"]',
    "end_date": 'input[aria-label="Please Enter Rental Agreement End Date"]',
    "first_name": 'input[aria-label="Please Enter First Name"]',
    "last_name": 'input[aria-label="Please Enter Last Name"]',
    "street": 'input[aria-label="Please Enter Mailing Address"]',
    "apt": 'input[aria-label="Please Enter Apt / Suite"]',
    "city": 'input[aria-label="Please Enter City"]',
    "state": 'select[aria-label="Please select State"]',
    "zip": 'input[aria-label="Please Enter ZipCode"]',
    "file": "input#file",
    "file_list": ".alert.alert-secondary",
    "details": 'textarea[title="Description"], textarea[aria-describedby="Please enter Description"]',
    "agree_ecomm": "#declaration-agree",
    "perjury": "#declaration-submit",
    "submit": "#RentalSubmitId",
    "grid_checkbox": 'input[type="checkbox"][aria-label="Please Check"]:not([hidden])',
    "expand_btn": 'img[title="Click here to Show Civil Penalties"]',
    "detail_modal": "#myModal",
    "ack_text": "Dispute Acknowledged",
}


from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class RentalDeclarationError(ToolError, ValueError):
    """Validation refusal; message is shown to the model."""


@dataclass
class Address:
    street: str
    city: str
    state: str
    zip: str
    apt: str | None = None


@dataclass
class Penalty:
    number: str  # TrxnNum / civil penalty number, digits only
    when: datetime | None
    when_raw: str | None
    location: str | None = None
    description: str | None = None
    status: str | None = None
    amount: str | None = None
    disputable: bool | None = None
    notice_number: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "civil_penalty_number": self.number,
            "date_time": self.when.isoformat() if self.when else None,
            "date_time_raw": self.when_raw,
            "location": self.location,
            "description": self.description,
            "status": self.status,
            "amount": self.amount,
            "disputable": self.disputable,
            "notice_number": self.notice_number,
        }


# ---------------------------------------------------------------- pure helpers (unit tested)


def digits(s: str | int | None) -> str:
    return re.sub(r"\D", "", str(s or ""))


def parse_when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    s = re.sub(r"\s*(?:MT|MST|MDT)$", "", s)
    s = re.sub(r"\.\d+", "", s)  # fractional seconds
    s = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", s).strip()
    fmts = (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
        "%m/%d/%y %I:%M %p", "%b %d, %Y %I:%M %p", "%b %d, %Y",
    )
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def parse_rental_bound(value: str, *, end: bool) -> datetime:
    """'2026-10-01' -> 00:00 (start) / 23:59:59 (end). Datetimes are kept as given."""
    v = (value or "").strip()
    dt = parse_when(v)
    if dt is None:
        raise RentalDeclarationError(f"Cannot parse rental {'end' if end else 'start'} '{value}'. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}", v):
        dt = datetime.combine(dt.date(), dtime(23, 59, 59) if end else dtime(0, 0))
    return dt


def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if len(parts) < 2:
        raise RentalDeclarationError("renter_name needs a first and last name (e.g. 'Jordan Smith').")
    return " ".join(parts[:-1]), parts[-1]


def check_attachment(path: str | os.PathLike) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise RentalDeclarationError(f"agreement_path not found: {p}")
    # The site compares the extension case-sensitively against lowercase values.
    if p.suffix not in ALLOWED_EXT:
        raise RentalDeclarationError(
            f"File type '{p.suffix}' not accepted by CTIO (allowed, lowercase: {', '.join(ALLOWED_EXT)}). "
            "Rename/convert it, or build a packet PDF with ctio_build_declaration_packet."
        )
    size = p.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise RentalDeclarationError(f"{p.name} is {size / 1048576:.2f} MB; CTIO's limit is 5 MB.")
    return p


def build_form_values(
    *, renter_name: str, renter_address: Address, rental_start: str, rental_end: str,
    contact_email: str, contact_phone: str, details: str | None, plate: str,
) -> dict:
    first, last = split_name(renter_name)
    state_code = renter_address.state.strip().upper()
    state_name = STATE_NAMES.get(state_code) or next(
        (n for n in STATE_NAMES.values() if n.lower() == renter_address.state.strip().lower()), None)
    if not state_name:
        raise RentalDeclarationError(f"Unknown renter state '{renter_address.state}'.")
    start = parse_rental_bound(rental_start, end=False)
    end = parse_rental_bound(rental_end, end=True)
    if end < start:
        raise RentalDeclarationError("rental_end is before rental_start.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", contact_email or ""):
        raise RentalDeclarationError("contact_email (the owner's email for CTIO's response) is missing or invalid.")
    phone_digits = digits(contact_phone)
    if len(phone_digits) == 11 and phone_digits.startswith("1"):
        phone_digits = phone_digits[1:]
    if len(phone_digits) != 10:
        raise RentalDeclarationError("contact_phone must be a 10-digit US number.")
    if details is None:
        details = (
            f"Beverley Fleet is a vehicle rental business. Plate {plate} was rented to {renter_name} "
            f"from {start.date().isoformat()} to {end.date().isoformat()} under the attached rental agreement."
        )
    values = {
        "start_date": start.date().isoformat(),  # <input type=date> value format
        "end_date": end.date().isoformat(),
        "first_name": first,
        "last_name": last,
        "street": renter_address.street.strip(),
        "apt": (renter_address.apt or "").strip(),
        "city": renter_address.city.strip(),
        "state": state_name,
        "zip": renter_address.zip.strip(),
        "contact_email": contact_email.strip(),
        "contact_phone": phone_digits,
        "details": details.strip(),
    }
    limits = {"first_name": "first", "last_name": "last", "street": "street", "apt": "apt", "city": "city",
              "zip": "zip", "contact_email": "email", "details": "details"}
    for k, lim in limits.items():
        if len(values[k]) > MAXLEN[lim]:
            raise RentalDeclarationError(f"{k} is {len(values[k])} chars; CTIO field max is {MAXLEN[lim]}.")
    for k in ("street", "city", "zip"):
        if not values[k]:
            raise RentalDeclarationError(f"renter_address.{k} is required.")
    return {"values": values, "rental_start": start, "rental_end": end}


def penalties_from_dispute_json(data: dict | None, violations_json: dict | None = None) -> list[Penalty]:
    """Merge GetDisputeViolations Details (TrxnNum/TransactionDate/Lane) with GetViolations Details
    (CivielPenalityNumber/DataTime/Location), whichever is present."""
    out: dict[str, Penalty] = {}
    tvn_to_notice = {}
    for src in (violations_json or {}, data or {}):
        for v in src.get("Violations") or []:
            if v.get("TVNID") is not None:
                tvn_to_notice[v["TVNID"]] = v.get("NoticeNumber")
    for src in (violations_json or {}, data or {}):
        for d in src.get("Details") or []:
            num = digits(d.get("TrxnNum") or d.get("CivielPenalityNumber"))
            if not num:
                continue
            raw = d.get("TransactionDate") or d.get("DataTime")
            p = out.get(num) or Penalty(number=num, when=None, when_raw=None)
            p.when_raw = p.when_raw or raw
            p.when = p.when or parse_when(raw)
            p.location = p.location or d.get("Lane") or d.get("Location")
            p.description = p.description or d.get("Description") or d.get("safetyViolation")
            p.status = p.status or d.get("StatusCode") or d.get("Status")
            p.amount = p.amount or (str(d.get("TollAmount") or d.get("Amount") or "") or None)
            if d.get("Disputable") is not None:
                p.disputable = d.get("Disputable") == 1
            p.notice_number = p.notice_number or tvn_to_notice.get(d.get("TVNID"))
            out[num] = p
    return list(out.values())


def select_and_validate(
    penalties: list[Penalty], requested: list[str] | None, start: datetime, end: datetime,
) -> list[Penalty]:
    """Pick the penalties to declare on and refuse unless every one is disputable and dated inside the rental."""
    if not penalties:
        raise RentalDeclarationError("No civil penalties found for this notice; nothing to declare.")
    by_num = {p.number: p for p in penalties}
    if requested:
        want = [digits(n) for n in requested]
        missing = [n for n in want if n not in by_num]
        if missing:
            raise RentalDeclarationError(f"Civil penalty number(s) not on this notice: {', '.join(missing)}. "
                                         f"On notice: {', '.join(by_num)}")
        chosen = [by_num[n] for n in want]
    else:
        chosen = [p for p in penalties if p.disputable is not False]
        if not chosen:
            raise RentalDeclarationError("No disputable civil penalties on this notice.")
    problems = []
    for p in chosen:
        if p.disputable is False:
            problems.append(f"{p.number}: site marks it not disputable (status {p.status})")
        elif p.when is None:
            problems.append(f"{p.number}: violation date/time unreadable ('{p.when_raw}'), cannot verify rental period")
        elif not (start <= p.when <= end):
            problems.append(f"{p.number}: violation at {p.when.isoformat(sep=' ')} ({p.location or 'location n/a'}) "
                             f"is OUTSIDE rental {start.isoformat(sep=' ')} .. {end.isoformat(sep=' ')}")
    if problems:
        raise RentalDeclarationError("Refusing rental declaration: " + "; ".join(problems))
    return chosen


def fmt_penalty_for_grid(num: str) -> str:
    """Site shows TrxnNum as 'ddd ddd ddd' (formateCivilPenality: first 9 digits in groups of 3)."""
    n = digits(num)
    return f"{n[0:3]} {n[3:6]} {n[6:9]}".strip()


# ---------------------------------------------------------------- DOM helpers (fixture tested)


async def read_grid_penalties(page) -> list[Penalty]:
    """DOM fallback: parse the dispute grid child rows (number, date time, location, description, status, amount)."""
    rows = await page.evaluate(
        """() => [...document.querySelectorAll('table.expanded-table tbody tr')].map(tr => {
              const cb = [...tr.querySelectorAll('input[type=checkbox][aria-label="Please Check"]')].find(c => !c.hidden);
              return {cells: [...tr.querySelectorAll('td')].map(td => td.innerText.trim()),
                      disabled: cb ? cb.disabled : null};
            })"""
    )
    out = []
    for row in rows:
        cells = row["cells"]
        if len(cells) < 7:
            continue
        num = digits(cells[1])
        if not num:
            continue
        out.append(Penalty(number=num, when=parse_when(cells[2]), when_raw=cells[2], location=cells[3] or None,
                           description=cells[4] or None, status=cells[5] or None, amount=cells[6] or None,
                           disputable=None if row["disabled"] is None else not row["disabled"]))
    return out


async def _close_detail_modal(page):
    modal = page.locator(RSEL["detail_modal"])
    if await modal.count() and await modal.first.is_visible():
        btn = modal.get_by_role("button", name=re.compile(r"continue|close", re.I))
        if await btn.count():
            await btn.first.click()
        await page.wait_for_timeout(300)


async def expand_all_notices(page):
    for img in await page.locator(RSEL["expand_btn"]).all():
        try:
            await img.click()
            await page.wait_for_timeout(300)
        except Exception:
            pass


async def tick_penalties(page, numbers: list[str]) -> list[str]:
    ticked = []
    for num in numbers:
        label = fmt_penalty_for_grid(num)
        row = page.locator("table.expanded-table tbody tr", has_text=label)
        if not await row.count():
            raise RentalDeclarationError(f"Civil penalty {label} not found in the Answer grid.")
        cb = row.first.locator(RSEL["grid_checkbox"])
        if not await cb.count():
            raise RentalDeclarationError(f"No selectable checkbox for civil penalty {label}.")
        if await cb.first.is_disabled():
            raise RentalDeclarationError(f"Civil penalty {label} checkbox is disabled (not disputable).")
        if not await cb.first.is_checked():
            await cb.first.check()
            await page.wait_for_timeout(300)
            await _close_detail_modal(page)
        ticked.append(num)
    return ticked


async def fill_rental_form(page, values: dict, attachment: Path | None) -> dict:
    await page.select_option(RSEL["reason_select"], value=RSEL["reason_value"])
    await page.wait_for_selector(RSEL["start_date"])
    await page.fill(RSEL["contact_email"], values["contact_email"])
    phone = page.locator(RSEL["contact_phone"])
    await phone.fill("")
    await phone.press_sequentially(values["contact_phone"], delay=30)  # site formats on keyup
    await page.fill(RSEL["start_date"], values["start_date"])
    await page.fill(RSEL["end_date"], values["end_date"])
    for k in ("first_name", "last_name", "street", "apt", "city", "zip"):
        if values.get(k):
            await page.fill(RSEL[k], values[k])
    await page.select_option(RSEL["state"], value=values["state"])
    await page.fill(RSEL["details"], values["details"])
    uploaded = None
    if attachment is not None:
        await page.set_input_files(RSEL["file"], str(attachment))
        await page.locator(RSEL["file_list"], has_text=attachment.name).wait_for(timeout=30000)
        uploaded = attachment.name
    agree = page.locator(RSEL["agree_ecomm"])
    if not await agree.is_checked():
        await agree.check()
    # Blur last field so Angular runs its validators.
    await page.locator(RSEL["zip"]).press("Tab")
    return {"uploaded_file": uploaded}


async def read_back(page) -> dict:
    vals = {}
    for k in ("contact_email", "start_date", "end_date", "first_name", "last_name", "street", "apt", "city", "zip"):
        vals[k] = await page.input_value(RSEL[k])
    vals["contact_phone"] = digits(await page.input_value(RSEL["contact_phone"]))
    vals["state"] = await page.input_value(RSEL["state"])
    vals["details"] = await page.input_value(RSEL["details"])
    vals["reason"] = await page.input_value(RSEL["reason_select"])
    vals["agree_ecomm_checked"] = await page.is_checked(RSEL["agree_ecomm"])
    vals["perjury_checked"] = await page.is_checked(RSEL["perjury"])
    return vals


def diff_values(expected: dict, actual: dict) -> dict:
    return {k: {"expected": v, "actual": actual.get(k)} for k, v in expected.items() if (actual.get(k) or "") != (v or "")}
