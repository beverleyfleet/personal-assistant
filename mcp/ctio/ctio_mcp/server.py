"""MCP server (stdio) exposing CTIO express-lane penalty tools for Beverley Fleet."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ctio_mcp import site
from ctio_mcp.credentials import op_status
from ctio_mcp.email_parser import parse_ctio_email
from ctio_mcp import packet, rental
from ctio_mcp.vehicles import fleet, vehicle_for_plate

# stdout is the MCP channel; log to stderr only, and never log secrets.
logging.basicConfig(stream=sys.stderr, level=os.environ.get("CTIO_LOG_LEVEL", "WARNING"))

# Not read-only: selecting the agreement file uploads it to CTIO's attachment store.
UPLOADS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
SUBMITS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = MCPServer(
    name="ctio",
    instructions=(
        "Colorado CTIO express-lane civil penalties (expresslanesafety.com/CO) for Beverley Fleet. "
        "Lookups are read-only. prepare_* tools stop at the final screen. Payments and hearing requests are "
        "never automated. The only filing tool is ctio_submit_rental_declaration (sworn 'vehicle was rented' "
        "declaration), gated by env CTIO_ALLOW_SUBMIT=1 and confirm=True; always run the prepare tool first and "
        "get the owner's explicit go-ahead on its exact output."
    ),
)


class SubmitBlocked(ToolError, PermissionError):
    """Submit refused by the gate (env flag off, or confirm not passed)."""


class SubmitNotImplemented(ToolError, NotImplementedError):
    """Both gate checks passed, but automated submission deliberately does not exist."""


def submit_gate(action: str, confirm: bool) -> None:
    """Gate for any irreversible action. Even when both checks pass, submitting is not implemented."""
    if os.environ.get("CTIO_ALLOW_SUBMIT", "").strip().lower() not in ("1", "true", "yes"):
        raise SubmitBlocked(f"{action}: submitting is disabled. CTIO_ALLOW_SUBMIT is not set (default off).")
    if confirm is not True:
        raise SubmitBlocked(f"{action}: CTIO_ALLOW_SUBMIT is on but confirm=True was not passed.")
    raise SubmitNotImplemented(
        f"{action}: automated submission is intentionally not implemented. Payments, Answers and hearing "
        "requests stay human: open the screenshot/URL returned by the prepare tool and finish it in a browser."
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
async def ctio_lookup_notice(notice_number: str, plate: str, state: str = "CO") -> dict[str, Any]:
    """Look up one Notice of Civil Penalty by its full notice number (000-000-000-000, bottom-right of
    the mailed notice) plus plate. Returns each civil penalty: number, date/time, location, violation,
    amount, status flags, plus summary totals and a screenshot path. Read-only."""
    return await site.lookup_notice(notice_number, plate, state)


@mcp.tool(annotations=READ_ONLY, structured_output=True)
async def ctio_list_notices(plate: str, state: str = "CO") -> dict[str, Any]:
    """List every CTIO notice on a plate via the site's guest-pay lookup (no notice number needed).
    Notice numbers come back MASKED (e.g. 90X-XXX-XXX-X79) with amounts and total due; no
    dates/locations. Read-only: does not enter an email or click Pay."""
    return await site.list_notices(plate, state)


@mcp.tool(annotations=LOCAL_ONLY, structured_output=True)
def ctio_parse_email(text: str, subject: str | None = None) -> dict[str, Any]:
    """Parse a CTIO email body (from support@expresslanesafetyco.com: Notice, Answer Received,
    Answer Reviewed, Dismissed, ...) into JSON: status, plate, answer/notice number, amount due,
    due date (ISO), hearing deadline, fleet vehicle. Pure/local."""
    return parse_ctio_email(text, subject)


@mcp.tool(annotations=LOCAL_ONLY, structured_output=True)
def ctio_vehicle_for_plate(plate: str) -> dict[str, Any]:
    """Map a plate (e.g. EIUY09, 'Temp 8620436') to the Beverley Fleet vehicle."""
    v = vehicle_for_plate(plate)
    if v is None:
        return {"plate": plate, "found": False, "fleet_plates": [x["plate"] for x in fleet()]}
    return {"found": True, **v}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
async def ctio_prepare_payment(
    plate: str,
    state: str = "CO",
    notice_number: str | None = None,
    submit: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Navigate to the payment screen (guest mode by plate, or notice mode if notice_number is given)
    and STOP before Pay. Returns totals, the Pay button text and a screenshot. Never pays.
    submit=True is gated by env CTIO_ALLOW_SUBMIT and confirm=True, and is not implemented."""
    if submit:
        submit_gate("ctio_prepare_payment", confirm)
    return await site.prepare_payment(plate, state, notice_number)


@mcp.tool(annotations=READ_ONLY, structured_output=True)
async def ctio_prepare_hearing_request(
    notice_number: str,
    plate: str,
    state: str = "CO",
    submit: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Log in with notice number + plate, open Answer -> Request a Hearing, and STOP before the $30-fee
    acknowledgement and Submit. Returns steps taken and a screenshot. Never submits.
    submit=True is gated by env CTIO_ALLOW_SUBMIT and confirm=True, and is not implemented."""
    if submit:
        submit_gate("ctio_prepare_hearing_request", confirm)
    return await site.prepare_hearing_request(notice_number, plate, state)


class RenterAddress(BaseModel):
    street: str = Field(description="Street / mailing address (max 60 chars)")
    city: str
    state: str = Field(description="2-letter code or full state name")
    zip: str
    apt: str | None = Field(default=None, description="Apt / suite (optional)")

    def to_dc(self) -> rental.Address:
        return rental.Address(street=self.street, city=self.city, state=self.state, zip=self.zip, apt=self.apt)


def _contact(email: str | None, phone: str | None) -> tuple[str, str]:
    return (email or os.environ.get("CTIO_CONTACT_EMAIL", ""), phone or os.environ.get("CTIO_CONTACT_PHONE", ""))


def submit_gate_rental(confirm: bool) -> None:
    """The ONLY submit the server performs. Needs CTIO_ALLOW_SUBMIT=1 and confirm=True."""
    if os.environ.get("CTIO_ALLOW_SUBMIT", "").strip().lower() not in ("1", "true", "yes"):
        raise SubmitBlocked("ctio_submit_rental_declaration: CTIO_ALLOW_SUBMIT is not set (default off). Nothing was sent.")
    if confirm is not True:
        raise SubmitBlocked("ctio_submit_rental_declaration: pass confirm=True after reviewing the prepare result. Nothing was sent.")


def _wrap(e: Exception) -> ToolError:
    return e if isinstance(e, ToolError) else ToolError(str(e))


@mcp.tool(annotations=UPLOADS, structured_output=True)
async def ctio_prepare_rental_declaration(
    notice_number: str,
    plate: str,
    renter_name: str,
    renter_address: RenterAddress,
    rental_start: str,
    rental_end: str,
    agreement_path: str,
    state: str = "CO",
    civil_penalty_numbers: list[str] | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    details: str | None = None,
) -> dict[str, Any]:
    """Fill CTIO's 'vehicle was rented' Declaration of Non-Liability for ONE notice and STOP before the
    penalty-of-perjury checkbox and Submit (#RentalSubmitId). Logs in with notice # + plate, reads each civil
    penalty's date/time/location and REFUSES if any selected penalty is outside rental_start..rental_end
    (YYYY-MM-DD or YYYY-MM-DDTHH:MM). civil_penalty_numbers=None means all disputable penalties on the notice.
    Uploads agreement_path (pdf/jpg/jpeg/png, <=5 MB) because the site uploads on file selection, but creates no
    Answer. contact_email/phone (owner's, for CTIO's reply) default to env CTIO_CONTACT_EMAIL/CTIO_CONTACT_PHONE.
    Returns the exact values entered and read back, penalties covered, and screenshot paths."""
    email, phone = _contact(contact_email, contact_phone)
    try:
        return await site.rental_declaration(
            notice_number=notice_number, plate=plate, state=state, renter_name=renter_name,
            renter_address=renter_address.to_dc(), rental_start=rental_start, rental_end=rental_end,
            agreement_path=agreement_path, civil_penalty_numbers=civil_penalty_numbers, contact_email=email,
            contact_phone=phone, details=details, submit=False)
    except rental.RentalDeclarationError as e:
        raise _wrap(e) from e


@mcp.tool(annotations=SUBMITS, structured_output=True)
async def ctio_submit_rental_declaration(
    notice_number: str,
    plate: str,
    renter_name: str,
    renter_address: RenterAddress,
    rental_start: str,
    rental_end: str,
    agreement_path: str,
    confirm: bool = False,
    state: str = "CO",
    civil_penalty_numbers: list[str] | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    details: str | None = None,
) -> dict[str, Any]:
    """FILES the rental Declaration of Non-Liability with CTIO (a sworn statement, under penalty of perjury,
    made on the owner's behalf). Only runs when env CTIO_ALLOW_SUBMIT=1 AND confirm=True. In the same browser
    session it redoes the full prepare (re-login, re-check dates against the rental period, re-fill, read back
    and compare every field), then ticks the perjury declaration and clicks #RentalSubmitId. Returns the CTIO
    ticket/confirmation and screenshots. Run ctio_prepare_rental_declaration first and have the owner review it."""
    submit_gate_rental(confirm)
    email, phone = _contact(contact_email, contact_phone)
    try:
        return await site.rental_declaration(
            notice_number=notice_number, plate=plate, state=state, renter_name=renter_name,
            renter_address=renter_address.to_dc(), rental_start=rental_start, rental_end=rental_end,
            agreement_path=agreement_path, civil_penalty_numbers=civil_penalty_numbers, contact_email=email,
            contact_phone=phone, details=details, submit=True)
    except rental.RentalDeclarationError as e:
        raise _wrap(e) from e


@mcp.tool(annotations=LOCAL_ONLY, structured_output=True)
def ctio_build_declaration_packet(
    plate: str,
    notice_number: str,
    renter_name: str,
    renter_address: RenterAddress,
    rental_start: str,
    rental_end: str,
    agreement_path: str,
    out_path: str | None = None,
    civil_penalty_numbers: list[str] | None = None,
    penalties_detail: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Make one PDF: a cover sheet (owner Beverley Fleet, plate, vehicle, notice #, penalty #s, renter
    name/address, rental period, signature line) followed by the rental agreement (PDF/JPG/PNG). Local only.
    penalties_detail can be the penalties_covered list from the prepare tool to print date/time/location."""
    out = out_path or str(site.artifact_dir().parent / "packets" /
                          f"CTIO-rental-{site.site_plate(plate)}-{rental.digits(notice_number)}.pdf")
    try:
        return packet.build_packet(
            out_path=out, agreement_path=agreement_path, plate=plate, notice_number=site.format_notice_number(notice_number),
            penalty_numbers=civil_penalty_numbers or [], renter_name=renter_name, renter_address=renter_address.to_dc(),
            rental_start=rental_start, rental_end=rental_end, penalties_detail=penalties_detail)
    except rental.RentalDeclarationError as e:
        raise _wrap(e) from e


@mcp.tool(annotations=LOCAL_ONLY, structured_output=True)
def ctio_status() -> dict[str, Any]:
    """Non-secret diagnostics: site URL, submit gate state, 1Password CLI availability."""
    return {
        "base_url": site.BASE_URL,
        "submit_enabled_env": os.environ.get("CTIO_ALLOW_SUBMIT", "") or "off",
        "submit_implemented": {"rental_declaration": True, "payment": False, "hearing_request": False},
        "contact_email_set": bool(os.environ.get("CTIO_CONTACT_EMAIL")),
        "contact_phone_set": bool(os.environ.get("CTIO_CONTACT_PHONE")),
        "artifact_dir": str(site.artifact_dir()),
        **op_status(),
    }


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
