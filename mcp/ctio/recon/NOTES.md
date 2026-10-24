# CTIO site recon (2026-10-24, headless Chromium 153 via Playwright 1.63)

Site: https://expresslanesafety.com/CO, an Angular SPA. `/CO` redirects to `/CO/violations`.
Backend: `https://api.expresslanesafety.com/` (JSON). Bundles: `main.db152199c442b4ec.js`,
lazy violations module `216.e009d76d86d338e6.js`. Hashes will change when CTIO redeploys.

## Does lookup need a login? No.
There is **no user-account / password login**. All flows use:

| Flow | Inputs | API call | Result page |
|---|---|---|---|
| "Yes" (have notice #) | Notice Number `000-000-000-000` (bottom-right of mailed notice), Plate State, Plate | `POST login/notification` `{NoticeNumber, LicensePlate, LP_State}` | `/CO/violations/Safetyviolations`, which loads `GET account/GetViolations/<AccountGuid>` |
| "No" (guest pay) | Plate State, Plate | `POST login/QuickPay` `{LicensePlate, LP_State}` | `/CO/violations/payment` "Summary" page |

Guest mode text: "GUEST PAY ONLY. To Answer by disputing or pay less than the total amount due
you will need your notice number and license plate."

## Selectors (landing page)
- Yes / No toggles: `button[title="Click here if you have your notice number"]` / `...do not have...`
- Notice: `input[aria-label="Notice Number"]` (placeholder `000-000-000-000`, maxlength 15)
- State: `select[aria-label="Select License Plate State"]`, option values are 2-letter codes (`CO`)
- Plate: `input[aria-label="License Plate Number"]` (site uppercases; "without state and dashes")
- Submit: `#SearchViolationsId`
- Errors (inline text): "Notice Number and License Plate do not match" (HTTP 401 `{"Message":"Invalid data"}`),
  "License Plate X-Colorado is not associated with a CTIO Civil Penality" (sic; HTTP 401 `Unauthorized`).

## Guest summary (verified live, plate EIUY09)
`login/QuickPay` returns `Notifications[]` with `Id` = masked notice (`90X-XXX-XXX-X79`), `Amount`,
`NoticeType`; `paynmData.amount` = total; `VehicleModel`; `AccountDetails` (also contains VIN,
owner name, AccountGuid, DMV ids, which the server drops). Page shows Summary, `Email address
(*Required)` and a **Pay** button. Screenshot: `05_guest_EIUY09.png`.
EIUY09 on 2026-10-24: 6 notices, 3 with balances ($300, $300, $142), total $742.00.
FNYI83, FNYG83, 8620436, 8632431 (CO): "not associated with a CTIO Civil Penalty".

## Notice-mode violations page (from bundle only, NOT seen live; no real notice # available)
- Table: Notice Number | Notice Date | description (Description1-3) | Amount (DisplayAmount); checkbox per
  row (`Payable`), expand button showing CIVIL PENALTY NUMBER, DATE / TIME, LOCATION, VIOLATION, AMOUNT
  (from `Details[]`: `CivielPenalityNumber` (sic), `DataTime`, `Location`, `safetyViolation`, `Amount`).
- Footer: Subtotal, adjudication fees (`Table2`: FeeDate, HearingFee), "Outstanding balance".
- Flags per row: `Payable`, `Disputable`, `AllowADJ` (hearing allowed).
- Pay button text "Pay $<amount>" (`PaymentSubmit()` -> `api/Payment/paypnm` -> Stripe/Paynearme). Not clicked.
- Answer: `#disputeLinkId` -> `/violations/disputes`. Reason select `#DisCivPenReasonSelect`; reasons include
  rental ("I declare that on the date of the violation, the vehicle ... was rented", needs copy of rental
  agreement + renter name/address, submit `#RentalSubmitId`), leased, stolen, sold, never owned, other.
- Request a Hearing: `#requestAHearingBtnId`, fee acknowledgement modal `#reqHearingAckModal` /
  `#ReqHearingAcknowledgementBtn` (POSTs `account/HearingAck` - a WRITE, so the server stops before it),
  "$30 Adjudication Fee if found liable", declaration checkboxes `#declarationAgree`, submit `#declarationSubmit`.
- Accounts with `account_status == "RENTAL"` get a different Answer link text.

## Files
- `01_landing.(html|png)` landing; `02_yes_fake_notice.png` mismatch error; `03_no_guest_form.png`;
  `04_no_guest_fake_plate.png`; `05_guest_EIUY09.(html|png)` real guest summary; `api_log.json` (dummy inputs only);
  `live/` screenshots produced by the server's own tools during testing.

## Rental Declaration of Non-Liability (Answer reason "Rental"), from bundle `216.e009d76d86d338e6.js`
Not reachable live without a real full notice number. Everything below is from the bundle; the
`live` test `test_live_bundle_still_has_rental_selectors` re-fetches the deployed chunk and checks
every selector string is still present (passed 2026-10-24).

Answer page (`/CO/violations/disputes`, component `app-dispute-grid`):
- Data: `GET account/GetDisputeViolations/<AccountGuid>` -> `Details[]`: `TrxnNum` (civil penalty #),
  `TransactionDate`, `Lane` (location), `Description`, `StatusCode` (UNPAID / IN REVIEW / HEARING REQUESTED /
  DISMISSED / IN COLLECTIONS), `TollAmount`, `Disputable` (1/0), `AllowADJ`, `TVNID`.
- Notice row expand: `button > img[title="Click here to Show Civil Penalties"]`.
- Child table `table.expanded-table` columns: [checkbox] | Civil Penality Number (shown `ddd ddd ddd`) |
  Date Time | Location | Description | Status | Amount Due.
- Per-penalty checkbox: `input[type=checkbox][aria-label="Please Check"]` (two per row; the Answer one is the
  non-`hidden` one, `disabled` when not Disputable). Ticking opens detail modal `#myModal`
  (buttons "Close" / "Continue to Answer").
- Selected penalties are sent as `Fields.ViolationNumbers` (comma-joined TrxnNum) or `Fields.NoticeNumbers`.
- Reason select `#DisCivPenReasonSelect` (`aria-label="Select a reason for the dispute"`), option values:
  "Vehicle was stolen", "I have never owned this vehicle", **"Rental"**, "I no longer own this vehicle",
  "Vehicle owner deceased", "Leased", "Bankruptcy", "Other".
- Contact info (shown once a reason is selected, both required): `input[aria-label="Please Enter Email"]`
  (maxlength 40), `input[aria-label="Please Enter Phone"]` (maxlength 14, US-validated, formatted on keyup).

`app-rental` form (Subject "Rental"; heading "Affidavit of Non-Liability"; "I declare that on the date of the
violation, the vehicle with the license plate number above was rented."):

| Field (API name) | Selector | Notes |
|---|---|---|
| Rental_Agrement_Start_Date | `input[type=date][aria-label="Please Enter Rental Agreement Start Date"]` | required |
| Rental_Agrement_End_Date | `input[type=date][aria-label="Please Enter Rental Agreement End Date"]` | required |
| FirstName | `input[aria-label="Please Enter First Name"]` | required, max 30 |
| LastName | `input[aria-label="Please Enter Last Name"]` | required, max 30 |
| MailingAddress | `input[aria-label="Please Enter Mailing Address"]` | required, max 60 |
| Apt_Date (sic, apt/suite) | `input[aria-label="Please Enter Apt / Suite"]` | optional, max 30 |
| City | `input[aria-label="Please Enter City"]` | required, max 30 |
| State | `select[aria-label="Please select State"]` | required; option values are FULL names ("Colorado") |
| ZipCode | `input[aria-label="Please Enter ZipCode"]` | required, max 10 |
| attachment | `input#file[type=file][accept=".pdf,.jpg,.jpeg,.png"]` | required; ext check is case-sensitive lowercase; helper text "Maximum file size is 5mb" (no client-side size check found). Selecting a file immediately `POST support/uploadAttachment/<AccountGuid>`; uploaded names listed in `.alert.alert-secondary .text-info` |
| Description | `textarea[title="Description"]` | optional, "Remaining characters 500" |
| e-communication consent | `#declaration-agree` (checkbox) | required |
| penalty-of-perjury declaration | `#declaration-submit` (checkbox) | required: "By clicking submit, I declare under penalty and perjury that the above information is true and correct." |
| Submit | `#RentalSubmitId` | Cancel: `button[aria-label="Cancel"]` |

Submit -> parent `disputeAction`: re-fetches GetDisputeViolations, aborts with alert "One or more selected
violations cannot be disputed." if any selected TrxnNum is not Disputable, else `POST support/Ticket`
(feedObject: Subject, Description, Email, Phone, AccountGUID, AttachmentTicket[tokens], Fields{...}) -> response
`{ticket}` (Zendesk id) -> modal "Dispute Acknowledged / You will receive an email confirmation shortly."
Site copy: "You will receive a response by email with five (5) business days".

Local fixture reproducing this markup: `tests/fixtures/dispute_rental.html`.
