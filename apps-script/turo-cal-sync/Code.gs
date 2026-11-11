/**
 * TuroCalendarSync.gs
 * ---------------------------------------------------------------
 * Realtime-ish job: scans info@beverleyfleet.com's Gmail for new
 * Turo "upcoming trip" notification emails, matches the vehicle
 * against the Beverley_Fleet_Vehicle_Info sheet, and creates a
 * Google Calendar event with the vehicle nickname + plate —
 * shared with info@beverleyfleet.com and vkallday1996@icloud.com.
 *
 * SETUP (one time, required after any push from clasp):
 *   1. Open this project at script.google.com.
 *   2. Pick `installRealtimeTrigger` from the function dropdown
 *      and click Run. Approve the Gmail + Calendar + Sheets
 *      permissions when prompted — this manual click is a Google
 *      security requirement for any brand-new trigger and cannot
 *      be done via clasp.
 *   3. That's it — it now checks for new Turo emails every 5
 *      minutes on its own. To test it immediately instead of
 *      waiting, run `syncTuroBookingsToCalendar` directly.
 *
 * NOTE ON "REAL-TIME": Apps Script has no true push trigger for
 * Gmail — the closest to instant without leaving Apps Script is a
 * time-based trigger polling every 1 minute (Google's own minimum
 * granularity), so this uses 5 minutes as a safer default that
 * won't bump into execution-quota limits. True push (event fires
 * within seconds of the email landing) is possible but requires a
 * separate Google Cloud Pub/Sub setup with Gmail's watch() API and
 * a renewal job every 7 days — meaningfully more infrastructure for
 * a few minutes of latency. Say the word if you want that instead.
 *
 * IMPORTANT: this must be set up under the Google account that
 * actually receives the Turo emails (info@beverleyfleet.com), since
 * Apps Script reads Gmail/Calendar for whichever account owns the
 * script project.
 * ---------------------------------------------------------------
 */

// ====================== CONFIG ======================

const VEHICLE_SHEET_ID = '1ve7L683k5j_TOWL93BnreU7Av2QXyf605POCXV8kec0'; // Beverley_Fleet_Vehicle_Info
const SHARE_WITH = ['info@beverleyfleet.com', 'vkallday1996@icloud.com'];
// NOTE: deliberately no "from:" filter. Turo's "upcoming trip" emails used
// to arrive as noreply@mail.turo.com, but as of Aug 2026 they're showing up
// in this mailbox re-sent from info@beverleyfleet.com (something upstream
// — a forward or relay — rewrites the From header). Subject text has stayed
// stable across both, so match on that instead of a sender we can't rely on.
// No date bound — deliberately searches full history every run. Safe
// because already-processed threads stay excluded by the label below, so
// this never redoes work, it just also catches anything older that was
// never picked up (e.g. a full history backfill).
// Matches both Turo email types: "X's trip is booked!" (fires immediately
// on booking) and "X has an upcoming trip..." (fires shortly before the
// trip starts). Relying on "upcoming trip" alone meant bookings made for
// dates further out never showed up until close to departure — this was
// the actual cause of "missing September bookings" (already-booked trips
// with no reminder email yet). Both email types use the same parseable
// body format, so no parser changes needed — but the same reservation
// will now arrive as two separate Gmail threads at different times, so
// the duplicate check below runs BEFORE creating a calendar event, not
// just before logging to the tracker, to avoid two events for one booking.
// FIX (2026-09-18): the old "trip is booked" clause was a no-op — Turo's
// real subject is "X's trip WITH YOUR VEHICLE is booked!", so the words
// "trip" and "is booked" are never adjacent and the exact-phrase match
// never fired. This is the actual root cause of bookings going missing —
// only the (correctly contiguous) "upcoming trip" reminder ever matched,
// and that fires shortly before the trip, not at booking time. Matching on
// "is booked!" (Turo's literal, stable subject ending) instead fixes it.
const GMAIL_SEARCH = '(subject:"upcoming trip" OR subject:"is booked!") -label:turo-calendared';
const PROCESSED_LABEL_NAME = 'turo-calendared';
const TIMEZONE = 'America/Denver';

// =====================================================

function syncTuroBookingsToCalendar() {
  // Nothing here locks a thread until it's fully processed and labeled, so
  // two overlapping runs (a slow run still going when the next 5-minute
  // trigger fires) would both see the same unlabeled threads and could
  // double-book. This lock makes the second run back off instead.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('Another sync run is already in progress — skipping this run.');
    return;
  }
  try {
    syncTuroBookingsToCalendar_();
  } finally {
    lock.releaseLock();
  }
}

function syncTuroBookingsToCalendar_() {
  const vehicles = loadVehicleLookup_();
  const processedLabel = getOrCreateLabel_(PROCESSED_LABEL_NAME);
  const threads = GmailApp.search(GMAIL_SEARCH);

  Logger.log('Found ' + threads.length + ' unprocessed Turo trip emails.');

  threads.forEach(function (thread) {
    const messages = thread.getMessages();
    const msg = messages[messages.length - 1];
    const body = msg.getPlainBody();

    const booking = parseTuroBooking_(body);
    if (!booking) {
      Logger.log('Could not parse booking from message: "' + msg.getSubject() + '" — skipping, left unlabeled for manual review.');
      return;
    }

    const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
    if (findDuplicateBookingRow_(ss, booking, 'Turo')) {
      Logger.log('Already tracked — skipping calendar event for ' + booking.guestName + ' (this is likely the "trip is booked" or "upcoming trip" counterpart of a reservation already processed).');
      thread.addLabel(processedLabel);
      return;
    }

    const vehicle = matchVehicle_(booking.vehicleText, booking.vehicleYear, vehicles);
    const vehicleLabel = vehicle ? (vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model) : (booking.vehicleText + ' ' + booking.vehicleYear);

    // Same car, same dates, already tracked as Private — the guest most
    // likely converted from Turo to a direct arrangement; don't add a
    // second (Turo) entry for what's now a private booking.
    if (findSameVehicleDateConflict_(ss, booking, vehicleLabel, 'Private')) {
      Logger.log('An existing Private row already covers this vehicle/dates for ' + booking.guestName + ' — skipping, already tracked as private.');
      thread.addLabel(processedLabel);
      return;
    }

    // Tracker first, then calendar. A trip that's already fully over (end
    // date in the past — e.g. backfilling months-old "is booked!" emails
    // that predate this fix) still gets logged for the record, but does
    // NOT get a calendar event; only current/upcoming bookings do.
    const accessories = accessoriesFromText_(body);
    logBookingToTracker_(booking, vehicle, 'Turo', { rawBody: body, accessories: accessories });
    if (isBookingOver_(booking)) {
      Logger.log('Trip already ended for ' + booking.guestName + ' — logged to tracker only, no calendar event created.');
    } else {
      createCalendarEvent_(booking, vehicle, 'Turo', accessories);
      // Also blocks these dates in the WordPress booking engine itself
      // (not just a personal calendar entry) — see WordPressSync.js —
      // so a guest can't book one of these vans online for the same dates.
      pushBookingToWordPress_(booking, vehicle, 'turo');
    }

    thread.addLabel(processedLabel);
  });
}

function isBookingOver_(booking) {
  return booking.end instanceof Date && booking.end.getTime() < Date.now();
}

/**
 * Parses one Turo "upcoming trip" email body into structured fields.
 * Turo's format (as of 2026) looks like:
 *
 *   <Guest> has a trip with your <Vehicle> ...
 *   <Make Model> <Year>
 *   booked by <Guest>
 *   Trip start: 7/23/26 11:00 am
 *   Trip end: 7/26/26 9:30 am
 *   ...
 *   <Guest>
 *       (555) 555-5555
 *   Reservation ID #12345678
 */
function parseTuroBooking_(body) {
  const vehicleMatch = body.match(/\n\s*([A-Za-z0-9\- ]+?)\s+(\d{4})\s*\n\s*booked by\s+(.+)/i);
  const startMatch = body.match(/Trip start:\s*([\d/]+\s+[\d:]+\s*[ap]m)/i);
  const endMatch = body.match(/Trip end:\s*([\d/]+\s+[\d:]+\s*[ap]m)/i);
  const reservationMatch = body.match(/Reservation ID #(\d+)/i);
  const phoneMatch = body.match(/\((\d{3})\)\s*(\d{3})-(\d{4})/);
  const earnMatch = body.match(/You earn:\s*\$([\d,]+\.\d{2})/i);

  if (!vehicleMatch || !startMatch || !endMatch) return null;

  const guestRaw = vehicleMatch[3].trim();

  return {
    vehicleText: vehicleMatch[1].trim(),
    vehicleYear: vehicleMatch[2].trim(),
    guestName: cleanGuestName_(guestRaw),
    start: parseTuroDate_(startMatch[1]),
    end: parseTuroDate_(endMatch[1]),
    reservationId: reservationMatch ? reservationMatch[1] : '',
    guestPhone: phoneMatch ? phoneMatch[0] : '',
    // Host payout after Turo's commission — NOT the guest-charged total.
    // See logBookingToTracker_ for why this doesn't go in "Total Due".
    hostPayout: earnMatch ? earnMatch[1] : ''
  };
}

// Turo sometimes sends names as "Last,first" — flip those back to
// "First Last" and title-case each part (Turo sends the raw form
// lowercase, e.g. "Clifford,albert" -> "Albert Clifford").
function cleanGuestName_(raw) {
  const titleCase = function (s) {
    return s.replace(/\w\S*/g, function (word) {
      return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    });
  };
  if (raw.indexOf(',') !== -1) {
    const parts = raw.split(',').map(function (s) { return s.trim(); });
    if (parts.length === 2) return titleCase(parts[1] + ' ' + parts[0]);
  }
  return raw;
}

// Turo date format: "7/23/26 11:00 am"
function parseTuroDate_(text) {
  const m = text.match(/(\d{1,2})\/(\d{1,2})\/(\d{2,4})\s+(\d{1,2}):(\d{2})\s*(am|pm)/i);
  if (!m) return null;
  let [, month, day, year, hour, minute, ampm] = m;
  year = year.length === 2 ? '20' + year : year;
  hour = parseInt(hour, 10);
  if (ampm.toLowerCase() === 'pm' && hour !== 12) hour += 12;
  if (ampm.toLowerCase() === 'am' && hour === 12) hour = 0;
  return new Date(parseInt(year, 10), parseInt(month, 10) - 1, parseInt(day, 10), hour, parseInt(minute, 10));
}

// Loads the fleet sheet, skipping the non-rentable Toyota Tacoma.
function loadVehicleLookup_() {
  const sheet = SpreadsheetApp.openById(VEHICLE_SHEET_ID).getSheets()[0];
  const data = sheet.getDataRange().getValues();
  const headers = data[0];

  const col = function (name) { return headers.indexOf(name); };
  const yearIdx = col('Year'), makeIdx = col('Make'), modelIdx = col('Model'),
    nameIdx = col('Name'), plateIdx = col('Lic Plate'), unitIdx = col('Unit #'),
    checkInIdx = col('Check In'), codeIdx = col('Code');

  return data.slice(1)
    .filter(function (row) { return String(row[modelIdx]).indexOf('Tacoma') === -1; }) // not a rental
    .map(function (row) {
      return {
        year: String(row[yearIdx]),
        make: row[makeIdx],
        model: row[modelIdx],
        name: row[nameIdx],
        plate: row[plateIdx],
        unit: row[unitIdx],
        checkInMethod: checkInIdx !== -1 ? row[checkInIdx] : '',
        code: codeIdx !== -1 ? String(row[codeIdx] || '').trim() : ''
      };
    });
}

// Standardized calendar title (2026-09-18): car, name, pickup/dropoff —
// nothing else. Cristin's exact complaint was that titles were "all over
// the place" (some had "(Turo)"/"(Private)", some didn't, vehicle naming
// varied) — this is the one format everything uses now.
function eventTitle_(vehicleLabel, guestName, start, end) {
  const fmt = function (d) { return Utilities.formatDate(d, TIMEZONE, 'M/d h:mm a'); };
  return vehicleLabel + ' — ' + guestName + ' — Pickup ' + fmt(start) + ' / Dropoff ' + fmt(end);
}

// "Lockbox 1067", "Door Code 46535" — or, for the three vehicles (Joker/
// 2024, Heaps/2025, the Sprinter) getting a lockbox installed 2026-09-19,
// "Lockbox (code coming Sat 9/19)" ahead of the actual code. Cristin
// confirmed 2026-09-18 these three are moving off Remote Unlock onto
// lockboxes, not just adding a code to the existing remote system — once
// she updates Beverley_Fleet_Vehicle_Info's Check In/Code columns with the
// real codes, this reads through automatically and this override can go.
// Shared by the calendar description and the tracker's "Lockbox/Door
// Code" column.
function lockboxInfoFor_(vehicle) {
  if (!vehicle || !vehicle.checkInMethod || vehicle.checkInMethod === 'N/A') return '';
  if (vehicle.code) return vehicle.checkInMethod + ' ' + vehicle.code;
  if (vehicle.checkInMethod === 'Remote Unlock') return 'Lockbox (code coming Sat 9/19)';
  return vehicle.checkInMethod + ' (code coming Sat 9/19)';
}

// Matches "Ford Transit" + "2025" (from the email) against the fleet sheet
// by year + make/model text overlap. Currently each fleet vehicle has a
// unique year, so year alone is close to sufficient — make/model is a
// safety check. If you ever add two vehicles of the same year, tighten
// this by also comparing model trim.
function matchVehicle_(vehicleText, year, vehicles) {
  const lower = (vehicleText || '').toLowerCase();
  const candidates = year ? vehicles.filter(function (v) { return v.year === String(year); }) : vehicles;
  const match = candidates.find(function (v) {
    return lower.indexOf(String(v.make).toLowerCase()) !== -1 &&
      lower.indexOf(String(v.model).split(' ')[0].split('-')[0].toLowerCase()) !== -1;
  });
  if (match) return match;

  // Model word missing from the text (e.g. a contract just says "Mercedes
  // Benz", not "Sprinter" — this is what left Luke Kuepfer's row with a
  // blank Lockbox/Door Code on 2026-09-18). If the make alone identifies
  // exactly one candidate, that's unambiguous — use it instead of falling
  // through to "first candidate of this year" below, which silently picks
  // the wrong vehicle whenever more than one shares a year.
  const byMakeOnly = candidates.filter(function (v) { return lower.indexOf(String(v.make).toLowerCase()) !== -1; });
  if (byMakeOnly.length === 1) return byMakeOnly[0];

  return (year ? candidates[0] : null) || null; // fall back to year-only match if model text differs
}

// sourceLabel: 'Turo' or 'Private' — controls how the reservation/contract
// reference line is worded in the description (source itself no longer
// appears in the title — Cristin wants titles standardized to just car,
// name, pickup/dropoff, see eventTitle_).
// accessories: same string logBookingToTracker_ puts in the tracker's
// Accessories column (car seats/prepaid fuel/unlimited miles only — see
// accessoriesFromText_), kept identical between the sheet and this event
// rather than computed twice.
function createCalendarEvent_(booking, vehicle, sourceLabel, accessories) {
  sourceLabel = sourceLabel || 'Turo';
  const cal = CalendarApp.getDefaultCalendar();

  const vehicleLabel = vehicle ? vehicle.name || (vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model) : 'UNMATCHED VEHICLE';
  const title = eventTitle_(vehicleLabel, booking.guestName, booking.start, booking.end);
  const desc = buildEventDescription_(booking, vehicle, sourceLabel, accessories);

  const event = cal.createEvent(title, booking.start, booking.end, { description: desc });

  SHARE_WITH.forEach(function (email) {
    try {
      event.addGuest(email);
    } catch (e) {
      Logger.log('Could not add guest ' + email + ': ' + e);
    }
  });
  return event;
}

function buildEventDescription_(booking, vehicle, sourceLabel, accessories) {
  let desc = 'Source: ' + sourceLabel + ' booking (auto-synced)\n';
  desc += 'Guest: ' + booking.guestName + (booking.guestPhone ? ' — ' + booking.guestPhone : '') + '\n';
  if (vehicle) {
    desc += 'Vehicle: ' + vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model + ' (' + vehicle.name + ')\n';
    desc += 'Plate: ' + (vehicle.plate || '[not on file yet]') + '\n';
    desc += 'Unit #: ' + vehicle.unit + '\n';
    const access = lockboxInfoFor_(vehicle);
    if (access) desc += 'Access: ' + access + '\n';
  } else {
    desc += 'Vehicle: could not auto-match — raw text was "' + booking.vehicleText + ' ' + booking.vehicleYear + '". Check manually.\n';
  }
  if (accessories) desc += 'Accessories: ' + accessories + '\n';
  if (booking.reservationId) {
    desc += sourceLabel === 'Turo'
      ? 'Turo reservation: https://turo.com/reservation/' + booking.reservationId + '\n'
      : 'Contract #: ' + booking.reservationId + '\n';
  }
  return desc;
}

// Retitles/redescribes an existing event in place — never creates a second
// one — for when a booking's source/vehicle/accessories change after its
// event already exists (e.g. a Turo booking that turns out to have a signed
// private contract too; see the knownSource === 'Turo' branch in
// PrivateBookings.js).
function updateCalendarEventForBooking_(event, booking, vehicle, sourceLabel, accessories) {
  const vehicleLabel = vehicle ? vehicle.name || (vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model) : 'UNMATCHED VEHICLE';
  event.setTitle(eventTitle_(vehicleLabel, booking.guestName, booking.start, booking.end));
  event.setDescription(buildEventDescription_(booking, vehicle, sourceLabel, accessories));
}

// Finds a guest's calendar event by first name within their own booking
// window — used to check whether a tracker row that's about to be edited
// (not created) already has a calendar event, since the tracker doesn't
// store an event ID to look one up directly.
function findCalendarEventForGuest_(guestName, start, end) {
  if (!guestName || !start || !end) return null;
  const firstName = String(guestName).toLowerCase().split(' ')[0];
  if (!firstName) return null;
  const events = CalendarApp.getDefaultCalendar().getEvents(start, end);
  return events.find(function (e) { return e.getTitle().toLowerCase().indexOf(firstName) !== -1; }) || null;
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

// ====================== TRIGGER SETUP ======================

function installRealtimeTrigger() {
  installEveryFiveMinutes_('syncTuroBookingsToCalendar');
  installEveryFiveMinutes_('syncGuestStatusNotes');
  installEveryFiveMinutes_('syncTuroCancellations');
  installEveryFiveMinutes_('syncTuroTripChanges');
  Logger.log('Triggers installed — checking for new Turo emails, cancellations, trip changes, and guest messages every 5 minutes.');
}

// Clears any existing trigger for this function first (so re-running this
// doesn't create duplicates or leave two triggers on the same function),
// then installs a fresh 5-minute one. Safe to call repeatedly.
function installEveryFiveMinutes_(functionName) {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === functionName) {
      ScriptApp.deleteTrigger(t);
    }
  });

  ScriptApp.newTrigger(functionName)
    .timeBased()
    .everyMinutes(5)
    .create();
}

// ====================== RENTAL_AGREEMENTS_TRACKER LOGGING ======================
// Shared by both the Turo and Private booking syncs (see PrivateBookings.js,
// which declares TRACKER_SHEET_ID). Every auto-calendared booking — Turo or
// Private — also gets a row appended here, so the tracker stops needing to
// be maintained by hand for anything the calendar automation already knows
// about.
//
// ONE-TIME SETUP: run `restructureRentalTrackerColumns` once (see
// PrivateBookings.js) before this writes anywhere, to add the "Carvergent
// Fee" and "Accessories" columns this expects.

// Carvergent fee reconciliation only applies going forward from 9/15/2026
// (Cristin: "we did a flat monthly until 9/15" — before that date Carvergent
// was billed a flat monthly amount unrelated to individual bookings, so no
// per-booking figure belongs in this column at all). Bookings starting
// before 9/15/2026 are left blank. From 9/15/2026 on, it's a flat
// per-vehicle-type fee: $40 for the Q5/Suburban, $70 for the vans
// (Transit/Sprinter/Mercedes).
const CARVERGENT_RECONCILE_START = new Date(2026, 8, 15); // Sep 15, 2026

function carvergentFee_(vehicle, vehicleText, bookingStart) {
  if (bookingStart instanceof Date && bookingStart < CARVERGENT_RECONCILE_START) return ''; // before September — not reconciled
  const text = (vehicle ? (vehicle.make + ' ' + vehicle.model) : (vehicleText || '')).toLowerCase();
  if (text.indexOf('q5') !== -1 || text.indexOf('suburban') !== -1) return 40;
  // 'mercedes' alone counts as a van match too — the fleet's only Mercedes
  // is the Sprinter, but a private contract's Vehicle text sometimes just
  // says "Mercedes Benz" without "Sprinter" (missed Luke Kuepfer's fee for
  // exactly this reason on the 2026-09-18 backfill).
  if (text.indexOf('transit') !== -1 || text.indexOf('sprinter') !== -1 || text.indexOf('mercedes') !== -1) return 70;
  return '';
}

// Sheet cells from hand-entered rows are sometimes plain text (e.g.
// "8/4/2026 4:30 PM" typed or pasted in rather than a real Sheets date
// value), which getValues() returns as a string, not a Date — silently
// failing any `instanceof Date` check. This coerces either into a real
// Date, or returns null if it truly can't be parsed.
function coerceDate_(value) {
  if (value instanceof Date) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = new Date(value);
    if (!isNaN(parsed.getTime())) return parsed;
  }
  return null;
}

// Returns the tab for a booking's month (e.g. "Sep 2026"), creating it by
// copying the header row from the spreadsheet's first tab if it doesn't
// exist yet. Tab is chosen by the booking's start date (Date Out).
function getOrCreateMonthSheet_(ss, date) {
  const tabName = Utilities.formatDate(date, TIMEZONE, 'MMM yyyy');
  let sheet = ss.getSheetByName(tabName);
  if (sheet) return sheet;

  const template = ss.getSheets()[0];
  const headerWidth = template.getLastColumn();
  sheet = ss.insertSheet(tabName);
  sheet.getRange(1, 1, 1, headerWidth).setValues(template.getRange(1, 1, 1, headerWidth).getValues());
  return sheet;
}

// Ops-view schema (2026-09-18): this sheet is team-facing scheduling +
// Carvergent fee tracking, not a revenue ledger — Day Rate/Total Due moved
// to the WordPress ops platform and are no longer tracked here at all.
//
// Deposit Paid / Fully Paid are set to 'N/A' for Turo — Turo collects
// payment on its own platform, so nothing ever flows through Beverley
// Fleet's QuickBooks for those, and the QuickBooks sync should never touch
// them. Private bookings are left blank until the QuickBooks sync fills
// them in from actual payment emails.
//
// Status is always "Confirmed" from live automation (both syncs only ever
// log a booking once it's actually booked/signed — see the "SAFETY DESIGN"
// note in PrivateBookings.js), hyperlinked to the Turo reservation or the
// signed-contract email thread. "Awaiting Contract" only applies to
// historical rows entered by hand before a contract was signed.
//
// Accessories/Special Requests are scanned for two specific things only —
// a child seat or prepaid fuel (Accessories), and an explicit ask made at
// booking time like "requested terminal delivery" (Special Requests) —
// never fees or boilerplate. Most of the time these come back blank;
// that's expected, not a bug. Cristin's plan is to pipe RingCentral
// call/text data in later to actually catch verbally-confirmed requests,
// which this can't see.
//
// Notes (2026-09-18: split into its own column, after Special Requests) is
// never set here — it's always blank on a brand-new row. It only ever gets
// filled in later, by syncGuestStatusNotes_ (GuestMessages.js) reporting a
// live in-trip update (a delay, a changed drop-off, etc.) on a booking
// that's already tracked. Special Requests captures what was asked for
// when the deal was made; Notes captures what's happening during the trip.
//
// extra: { rawBody, accessories, specialRequests, sourceLink } — Turo
// passes rawBody and lets this scan it; Private (PrivateBookings.js)
// already has Claude's structured answer, so it passes
// accessories/specialRequests/sourceLink directly.
function logBookingToTracker_(booking, vehicle, sourceLabel, extra) {
  extra = extra || {};
  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const vehicleLabel = vehicle
    ? (vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model)
    : (booking.vehicleText + ' ' + booking.vehicleYear);

  // Guard against double-logging a booking that's already in the tracker —
  // matters most when backfilling full history, since old bookings that
  // were already manually entered (CO-1005..CO-1027b) would otherwise get
  // a second, duplicate row the first time this processes their email.
  if (findDuplicateBookingRow_(ss, booking, sourceLabel)) {
    Logger.log('Skipping tracker row for ' + booking.guestName + ' (' + vehicleLabel + ') — already tracked.');
    return;
  }

  const sheet = getOrCreateMonthSheet_(ss, booking.start);
  const isTuro = sourceLabel === 'Turo';

  const accessories = extra.accessories !== undefined ? extra.accessories : accessoriesFromText_(extra.rawBody);
  const specialRequests = extra.specialRequests !== undefined ? extra.specialRequests : specialRequestFromText_(extra.rawBody);
  const sourceLink = extra.sourceLink || (isTuro && booking.reservationId ? 'https://turo.com/reservation/' + booking.reservationId : '');
  const status = sourceLink ? '=HYPERLINK("' + sourceLink + '","Confirmed")' : 'Confirmed';

  insertRowSortedByDate_(sheet, {
    'Contract #': booking.reservationId || '',
    'Name': booking.guestName || '',
    'Phone': booking.guestPhone || '',
    'Vehicle': vehicleLabel,
    'Lockbox/Door Code': lockboxInfoFor_(vehicle),
    'Date Out': booking.start,
    'Date Due': booking.end,
    'Deposit Paid': isTuro ? 'N/A' : '',
    'Fully Paid': isTuro ? 'N/A' : '',
    'Status': status,
    'Source': sourceLabel,
    'Carvergent Fee': carvergentFee_(vehicle, booking.vehicleText, booking.start),
    'Accessories': accessories,
    'Special Requests': specialRequests,
    'Notes': ''
  });
}

// Keeps each month tab sorted by Date Out at all times, rather than letting
// new bookings just pile up at the bottom in whatever order their source
// emails happened to arrive — Cristin wants the sheet always readable as a
// chronological schedule, including as new bookings land.
//
// `values` is keyed by header name, not column position — this tab's own
// header row decides where each value lands, the same way setRowValue_/
// getRowValue_ (QuickBooksSync.js) already do for updates to existing rows.
// A hardcoded column array here would silently misalign the moment any one
// tab's column order differs from another's (a manually inserted/reordered
// column, a tab reshaped at a different time than the others, etc.).
function insertRowSortedByDate_(sheet, values) {
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const row = headers.map(function (h) { return values.hasOwnProperty(h) ? values[h] : ''; });

  const dateOutCol = headers.indexOf('Date Out') + 1; // 1-based; 0 if this tab has no such column
  const newDate = dateOutCol && values['Date Out'] instanceof Date ? values['Date Out'].getTime() : null;
  const lastRow = sheet.getLastRow();

  if (newDate === null || lastRow < 2) {
    sheet.appendRow(row);
    return;
  }

  const existingDates = sheet.getRange(2, dateOutCol, lastRow - 1, 1).getValues();
  let insertAt = null;
  for (let i = 0; i < existingDates.length; i++) {
    const existing = coerceDate_(existingDates[i][0]);
    if (existing && existing.getTime() > newDate) {
      insertAt = i + 2; // 1-based sheet row (data starts at row 2)
      break;
    }
  }

  if (insertAt === null) {
    sheet.appendRow(row);
  } else {
    sheet.insertRowBefore(insertAt);
    sheet.getRange(insertAt, 1, 1, row.length).setValues([row]);
  }
}

// Only ever flags what's actually an accessory — car seats (with count and
// size/facing when stated), prepaid fuel, unlimited miles — never a dollar
// amount and never a free-text note. Blank whenever none of these three
// are clearly mentioned; that's the expected common case, not a bug.
function accessoriesFromText_(text) {
  if (!text) return '';
  const lower = text.toLowerCase();
  const found = [];

  const seatMatch = lower.match(/(\d+)\s*(?:x\s*)?(?:child|car)\s*seats?/) || lower.match(/(?:child|car)\s*seats?/);
  if (seatMatch) {
    const count = seatMatch[1] && /^\d+$/.test(seatMatch[1]) ? parseInt(seatMatch[1], 10) : 1;
    const sizeMatch = lower.match(/(infant|toddler|booster|forward-facing|rear-facing)/);
    let label = count + ' car seat' + (count !== 1 ? 's' : '');
    if (sizeMatch) label += ' (' + sizeMatch[1] + ')';
    found.push(label);
  }
  if (lower.indexOf('prepaid') !== -1 && (lower.indexOf('fuel') !== -1 || lower.indexOf('refuel') !== -1)) found.push('Prepaid fuel');
  if (lower.indexOf('unlimited mile') !== -1) found.push('Unlimited miles');

  return found.join(', ');
}

// Only catches an explicit terminal-delivery request today. Most special
// requests come through calls/texts this can't read — see the note above
// logBookingToTracker_ on the planned RingCentral integration.
function specialRequestFromText_(text) {
  if (!text) return '';
  const lower = text.toLowerCase();
  if (lower.indexOf('terminal delivery') !== -1 || (lower.indexOf('terminal') !== -1 && lower.indexOf('deliver') !== -1)) {
    return 'Requested terminal delivery';
  }
  return '';
}
