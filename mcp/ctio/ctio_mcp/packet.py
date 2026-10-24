"""Build a single-PDF declaration packet: one-page cover sheet + the rental agreement (PDF or image)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pymupdf

from ctio_mcp.rental import MAX_UPLOAD_BYTES, RentalDeclarationError, fmt_penalty_for_grid
from ctio_mcp.vehicles import vehicle_for_plate

OWNER = "Beverley Fleet"


def _cover_lines(*, plate, notice_number, penalty_numbers, renter_name, renter_address, rental_start, rental_end,
                 penalties_detail) -> list[tuple[str, str]]:
    v = vehicle_for_plate(plate)
    addr = renter_address
    addr_str = ", ".join(x for x in [addr.street, addr.apt, addr.city, f"{addr.state} {addr.zip}".strip()] if x)
    rows = [
        ("Registered owner", f"{OWNER} (vehicle rental business), Denver, Colorado"),
        ("License plate", plate),
        ("Vehicle", v["label"] if v else "(not in fleet list)"),
        ("Notice number", notice_number),
        ("Civil penalty number(s)", ", ".join(fmt_penalty_for_grid(n) for n in penalty_numbers) or "all on notice"),
        ("Renter name", renter_name),
        ("Renter address", addr_str),
        ("Rental period", f"{rental_start}  to  {rental_end}"),
    ]
    for p in penalties_detail or []:
        rows.append((f"Penalty {fmt_penalty_for_grid(p.get('civil_penalty_number', ''))}",
                     f"{(p.get('date_time') or p.get('date_time_raw') or '?').replace('T', ' ', 1)} at {p.get('location') or '?'}"))
    return rows


def build_packet(
    *, out_path: str | Path, agreement_path: str | Path, plate: str, notice_number: str,
    penalty_numbers: list[str], renter_name: str, renter_address, rental_start: str, rental_end: str,
    penalties_detail: list[dict] | None = None,
) -> dict:
    src = Path(agreement_path).expanduser()
    if not src.is_file():
        raise RentalDeclarationError(f"agreement_path not found: {src}")
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)  # US Letter
    y = 72
    page.insert_text((72, y), "Declaration of Non-Liability: Rented Vehicle", fontsize=16, fontname="hebo")
    y += 22
    page.insert_text((72, y), "Colorado Transportation Investment Office (CTIO), Safety & Toll Enforcement",
                     fontsize=10, fontname="helv")
    y += 30
    page.insert_textbox(
        pymupdf.Rect(72, y, 540, y + 60),
        "I declare that on the date of each violation listed below, the vehicle bearing the license plate "
        "number below was rented. A copy of the rental agreement showing the date of rental and the name and "
        "address of the renter is attached.",
        fontsize=10.5, fontname="helv",
    )
    y += 72
    for label, value in _cover_lines(plate=plate, notice_number=notice_number, penalty_numbers=penalty_numbers,
                                     renter_name=renter_name, renter_address=renter_address,
                                     rental_start=rental_start, rental_end=rental_end, penalties_detail=penalties_detail):
        page.insert_text((72, y), f"{label}:", fontsize=10.5, fontname="hebo")
        rc = page.insert_textbox(pymupdf.Rect(230, y - 10, 540, y + 30), str(value), fontsize=10.5, fontname="helv")
        y += 20 if rc >= 0 else 34
    y += 40
    page.draw_line((72, y), (330, y))
    page.draw_line((380, y), (540, y))
    page.insert_text((72, y + 14), f"Signature, {OWNER} (owner / authorized agent)", fontsize=9, fontname="helv")
    page.insert_text((380, y + 14), "Date", fontsize=9, fontname="helv")
    y += 46
    page.draw_line((72, y), (330, y))
    page.insert_text((72, y + 14), "Printed name", fontsize=9, fontname="helv")
    page.insert_text((72, 760), f"Prepared {date.today().isoformat()} | Attachment: {src.name}", fontsize=8, fontname="helv")

    suffix = src.suffix.lower()
    if suffix == ".pdf":
        with pymupdf.open(src) as a:
            doc.insert_pdf(a)
    elif suffix in (".jpg", ".jpeg", ".png"):
        img_page = doc.new_page(width=612, height=792)
        img_page.insert_image(pymupdf.Rect(36, 36, 576, 756), filename=str(src), keep_proportion=True)
    else:
        raise RentalDeclarationError(f"Unsupported agreement type '{src.suffix}' (use PDF, JPG or PNG).")

    doc.save(out, garbage=4, deflate=True)
    pages = doc.page_count
    doc.close()
    size = out.stat().st_size
    return {
        "packet_path": str(out),
        "pages": pages,
        "bytes": size,
        "within_ctio_5mb_limit": size <= MAX_UPLOAD_BYTES,
        "note": "Sign the cover sheet before mailing. For the web form you can upload this PDF as agreement_path.",
    }
