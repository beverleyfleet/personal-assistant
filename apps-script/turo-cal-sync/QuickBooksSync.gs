/**
 * QuickBooksSync.js
 * ---------------------------------------------------------------
 * There's no live QuickBooks API connection here — hooking Apps
 * Script up to Intuit's actual API would mean registering an
 * Intuit developer app and handling OAuth refresh tokens, a much
 * bigger lift than this needs. Instead, this reads the "Payment
 * received" notification emails QuickBooks already sends, which
 * reliably embed the contract number in the invoice number (e.g.
 * "Invoice #CO-1029-INV") and the paid/invoice amounts in the body.
 *
 * This only ever applies to Private bookings — Turo collects
 * payment on its own platform, so a Turo row's Deposit Paid/Fully
 * Paid stay 'N/A' (set by logBookingToTracker_) and this script
 * will not touch them even if it somehow matches a Turo row.
 *
 * SETUP: none beyond what Turo/PrivateBookings already need — same
 * Gmail/Sheets scopes, no new permission. Run `syncQuickBookPayments`
 * manually once to check it, same as the other sync functions.
 * ---------------------------------------------------------------
 */

// ====================== CONFIG ======================

// No date bound — see the equivalent note on Code.js's GMAIL_SEARCH.
const QB_GMAIL_SEARCH = 'from:quickbooks@notification.intuit.com subject:"Payment received" -label:qb-payment-synced';
const QB_PROCESSED_LABEL = 'qb-payment-synced';

// =====================================================

function syncQuickBookPayments() {
  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const processedLabel = getOrCreateLabel_(QB_PROCESSED_LABEL);
  const threads = GmailApp.search(QB_GMAIL_SEARCH);

  Logger.log('Found ' + threads.length + ' candidate QuickBooks payment emails.');

  threads.forEach(function (thread) {
    const subject = thread.getFirstMessageSubject();
    const messages = thread.getMessages();
    const body = messages[messages.length - 1].getPlainBody();

    const payment = parseQuickBooksPayment_(subject, body);
    if (!payment) {
      Logger.log('Could not parse a contract number/amount from: "' + subject + '" — leaving unlabeled for manual review.');
      return;
    }

    const found = findTrackerRowByContract_(ss, payment.contractNumber);
    if (!found) {
      Logger.log('No tracker row found yet for ' + payment.contractNumber + ' ("' + subject + '") — will retry next run in case the booking hasn\'t been logged yet.');
      return; // don't label — retry later once the row exists
    }

    const source = getRowValue_(found, 'Source');
    if (String(source).trim() === 'Turo') {
      Logger.log(payment.contractNumber + ' is a Turo row — leaving Deposit/Fully Paid as-is (Turo payments don\'t run through QuickBooks).');
      thread.addLabel(processedLabel);
      return;
    }

    applyPaymentToRow_(found, payment);
    Logger.log('Updated ' + payment.contractNumber + ': paid $' + payment.amountPaid + ' of $' + (payment.invoiceAmount || '?') + '.');
    thread.addLabel(processedLabel);
  });
}

// Extracts contract number + amounts from a QuickBooks "Payment received"
// email. Subject looks like: 'Payment received: Invoice #CO-1029-INV-(Jannalyn Santillana)'.
// Body (plain-text rendering of the HTML) contains lines like:
//   $300.00
//   Payment has been received
//   ...
//   Invoice no.  CO-1029-INV
//   Paid date  09/17/2026
//   Invoice amount  $600.00
function parseQuickBooksPayment_(subject, body) {
  const contractMatch = subject.match(/Invoice #([A-Za-z0-9\-]+)-INV/i) || body.match(/Invoice no\.?\s*([A-Za-z0-9\-]+)-INV/i);
  const paidMatch = body.match(/\$([\d,]+\.\d{2})\s*\r?\n\s*Payment has been received/i);
  const invoiceAmountMatch = body.match(/Invoice amount\s*\$?([\d,]+\.\d{2})/i);
  const paidDateMatch = body.match(/Paid date\s*([\d/]+)/i);

  if (!contractMatch || !paidMatch) return null;

  return {
    contractNumber: contractMatch[1].toUpperCase().trim(),
    amountPaid: parseFloat(paidMatch[1].replace(/,/g, '')),
    invoiceAmount: invoiceAmountMatch ? parseFloat(invoiceAmountMatch[1].replace(/,/g, '')) : null,
    paidDate: paidDateMatch ? paidDateMatch[1] : ''
  };
}

// Searches every tab in the tracker spreadsheet (each month's tab) for a
// row whose "Contract #" matches. Returns {sheet, rowIndex (1-based),
// headers} or null.
function findTrackerRowByContract_(ss, contractNumber) {
  const sheets = ss.getSheets();
  for (let s = 0; s < sheets.length; s++) {
    const sheet = sheets[s];
    const data = sheet.getDataRange().getValues();
    const headers = data[0];
    const contractIdx = headers.indexOf('Contract #');
    if (contractIdx === -1) continue;

    for (let r = 1; r < data.length; r++) {
      if (String(data[r][contractIdx]).toUpperCase().trim() === contractNumber) {
        return { sheet: sheet, rowIndex: r + 1, headers: headers };
      }
    }
  }
  return null;
}

// Used by logBookingToTracker_ (Code.js) to avoid double-logging a booking
// that's already in the tracker. Private bookings' reservationId IS the
// CO-#### contract number, so an exact match on Contract # catches those.
// Turo's reservationId is Turo's own internal number, which was never
// stored anywhere in the tracker (historical Turo rows only have the
// CO-#### contract number, assigned separately) — so for Turo bookings we
// fall back to matching by guest first name + exact start date, which is
// what's actually comparable between the two systems.
function findDuplicateBookingRow_(ss, booking, sourceLabel) {
  if (booking.reservationId) {
    const exact = findTrackerRowByContract_(ss, String(booking.reservationId).toUpperCase().trim());
    if (exact) return exact;
  }
  if (sourceLabel !== 'Turo') return null;

  const guestFirstName = (booking.guestName || '').toLowerCase().split(' ')[0];
  if (!guestFirstName) return null;
  const startDay = Utilities.formatDate(booking.start, TIMEZONE, 'yyyy-MM-dd');

  const sheets = ss.getSheets();
  for (let s = 0; s < sheets.length; s++) {
    const sheet = sheets[s];
    const data = sheet.getDataRange().getValues();
    const headers = data[0];
    const nameIdx = headers.indexOf('Name');
    const dateOutIdx = headers.indexOf('Date Out');
    if (nameIdx === -1 || dateOutIdx === -1) continue;

    for (let r = 1; r < data.length; r++) {
      const rowName = String(data[r][nameIdx] || '').toLowerCase();
      const rowDateOut = coerceDate_(data[r][dateOutIdx]);
      if (!rowName || !rowDateOut) continue;
      if (rowName.indexOf(guestFirstName) === -1) continue;
      if (Utilities.formatDate(rowDateOut, TIMEZONE, 'yyyy-MM-dd') !== startDay) continue;
      return { sheet: sheet, rowIndex: r + 1, headers: headers };
    }
  }
  return null;
}

function getRowValue_(found, headerName) {
  const col = found.headers.indexOf(headerName);
  if (col === -1) return '';
  return found.sheet.getRange(found.rowIndex, col + 1).getValue();
}

// General rule (not tied to any specific booking): if the same vehicle
// already has a row from a DIFFERENT source whose dates overlap this one,
// that's almost always the same real-world booking recorded twice — most
// commonly because a guest booked via Turo and then the deal moved to a
// direct/private arrangement for the same dates. Scans every tab.
function findSameVehicleDateConflict_(ss, booking, vehicleLabel, wantedSource) {
  const vehicleLower = (vehicleLabel || '').toLowerCase();
  if (!vehicleLower || !booking.start || !booking.end) return null;

  const sheets = ss.getSheets();
  for (let s = 0; s < sheets.length; s++) {
    const sheet = sheets[s];
    const data = sheet.getDataRange().getValues();
    const headers = data[0];
    const vehicleIdx = headers.indexOf('Vehicle');
    const dateOutIdx = headers.indexOf('Date Out');
    const dateDueIdx = headers.indexOf('Date Due');
    const sourceIdx = headers.indexOf('Source');
    if (vehicleIdx === -1 || dateOutIdx === -1 || dateDueIdx === -1 || sourceIdx === -1) continue;

    for (let r = 1; r < data.length; r++) {
      if (String(data[r][sourceIdx] || '').trim() !== wantedSource) continue;

      const rowVehicle = String(data[r][vehicleIdx] || '').toLowerCase();
      if (!rowVehicle || (rowVehicle.indexOf(vehicleLower) === -1 && vehicleLower.indexOf(rowVehicle) === -1)) continue;

      const rowStart = coerceDate_(data[r][dateOutIdx]);
      const rowEnd = coerceDate_(data[r][dateDueIdx]);
      if (!rowStart || !rowEnd) continue;

      if (rowStart <= booking.end && booking.start <= rowEnd) {
        return { sheet: sheet, rowIndex: r + 1, headers: headers };
      }
    }
  }
  return null;
}

function setRowValue_(found, headerName, value) {
  const col = found.headers.indexOf(headerName);
  if (col === -1) return;
  found.sheet.getRange(found.rowIndex, col + 1).setValue(value);
}

// Marks Deposit Paid true on first payment; marks Fully Paid true once the
// cumulative signal says the invoice is settled — either this single
// payment already covers the full invoice amount, or a deposit was already
// on record before this payment (i.e. this is the balance completing it).
// Revenue amounts themselves (Total Due) live in the WordPress ops
// platform now, not this sheet — see logBookingToTracker_ in Code.js.
function applyPaymentToRow_(found, payment) {
  const depositAlreadyPaid = getRowValue_(found, 'Deposit Paid') === true;

  setRowValue_(found, 'Deposit Paid', true);

  const paidInFull = payment.invoiceAmount && payment.amountPaid >= payment.invoiceAmount - 0.01;
  if (paidInFull || depositAlreadyPaid) {
    setRowValue_(found, 'Fully Paid', true);
  }
}
