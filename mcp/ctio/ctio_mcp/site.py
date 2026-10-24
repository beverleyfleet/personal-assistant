"""Playwright automation of https://expresslanesafety.com/CO (Angular SPA).

Recon summary (see recon/NOTES.md for detail):

* No user-account login exists. Two public entry flows on /CO/violations:
  - "Yes" (have notice #): Notice Number + Plate State + Plate
      -> POST api.expresslanesafety.com/login/notification
      -> /CO/violations/Safetyviolations, which loads GET account/GetViolations/<AccountGuid>
         (Violations[], Details[], ViolationsDetails[0] summary, Table2 adjudication fees).
         Buttons: Pay (PaymentSubmit), Answer/dispute (#disputeLinkId -> /violations/disputes),
         Request a Hearing (#requestAHearingBtnId, then a $30-fee acknowledgement modal
         #reqHearingAckModal / #ReqHearingAcknowledgementBtn which POSTs account/HearingAck).
  - "No" (guest pay): Plate State + Plate only
      -> POST login/QuickPay -> /CO/violations/payment "Summary" page listing every notice on
         the plate with MASKED notice numbers and amounts, total, an email box and a Pay button.
         Guest mode cannot Answer/dispute or request a hearing.

Lookups and prepare_payment/prepare_hearing_request are read-only: they never click Pay, Submit or
Acknowledge, and never type payment details. The single exception is rental_declaration(submit=True),
which clicks #RentalSubmitId and must only be reached through server.submit_gate_rental
(CTIO_ALLOW_SUBMIT=1 + confirm=True). rental_declaration(submit=False) uploads the agreement file
(the site uploads on file selection) but creates no Answer.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ctio_mcp.vehicles import normalize_plate, vehicle_for_plate

BASE_URL = os.environ.get("CTIO_BASE_URL", "https://expresslanesafety.com/CO").rstrip("/")
API_HOST = "api.expresslanesafety.com"
NAV_TIMEOUT_MS = int(os.environ.get("CTIO_TIMEOUT_MS", "45000"))


class CtioError(ToolError):
    """Anticipated site failure; the message is shown to the model (never contains secrets)."""


# --- selectors (recorded 2026-10-24) -----------------------------------------------------
SEL = {
    "yes_btn": 'button[title="Click here if you have your notice number"]',
    "no_btn": 'button[title="Click here if you do not have your notice number"]',
    "notice_input": 'input[aria-label="Notice Number"]',
    "state_select": 'select[aria-label="Select License Plate State"]',
    "plate_input": 'input[aria-label="License Plate Number"]',
    "search_btn": "#SearchViolationsId",
    "dispute_link": "#disputeLinkId",
    "request_hearing_btn": "#requestAHearingBtnId",
    "hearing_ack_modal": "#reqHearingAckModal",
    "pay_email": "#payEmail",
    "payment_summary": "#paymentSummary",
}
ERROR_TEXTS = (
    "Notice Number and License Plate do not match",
    "is not associated with a CTIO Civil Penality",  # sic, site typo
    "is not associated with a CTIO Civil Penalty",
    "Please contact support for payment.",
)

# Fields we are willing to return from site JSON. Everything else (VIN, AccountGuid,
# DMV ids, session ids, SSN/DOB placeholders, payment keys) is dropped.
_VIOLATION_KEYS = {
    "NoticeNumber", "Date", "Description1", "Description2", "Description3", "DisplayAmount",
    "AmountDue", "Amount", "Status", "Payable", "Disputable", "AllowADJ", "TVNID",
}
_DETAIL_KEYS = {
    "CivielPenalityNumber", "DataTime", "TransactionDate", "Location", "safetyViolation", "Amount",
    "TollAmount", "AmountDue", "Status", "StatusCode", "Payable", "Disputable", "AllowADJ", "TVNID",
    "CollectionFee", "HearingFee", "FeeDate",
}
_SUMMARY_KEYS = {"SubTotal", "TotalDue", "OutstandingNoticeCount", "HearingFee"}


def artifact_dir() -> Path:
    d = Path(os.environ.get("CTIO_ARTIFACT_DIR", Path.home() / ".cache" / "ctio-mcp" / "screenshots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def format_notice_number(notice: str) -> str:
    digits = re.sub(r"\D", "", notice or "")
    if len(digits) == 12:
        return "-".join(digits[i : i + 3] for i in range(0, 12, 3))
    return (notice or "").strip()


def site_plate(plate: str) -> str:
    """Plate as the site wants it: uppercase, no spaces/dashes, TEMP prefix dropped (unverified for temp tags)."""
    return normalize_plate(plate)


def _pick(d: dict, keys: set[str]) -> dict:
    return {k: v for k, v in d.items() if k in keys and v not in (None, "")}


@asynccontextmanager
async def _browser():
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover
        raise CtioError("playwright is not installed: pip install playwright && playwright install chromium") from e
    headless = os.environ.get("CTIO_HEADLESS", "1") != "0"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 1000})
        page = await ctx.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        captured: dict[str, Any] = {}

        async def on_response(resp):
            if API_HOST in resp.url and resp.request.resource_type in ("xhr", "fetch"):
                path = resp.url.split(API_HOST, 1)[1]
                try:
                    captured[path] = {"status": resp.status, "json": await resp.json()}
                except Exception:
                    captured[path] = {"status": resp.status, "json": None}

        page.on("response", on_response)
        try:
            yield page, captured
        finally:
            await browser.close()


def _find(captured: dict, prefix: str) -> dict | None:
    for path, v in captured.items():
        if path.lstrip("/").lower().startswith(prefix.lower()):
            return v
    return None


async def _screenshot(page, tag: str) -> str:
    path = artifact_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


async def _visible_error(page) -> str | None:
    body = await page.inner_text("body")
    for t in ERROR_TEXTS:
        if t in body:
            line = next((ln for ln in body.splitlines() if t in ln), t)
            return line.strip()
    return None


async def _wait_settled(page, ms: int = 1500):
    try:
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def _search(page, captured, *, plate: str, state: str, notice_number: str | None):
    await page.goto(BASE_URL, wait_until="networkidle")
    await page.click(SEL["yes_btn"] if notice_number else SEL["no_btn"])
    if notice_number:
        await page.fill(SEL["notice_input"], format_notice_number(notice_number))
    await page.select_option(SEL["state_select"], value=state.upper())
    await page.fill(SEL["plate_input"], site_plate(plate))
    await page.click(SEL["search_btn"])
    await _wait_settled(page, 2500)
    err = await _visible_error(page)
    if err:
        shot = await _screenshot(page, "lookup-error")
        raise CtioError(f"{err} (screenshot: {shot})")


# --- read-only flows ------------------------------------------------------------------------


async def list_notices(plate: str, state: str = "CO") -> dict:
    """Guest ("No notice number") flow: every notice on a plate, masked numbers + amounts."""
    async with _browser() as (page, captured):
        try:
            await _search(page, captured, plate=plate, state=state, notice_number=None)
        except CtioError as e:
            if "is not associated with a CTIO" not in str(e):
                raise
            return {
                "plate": site_plate(plate),
                "state": state.upper(),
                "fleet_vehicle": (vehicle_for_plate(plate) or {}).get("label"),
                "notices": [],
                "open_notice_count": 0,
                "site_message": str(e),
                "note": "CTIO has no civil penalty on record for this plate/state (as entered). "
                "Temp-tag entry format is unverified; the tag number is sent without 'TEMP'.",
            }
        qp = _find(captured, "login/QuickPay")
        data = (qp or {}).get("json") or {}
        shot = await _screenshot(page, f"guest-{site_plate(plate)}")
        notices = [
            {
                "notice_number_masked": n.get("Id"),
                "amount": float(n["Amount"]) if n.get("Amount") not in (None, "") else None,
                "notice_type": n.get("NoticeType"),
            }
            for n in data.get("Notifications") or []
        ]
        acct = data.get("AccountDetails") or {}
        return {
            "plate": data.get("LicensePlate") or site_plate(plate),
            "state": data.get("LP_State") or state.upper(),
            "site_vehicle": data.get("VehicleModel"),
            "fleet_vehicle": (vehicle_for_plate(plate) or {}).get("label"),
            "account_status_code": acct.get("StatusCode"),
            "account_type": acct.get("AccountType"),
            "total_due": (data.get("paynmData") or {}).get("amount"),
            "notices": notices,
            "open_notice_count": sum(1 for n in notices if (n["amount"] or 0) > 0),
            "page_url": page.url,
            "screenshot": shot,
            "note": "Guest mode masks notice numbers and gives no date/location. Use the full notice "
            "number from the mailed notice or CTIO email with ctio_lookup_notice for details.",
        }


async def _notice_login(page, captured, notice_number: str, plate: str, state: str) -> dict:
    await _search(page, captured, plate=plate, state=state, notice_number=notice_number)
    # Violations page lazy-loads account/GetViolations/<guid>.
    for _ in range(20):
        if _find(captured, "account/GetViolations/"):
            break
        await page.wait_for_timeout(500)
    login = _find(captured, "login/notification")
    if login and login["status"] >= 400:
        raise CtioError(f"Notice login rejected (HTTP {login['status']}): {(login.get('json') or {}).get('Message')}")
    return (_find(captured, "account/GetViolations/") or {}).get("json") or {}


async def lookup_notice(notice_number: str, plate: str, state: str = "CO") -> dict:
    async with _browser() as (page, captured):
        data = await _notice_login(page, captured, notice_number, plate, state)
        details = data.get("Details") or []
        violations = []
        for v in data.get("Violations") or []:
            row = _pick(v, _VIOLATION_KEYS)
            row["penalties"] = [_pick(d, _DETAIL_KEYS) for d in details if d.get("TVNID") == v.get("TVNID")]
            for p in row["penalties"]:
                p.pop("TVNID", None)
            row.pop("TVNID", None)
            violations.append(row)
        summary = _pick((data.get("ViolationsDetails") or [{}])[0], _SUMMARY_KEYS)
        shot = await _screenshot(page, f"notice-{site_plate(plate)}")
        text = await page.inner_text("main") if await page.query_selector("main") else await page.inner_text("body")
        return {
            "notice_number": format_notice_number(notice_number),
            "plate": site_plate(plate),
            "state": state.upper(),
            "fleet_vehicle": (vehicle_for_plate(plate) or {}).get("label"),
            "summary": summary,
            "adjudication_fees": [_pick(f, _DETAIL_KEYS) for f in data.get("Table2") or []],
            "violations": violations,
            "can_answer": any(v.get("Disputable") == 1 for v in data.get("Violations") or []),
            "can_request_hearing": any(v.get("AllowADJ") == 1 for v in data.get("Violations") or []),
            "page_url": page.url,
            "page_text": text[:6000],
            "screenshot": shot,
            "raw_data_found": bool(data),
        }


# --- "prepare" flows: navigate to the last screen before an irreversible action, then STOP ---


async def prepare_payment(plate: str, state: str = "CO", notice_number: str | None = None) -> dict:
    async with _browser() as (page, captured):
        if notice_number:
            await _notice_login(page, captured, notice_number, plate, state)
        else:
            await _search(page, captured, plate=plate, state=state, notice_number=None)
        await _wait_settled(page)
        pay = page.get_by_role("button", name=re.compile(r"^\s*Pay", re.I))
        pay_text = (await pay.first.inner_text()).strip() if await pay.count() else None
        shot = await _screenshot(page, f"pay-ready-{site_plate(plate)}")
        text = await page.inner_text("body")
        total = re.search(r"(?:Total Amount|TOTAL DUE|Outstanding balance)\s*:?\s*\n?\s*(\$[\d,]+\.\d{2})", text)
        return {
            "stopped_before": "Pay button (not clicked; no email or card details entered)",
            "pay_button_text": pay_text,
            "total_shown": total.group(1) if total else None,
            "mode": "notice" if notice_number else "guest",
            "page_url": page.url,
            "screenshot": shot,
            "warning": "Per CTIO terms, paying waives the right to dispute/adjudicate. "
            "For rented vehicles consider Answer -> 'vehicle was rented' (Declaration of Non-Liability) instead.",
        }


async def prepare_hearing_request(notice_number: str, plate: str, state: str = "CO") -> dict:
    async with _browser() as (page, captured):
        data = await _notice_login(page, captured, notice_number, plate, state)
        steps = ["notice login"]
        if not await page.locator(SEL["dispute_link"]).count():
            shot = await _screenshot(page, f"hearing-no-answer-link-{site_plate(plate)}")
            return {"ready": False, "reason": "No Answer/dispute link on violations page (nothing disputable?)",
                    "page_url": page.url, "screenshot": shot}
        # Opening the Answer page is navigation only (no POST).
        await page.click(SEL["dispute_link"])
        await _wait_settled(page)
        steps.append("opened Answer (dispute) page")
        hearing_btn = page.locator(SEL["request_hearing_btn"])
        if await hearing_btn.count() and await hearing_btn.first.is_enabled():
            # Clicking this opens the hearing form / $30 fee acknowledgement modal. We do NOT
            # click Acknowledge (that POSTs account/HearingAck) and do NOT click Submit.
            await hearing_btn.first.click()
            await _wait_settled(page)
            steps.append("opened Request-a-Hearing form (acknowledgement NOT clicked)")
            ready = True
        else:
            ready = False
            steps.append("Request a Hearing button absent/disabled (hearing may require a prior Answer review)")
        shot = await _screenshot(page, f"hearing-ready-{site_plate(plate)}")
        text = await page.inner_text("body")
        return {
            "ready": ready,
            "steps": steps,
            "stopped_before": "Hearing fee acknowledgement + Submit (declaration under penalty of perjury)",
            "can_request_hearing_per_data": any(v.get("AllowADJ") == 1 for v in data.get("Violations") or []),
            "page_url": page.url,
            "page_text": text[:4000],
            "screenshot": shot,
        }


# --- rental Declaration of Non-Liability ---------------------------------------------------


async def rental_declaration(
    *, notice_number: str, plate: str, state: str, renter_name: str, renter_address, rental_start: str,
    rental_end: str, agreement_path: str, civil_penalty_numbers: list[str] | None, contact_email: str,
    contact_phone: str, details: str | None, submit: bool,
) -> dict:
    """Shared prepare/submit flow. submit=False stops before #declaration-submit and #RentalSubmitId.
    Callers MUST gate submit=True (see server.submit_gate_rental)."""
    from ctio_mcp import rental as R

    # All offline validation happens before a browser is opened.
    attachment = R.check_attachment(agreement_path)
    form = R.build_form_values(
        renter_name=renter_name, renter_address=renter_address, rental_start=rental_start, rental_end=rental_end,
        contact_email=contact_email, contact_phone=contact_phone, details=details, plate=site_plate(plate),
    )
    values, start, end = form["values"], form["rental_start"], form["rental_end"]
    tag = f"{site_plate(plate)}-{R.digits(notice_number)[-4:]}"
    shots: list[str] = []

    async with _browser() as (page, captured):
        viol_json = await _notice_login(page, captured, notice_number, plate, state)
        if not await page.locator(SEL["dispute_link"]).count():
            shots.append(await _screenshot(page, f"rental-no-answer-link-{tag}"))
            raise CtioError(f"No Answer link on the violations page; nothing disputable? (screenshot: {shots[-1]})")
        await page.click(SEL["dispute_link"])
        await _wait_settled(page)
        for _ in range(20):
            if _find(captured, "account/GetDisputeViolations/"):
                break
            await page.wait_for_timeout(500)
        dispute_json = (_find(captured, "account/GetDisputeViolations/") or {}).get("json")
        await R.expand_all_notices(page)
        penalties = R.penalties_from_dispute_json(dispute_json, viol_json) or await R.read_grid_penalties(page)
        try:
            chosen = R.select_and_validate(penalties, civil_penalty_numbers, start, end)
        except R.RentalDeclarationError as e:
            shots.append(await _screenshot(page, f"rental-refused-{tag}"))
            raise CtioError(f"{e} (penalties on notice: {[p.as_dict() for p in penalties]}; screenshot: {shots[-1]})")
        ticked = await R.tick_penalties(page, [p.number for p in chosen])
        fill_info = await R.fill_rental_form(page, values, attachment)
        upload = _find(captured, "support/uploadAttachment")
        if upload and upload["status"] >= 400:
            shots.append(await _screenshot(page, f"rental-upload-failed-{tag}"))
            raise CtioError(f"CTIO rejected the attachment upload (HTTP {upload['status']}). Screenshot: {shots[-1]}")
        actual = await R.read_back(page)
        mismatch = R.diff_values(values, actual)
        shots.append(await _screenshot(page, f"rental-filled-{tag}"))
        result = {
            "notice_number": format_notice_number(notice_number),
            "plate": site_plate(plate),
            "fleet_vehicle": (vehicle_for_plate(plate) or {}).get("label"),
            "penalties_covered": [p.as_dict() for p in chosen],
            "penalties_ticked": ticked,
            "rental_period": {"start": start.isoformat(sep=" "), "end": end.isoformat(sep=" ")},
            "fields_entered": values,
            "fields_read_back": actual,
            "mismatches": mismatch,
            "attachment": {"file": str(attachment), "uploaded_to_ctio": bool(fill_info["uploaded_file"]),
                           "upload_http_status": (upload or {}).get("status")},
            "screenshots": shots,
            "page_url": page.url,
        }
        if not submit:
            result["stopped_before"] = ("#declaration-submit (penalty-of-perjury checkbox) and #RentalSubmitId. "
                                        "NOTE: the agreement file WAS uploaded to CTIO's attachment store (the site "
                                        "uploads on file selection), but no Answer/ticket was created.")
            return result

        # ---- submit path (gated by caller) ----
        if mismatch:
            raise CtioError(f"Refusing to submit: form values differ from intended: {mismatch} (screenshot: {shots[-1]})")
        await page.check(R.RSEL["perjury"])
        btn = page.locator(R.RSEL["submit"])
        if not await btn.is_enabled():
            shots.append(await _screenshot(page, f"rental-submit-disabled-{tag}"))
            raise CtioError(f"#RentalSubmitId is disabled; form not accepted by site validation (screenshot: {shots[-1]})")
        await btn.click()
        try:
            await page.get_by_text(R.RSEL["ack_text"]).first.wait_for(timeout=NAV_TIMEOUT_MS)
            acknowledged = True
        except Exception:
            acknowledged = False
        await _wait_settled(page)
        shots.append(await _screenshot(page, f"rental-submitted-{tag}"))
        ticket = _find(captured, "support/Ticket")
        tj = (ticket or {}).get("json") or {}
        result.update({
            "submitted": True,
            "acknowledged": acknowledged,
            "ctio_ticket_http_status": (ticket or {}).get("status"),
            "confirmation_ticket": tj.get("ticket") if isinstance(tj, dict) else None,
            "page_text_after": (await page.inner_text("body"))[:3000],
            "screenshots": shots,
        })
        if not acknowledged:
            result["warning"] = "Submit clicked but the 'Dispute Acknowledged' confirmation was not seen; check the screenshot and email."
        return result
