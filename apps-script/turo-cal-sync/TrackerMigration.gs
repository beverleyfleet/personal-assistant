/**
 * TrackerMigration.js
 * ---------------------------------------------------------------
 * One-time move of the existing hand-entered rows (CO-1005 through
 * CO-1027b, all currently on the tracker's first tab) out into
 * their proper month tabs, so "a tab per month" applies to the
 * full history, not just bookings synced from here on.
 *
 * Copies each row to the month tab matching its Date Out, then
 * deletes it from the original tab — copy is verified (appendRow
 * doesn't throw) before the delete happens, and Google Sheets keeps
 * version history (File > Version history) if anything needs undoing.
 *
 * Rows with no parseable Date Out (e.g. CO-1011, which has no dates
 * on file at all) are left in place rather than guessed at.
 *
 * Also backfills the Carvergent Fee column for these older rows
 * (blank until now, since they predate that column existing).
 *
 * Run this ONCE, before running the Turo/Private/QuickBooks syncs
 * over full history — otherwise there's a brief window where a
 * historical booking exists only on the original tab, which the
 * duplicate-check still handles fine (it scans every tab), but it's
 * cleaner to migrate first.
 * ---------------------------------------------------------------
 */

function migrateExistingTrackerRowsToMonthTabs() {
  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const sourceSheet = ss.getSheets()[0];
  const data = sourceSheet.getDataRange().getValues();
  const headers = data[0];

  const dateOutIdx = headers.indexOf('Date Out');
  const dateDueIdx = headers.indexOf('Date Due');
  const vehicleIdx = headers.indexOf('Vehicle');
  const carvergentFeeIdx = headers.indexOf('Carvergent Fee');
  if (dateOutIdx === -1) throw new Error('Could not find a "Date Out" column — run restructureRentalTrackerColumns first if this is a fresh sheet.');

  const rowsToDelete = [];

  for (let r = 1; r < data.length; r++) {
    const row = data[r].slice(); // copy — about to mutate cells below
    const dateOut = coerceDate_(row[dateOutIdx]);
    const contractNumber = row[0];

    if (!dateOut) {
      Logger.log('Skipping ' + contractNumber + ' — no valid Date Out to determine its month tab; left in place for manual handling.');
      continue;
    }
    row[dateOutIdx] = dateOut; // normalize to a real Date on the migrated row
    if (dateDueIdx !== -1) {
      const dateDue = coerceDate_(row[dateDueIdx]);
      if (dateDue) row[dateDueIdx] = dateDue;
    }

    if (carvergentFeeIdx !== -1 && !row[carvergentFeeIdx] && vehicleIdx !== -1) {
      row[carvergentFeeIdx] = carvergentFee_(null, row[vehicleIdx], dateOut);
    }

    const monthSheet = getOrCreateMonthSheet_(ss, dateOut);
    if (monthSheet.getName() === sourceSheet.getName()) continue; // shouldn't happen, but don't self-delete if it does

    monthSheet.appendRow(row);
    rowsToDelete.push(r + 1); // 1-based row index in sourceSheet
    Logger.log('Migrated ' + contractNumber + ' to tab "' + monthSheet.getName() + '".');
  }

  // Delete bottom-up so earlier row indices don't shift mid-loop.
  rowsToDelete.sort(function (a, b) { return b - a; }).forEach(function (rowIndex) {
    sourceSheet.deleteRow(rowIndex);
  });

  Logger.log('Migration complete — moved ' + rowsToDelete.length + ' row(s) into monthly tabs; ' + (data.length - 1 - rowsToDelete.length) + ' left in place (no valid date).');
}

/**
 * One-time reshape of every tab in Rental_Agreements_Tracker to the
 * simplified ops-view schema Cristin asked for (2026-09-18):
 *   - Drops Day Rate and Total Due — revenue now lives in the WordPress
 *     ops platform; this sheet is Carvergent fee tracking + a team-facing
 *     "what's upcoming" view, not a revenue ledger.
 *   - Adds "Lockbox/Door Code" right after Vehicle, backfilled from
 *     Beverley_Fleet_Vehicle_Info by matching each row's Vehicle text.
 *   - Rewrites Status: "Fully Executed" -> "Confirmed", "Draft" ->
 *     "Awaiting Contract" (plain text for this historical backfill — new
 *     rows going forward get a live hyperlink to the Turo booking or the
 *     signed-contract email thread; see logBookingToTracker_ in Code.js).
 *   - Clears the auto-generated "Auto-added by calendar sync..." text out
 *     of Notes, leaving any real hand-written note (e.g. "Signed",
 *     "2nd driver Dean Threadgill") untouched.
 *
 * Run this ONCE, manually, from the Apps Script editor (Run > this
 * function) — same as migrateExistingTrackerRowsToMonthTabs above. It
 * can't go through clasp: clasp's login token only carries scopes for
 * managing the script project itself, not this project's own Sheets/Gmail
 * scopes, so it can't execute anything in the sheet.
 *
 * Supersedes restructureRentalTrackerColumns (PrivateBookings.js), which
 * only ever added the Carvergent Fee/Accessories columns — leave that one
 * alone, it's just historical record of that earlier setup step.
 */
function restructureTrackerForOpsView() {
  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const vehicles = loadVehicleLookup_();

  ss.getSheets().forEach(function (sheet) {
    reshapeTrackerSheet_(sheet, vehicles);
  });

  Logger.log('Done — reshaped ' + ss.getSheets().length + ' tab(s).');
}

function reshapeTrackerSheet_(sheet, vehicles) {
  let headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];

  // Highest index first so removing one doesn't shift the other's index.
  ['Total Due', 'Day Rate'].forEach(function (name) {
    const idx = headers.indexOf(name);
    if (idx !== -1) {
      sheet.deleteColumn(idx + 1);
      headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    }
  });

  let lockboxIdx = headers.indexOf('Lockbox/Door Code');
  if (lockboxIdx === -1) {
    const vehicleIdx = headers.indexOf('Vehicle');
    if (vehicleIdx === -1) throw new Error('Sheet "' + sheet.getName() + '" has no Vehicle column — check its header row.');
    sheet.insertColumnAfter(vehicleIdx + 1);
    lockboxIdx = vehicleIdx + 1;
    sheet.getRange(1, lockboxIdx + 1).setValue('Lockbox/Door Code');
    headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  }

  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return; // header only, nothing to backfill

  const data = sheet.getRange(2, 1, lastRow - 1, sheet.getLastColumn()).getValues();
  const vehicleIdx = headers.indexOf('Vehicle');
  const statusIdx = headers.indexOf('Status');
  const notesIdx = headers.indexOf('Notes');

  for (let r = 0; r < data.length; r++) {
    const row = data[r];
    const sheetRow = r + 2;

    if (vehicleIdx !== -1 && !row[lockboxIdx]) {
      const vehicle = matchVehicleByLabel_(String(row[vehicleIdx] || ''), vehicles);
      const info = lockboxInfoFor_(vehicle);
      if (info) sheet.getRange(sheetRow, lockboxIdx + 1).setValue(info);
    }

    if (statusIdx !== -1) {
      const status = String(row[statusIdx] || '').trim();
      if (status === 'Fully Executed') sheet.getRange(sheetRow, statusIdx + 1).setValue('Confirmed');
      else if (status === 'Draft') sheet.getRange(sheetRow, statusIdx + 1).setValue('Awaiting Contract');
    }

    if (notesIdx !== -1) {
      const notes = String(row[notesIdx] || '');
      if (notes.indexOf('Auto-added by calendar sync') === 0) {
        sheet.getRange(sheetRow, notesIdx + 1).setValue('');
      }
    }
  }

  Logger.log('Reshaped "' + sheet.getName() + '".');
}

// Matches a tracker row's free-text "Vehicle" cell (e.g. "2022 Audi Q5", or
// an older hand-written "Cristin's Audi (CO #EIUY09), Audi Q5 2022") against
// the fleet list by make/model — unlike matchVehicle_ (Code.js), which
// expects a parsed Turo email's "Make Model" + separate year, this doesn't
// assume the cell has any particular shape.
function matchVehicleByLabel_(label, vehicles) {
  const lower = label.toLowerCase();
  const match = vehicles.find(function (v) {
    return lower.indexOf(String(v.make).toLowerCase()) !== -1 &&
      lower.indexOf(String(v.model).split(' ')[0].split('-')[0].toLowerCase()) !== -1;
  });
  if (match) return match;

  // Model word missing from the label (e.g. "Mercedes Benz" with no
  // "Sprinter") — if the make alone is unambiguous in the fleet, use it.
  const byMakeOnly = vehicles.filter(function (v) { return lower.indexOf(String(v.make).toLowerCase()) !== -1; });
  return byMakeOnly.length === 1 ? byMakeOnly[0] : null;
}

/**
 * One-time backfill for a real gap found 2026-09-18: the 13 bookings
 * manually calendared straight from a Turo CSV export (before the tracker
 * even had a Carvergent Fee/Accessories column — titled "BOOKED — <Vehicle>
 * (Turo #<ID>)") had their source Gmail threads pre-labeled turo-calendared
 * at the time, specifically to stop the sync from re-calendaring them. That
 * label permanently excludes them from GMAIL_SEARCH too, though — so those
 * threads can NEVER reach logBookingToTracker_ on their own, and 11 of the
 * 13 (everything except Tom/"Thomas I." and Donald Richard R., which had no
 * findable source email and got backfilled separately once found) simply
 * never got a tracker row. Paul is one of these 11.
 *
 * This reads the calendar events directly instead of Gmail — they're the
 * one place this information still exists — and logs each one to the
 * tracker exactly like the live syncs do, without touching the calendar at
 * all (the event already exists; only Tracker was missing it).
 * logBookingToTracker_'s own duplicate check makes this safe to re-run.
 *
 * MUST run AFTER restructureTrackerForOpsView — it appends rows in that
 * function's column order (with Lockbox/Door Code), so running this first
 * against the old schema would misalign every column.
 *
 * Run this ONCE, manually, same as the other functions in this file.
 */
function backfillTrackerFromManualCalendarEvents() {
  const vehicles = loadVehicleLookup_();
  const cal = CalendarApp.getDefaultCalendar();
  // Covers the full span these events were ever created across (Jul 2026
  // trips through Dec 2026 bookings made that far ahead) with headroom.
  const events = cal.getEvents(new Date(2026, 0, 1), new Date(2027, 11, 31));

  let checked = 0;
  events.forEach(function (event) {
    const titleMatch = event.getTitle().match(/^BOOKED — (.+) \(Turo #(\d+)\)$/);
    if (!titleMatch) return;
    checked++;

    const desc = event.getDescription() || '';
    const guestMatch = desc.match(/Guest:\s*(.+)/);

    const booking = {
      guestName: guestMatch ? guestMatch[1].trim() : 'Unknown',
      guestPhone: '',
      start: event.getStartTime(),
      end: event.getEndTime(),
      reservationId: titleMatch[2],
      vehicleText: titleMatch[1]
    };

    const vehicle = matchVehicleByLabel_(titleMatch[1], vehicles);
    logBookingToTracker_(booking, vehicle, 'Turo', { specialRequests: '' });
  });

  Logger.log('Checked ' + checked + ' "BOOKED —" calendar event(s) — logBookingToTracker_ skipped any already tracked, and logged the rest.');
}

