# ctio-mcp: CTIO express-lane penalties for Beverley Fleet

Local MCP server (stdio) that reads Colorado Transportation Investment Office (CTIO) express-lane
civil-penalty data from https://expresslanesafety.com/CO and parses CTIO emails from
`support@expresslanesafetyco.com`. Read-only by default; nothing pays, disputes or requests a hearing.

## Install (on the owner's machine)

```bash
cd ~/beverley-ops/mcp/ctio
python3 -m venv .venv && . .venv/bin/activate      # or: uv venv && source .venv/bin/activate
pip install -e '.[test]'                            # or: uv pip install -e '.[test]'
playwright install chromium                         # one-time browser download
pytest -q                                           # offline tests (~1 min; fixture tests drive Chromium)
CTIO_LIVE=1 pytest -q -m live -s                    # optional: real read-only lookup of EIUY09
```

Register with Claude Code (use the venv's python so the deps resolve):

```bash
claude mcp add ctio -- ~/beverley-ops/mcp/ctio/.venv/bin/python -m ctio_mcp
# with options:
claude mcp add ctio -e CTIO_ARTIFACT_DIR=$HOME/beverley-ops/ctio-screens -- ~/beverley-ops/mcp/ctio/.venv/bin/python -m ctio_mcp
```

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `ctio_list_notices(plate, state="CO")` | read-only, web | Guest lookup by plate: every notice on the plate, **masked** notice numbers, amounts, total due, screenshot. Plates with nothing on file return `notices: []`. |
| `ctio_lookup_notice(notice_number, plate, state="CO")` | read-only, web | Notice # + plate lookup: per-penalty number, date/time, location, violation, amount, Payable/Disputable/hearing flags, totals. |
| `ctio_parse_email(text, subject=None)` | pure, local | CTIO email -> `{status, plate, answer_number, notice_number, amount_due, due_date, hearing_request_deadline, vehicle, ...}` |
| `ctio_vehicle_for_plate(plate)` | local | Plate -> fleet vehicle (from `ctio_mcp/fleet.toml`). |
| `ctio_prepare_payment(plate, state, notice_number=None)` | read-only, web | Goes to the screen with the Pay button and **stops**. Returns total, button text, screenshot. Does not type an email or card. |
| `ctio_prepare_hearing_request(notice_number, plate, state)` | read-only, web | Notice login -> Answer page -> Request a Hearing, **stops** before the $30-fee acknowledgement (which POSTs to CTIO) and Submit. |
| `ctio_prepare_rental_declaration(...)` | **uploads file**, web | Fills the "vehicle was rented" Declaration of Non-Liability for one notice; refuses if any penalty is outside the rental period; stops before the perjury checkbox and Submit. |
| `ctio_submit_rental_declaration(..., confirm)` | **files with CTIO**, gated | Same as prepare, re-done fresh in one session, then ticks the perjury declaration and clicks Submit. Needs `CTIO_ALLOW_SUBMIT=1` and `confirm=True`. |
| `ctio_build_declaration_packet(...)` | local | One PDF: signed-cover-sheet page + rental agreement, for mailing or single-file upload. |
| `ctio_status()` | local | Base URL, submit-gate state, `op` availability. No secrets. |

### Gated
**Never automated:** payments and hearing requests. Both prepare tools accept `submit` and `confirm`. `submit=True` is refused unless env
`CTIO_ALLOW_SUBMIT=1` **and** `confirm=True`; even then it raises "not implemented". Payments,
Answers (disputes) and hearing requests stay human: open the returned screenshot/URL and finish
in a browser. Note CTIO's terms: paying waives the right to dispute. For rented vehicles, the
Answer form has a "vehicle was rented" Declaration of Non-Liability (needs the rental agreement
and renter name/address); that is the usual route for a rental fleet, and the one filing the
server can do (next section).

## File a rental declaration (step by step)

One filing per notice; each needs the **full** notice number (bottom-right of the mailed notice, or the CTIO
email) plus the plate. Guest lookups only show masked numbers.

1. Set the owner contact CTIO replies to (once, in the `claude mcp add` env):
   `-e CTIO_CONTACT_EMAIL=... -e CTIO_CONTACT_PHONE=...` (or pass `contact_email`/`contact_phone`).
2. Have the rental agreement as **.pdf/.jpg/.jpeg/.png (lowercase extension), max 5 MB**, showing rental dates
   and the renter's name and address.
3. Optional: `ctio_build_declaration_packet(plate, notice_number, renter_name, renter_address, rental_start,
   rental_end, agreement_path, civil_penalty_numbers=[...])` makes one PDF (cover sheet + agreement). Sign it
   if mailing; it can also be used as `agreement_path` for the upload.
4. `ctio_prepare_rental_declaration(notice_number, plate, renter_name="First Last",
   renter_address={street, apt?, city, state, zip}, rental_start="YYYY-MM-DD[THH:MM]", rental_end=...,
   agreement_path=..., civil_penalty_numbers=None|[...])`.
   - Logs in, reads each civil penalty's date/time/location, and **refuses** if any chosen penalty is outside
     `rental_start..rental_end` (date-only bounds cover the whole day), unreadable, or not disputable.
   - Ticks those penalties, picks reason "Rental", fills contact + renter fields, uploads the agreement, ticks
     the e-communication consent, reads every field back, screenshots, and **stops** (perjury box unticked,
     Submit not clicked). Side effect: the file is uploaded to CTIO's attachment store, but no Answer exists.
5. The owner reviews `fields_entered`, `fields_read_back`, `mismatches` (must be `{}`), `penalties_covered`
   (date/time/location) and the screenshot.
6. To file: start the server with `CTIO_ALLOW_SUBMIT=1`, then call `ctio_submit_rental_declaration` with the
   **same arguments** plus `confirm=True`. It redoes step 4 from scratch in one browser session, refuses on
   any mismatch or date problem, ticks the penalty-of-perjury declaration, clicks `#RentalSubmitId`, and
   returns `confirmation_ticket` (CTIO/Zendesk ticket id), whether "Dispute Acknowledged" appeared, and screenshots.
   Unset `CTIO_ALLOW_SUBMIT` afterwards. CTIO says it replies by email within 5 business days.
7. Repeat per notice. Track replies with `ctio_parse_email` (Answer Received / Reviewed / Dismissed).

The declaration is sworn by the owner. Only submit after the owner has seen the prepare output for that notice.

## Credentials / 1Password

The site has **no account login** (recon 2026-10-24): lookups use notice # + plate, or plate only.
So no tool needs a password today and none calls `op`. `ctio_mcp/credentials.py` is ready if CTIO
adds accounts: it runs `op read "op://<vault>/<item>/username"` and `.../password` via subprocess,
reference from `CTIO_OP_REF` (default `op://Beverley Fleet/CTIO ExpressLaneSafety`), and never logs
or returns the values. If you want the item in place anyway, create in vault **Beverley Fleet** a
Login item titled **CTIO ExpressLaneSafety** with `username`, `password` and website
`https://expresslanesafety.com/CO`. For headless use, export `OP_SERVICE_ACCOUNT_TOKEN`; `op` reads it
itself (this code never touches it).

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `CTIO_ALLOW_SUBMIT` | off | First of two gates for submit (submit is still not implemented). |
| `CTIO_ARTIFACT_DIR` | `~/.cache/ctio-mcp/screenshots` | Where screenshots go. |
| `CTIO_HEADLESS` | `1` | `0` to watch the browser. |
| `CTIO_TIMEOUT_MS` | `45000` | Page timeout. |
| `CTIO_BASE_URL` | `https://expresslanesafety.com/CO` | Site root. |
| `CTIO_FLEET_FILE` | bundled `fleet.toml` | Plate map. |
| `CTIO_OP_REF` / `CTIO_OP_BIN` | see above / `op` | 1Password reference / binary. |
| `CTIO_CONTACT_EMAIL` / `CTIO_CONTACT_PHONE` | none | Owner contact for rental declarations. |

## What is verified vs not (as of 2026-10-24, from a cloud container)

Verified live against the real site:
- Guest lookup (`ctio_list_notices`) for EIUY09: 6 notices, total $742.00, masked numbers.
- "No record" handling for FNYI83, FNYG83, 8620436, 8632431.
- `ctio_prepare_payment` guest mode reaches the Summary/Pay screen and stops.
- Notice-mode error path ("Notice Number and License Plate do not match") for a fake notice #.

Not verified (no real full notice number was available; guest mode masks them):
- `ctio_lookup_notice` success path and its field mapping (built from the site's JS bundle).
- `ctio_prepare_payment` with `notice_number`, and all of `ctio_prepare_hearing_request`
  (button IDs `#disputeLinkId`, `#requestAHearingBtnId` taken from the bundle).
- How temp tags (e.g. "Temp 8620436") must be typed; the tool sends `8620436`.
- Email parser was tested only on the sample fields provided plus synthetic Reviewed/Dismissed/Notice
  bodies; real CTIO email wording for those kinds may differ.
- `op` integration: tested with a fake `op` binary only.
- Rental declaration (prepare and submit): **never run against the real site**. Selectors/field names come
  from the deployed JS bundle (a live test confirms they are still present) and the fill/tick/read-back/submit
  logic is tested end-to-end against `tests/fixtures/dispute_rental.html`, a local copy of that markup. Not
  verified: the real login->Answer navigation, the detail modal after ticking a penalty, the real
  `TransactionDate` format, the upload response, whether `#RentalSubmitId` enables as expected, and the real
  confirmation/ticket response. Nothing has been submitted to CTIO.
- Packet PDF: built and text-checked in tests; layout not reviewed by a person.

Recon notes, selectors and screenshots: `recon/NOTES.md`.
