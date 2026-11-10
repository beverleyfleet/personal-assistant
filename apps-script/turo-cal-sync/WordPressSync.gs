/**
 * WordPressSync.js
 * ---------------------------------------------------------------
 * Bridges this Gmail/Calendar automation to the real booking engine
 * at beverleyfleet.com (the bf-booking-platform WordPress plugin),
 * so a Turo or private booking actually blocks those dates there —
 * not just on this Google Calendar — and a guest can't book one of
 * these vans online for dates Turo (or a private arrangement) has
 * already taken.
 *
 * Two directions:
 *   1. pushBookingToWordPress_() — called from syncTuroBookingsToCalendar_
 *      (Code.js) and syncPrivateBookingsToCalendar_ (PrivateBookings.js)
 *      right after a new booking is calendared, POSTing it to the
 *      plugin's existing /wp-json/bf/v1/import endpoint (the same one
 *      the Turo Bookings sheet script already used to call, per the
 *      settings page). Creates a "block" row there tagged turo/private.
 *   2. syncTuroCancellations() — a new poll, same 5-minute cadence as
 *      the booking sync, that watches for Turo's cancellation emails
 *      (guest-canceled, Turo-canceled, or host-canceled — all three
 *      have distinct subject wording but share "trip with your" +
 *      "cancel..." and the same "Reservation ID #NNNNNNNN" line the
 *      booking emails have) and, on a match:
 *        - marks that Reservation ID's Rental_Agreements_Tracker row
 *          Status "Canceled" (Notes gets a line too),
 *        - removes its Google Calendar event,
 *        - calls the plugin's new /wp-json/bf/v1/import-cancel endpoint
 *          so the dates free up in the actual availability engine too.
 *
 * SETUP (one time):
 *   1. In wp-admin -> BF Bookings -> Settings -> "Turo / private import",
 *      copy the Import key shown there (it already exists — the same key
 *      used to be for a sheet script that was never installed).
 *   2. In this Apps Script project: Project Settings -> Script Properties
 *      -> add WP_IMPORT_KEY with that value. (Never paste it into chat —
 *      same rule as every other credential in this project.)
 *   3. Run installRealtimeTrigger (Code.js) once from the Apps Script
 *      editor to pick up the new syncTuroCancellations trigger — this is
 *      the same one-time manual click any brand-new trigger needs.
 *   4. Run syncTuroCancellations directly once to test against real
 *      cancellation emails before trusting the 5-minute trigger with it.
 *
 * If WP_IMPORT_KEY isn't set, both functions log why and return — Turo
 * sync, the Tracker, and this calendar all keep working exactly as they
 * did before; only the WordPress side of things is skipped.
 *
 * The cancellation subject-line matching is a first pass built from six
 * real cancellation emails in this mailbox (three wordings: guest-
 * canceled, Turo-canceled, host-canceled). If a real cancellation ever
 * comes through with different wording and gets missed, forward it and
 * the match gets tightened — same iterative process that fixed the
 * booking-detection subject line on 2026-09-18 (see Code.js).
 * ---------------------------------------------------------------
 */

const WP_IMPORT_URL = 'https://beverleyfleet.com/wp-json/bf/v1/import';
const WP_IMPORT_CANCEL_URL = 'https://beverleyfleet.com/wp-json/bf/v1/import-cancel';

const CANCEL_GMAIL_SEARCH = '(subject:"trip with your") (subject:cancel OR subject:canceled OR subject:cancelled) -label:turo-cancel-processed';
const CANCEL_PROCESSED_LABEL_NAME = 'turo-cancel-processed';

/* ====================== PUSH: booking -> WordPress ====================== */

function pushBookingToWordPress_(booking, vehicle, source) {
  const key = PropertiesService.getScriptProperties().getProperty('WP_IMPORT_KEY');
  if (!key) {
    Logger.log('WP_IMPORT_KEY is not set in Script Properties — skipping the WordPress calendar push for ' + (booking.guestName || 'this booking') + '. See the setup notes at the top of WordPressSync.js. Tracker + this Google Calendar are unaffected.');
    return;
  }
  if (!booking.start || !booking.end || !booking.reservationId) {
    Logger.log('Booking for ' + (booking.guestName || 'unknown guest') + ' is missing start/end/reservationId — skipping the WordPress push (nothing to block reliably).');
    return;
  }

  const vehicleLabel = vehicle
    ? (vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model)
    : ((booking.vehicleText || '') + ' ' + (booking.vehicleYear || ''));

  const payload = {
    source: source, // 'turo' or 'private'
    rows: [{
      vehicle: vehicleLabel,
      start: Utilities.formatDate(booking.start, TIMEZONE, 'yyyy-MM-dd'),
      end: Utilities.formatDate(booking.end, TIMEZONE, 'yyyy-MM-dd'),
      guest: booking.guestName || '',
      phone: booking.guestPhone || '',
      ref: String(booking.reservationId),
    }],
  };

  const res = UrlFetchApp.fetch(WP_IMPORT_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-BF-Key': key },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const status = res.getResponseCode();
  if (status < 200 || status >= 300) {
    Logger.log('WordPress import failed (' + status + ') for Reservation #' + booking.reservationId + ': ' + res.getContentText().slice(0, 300));
    return;
  }
  const data = JSON.parse(res.getContentText() || '{}');
  if (data.skipped && data.skipped.length) {
    Logger.log('WordPress import skipped Reservation #' + booking.reservationId + ': ' + JSON.stringify(data.skipped));
  } else {
    Logger.log('WordPress calendar updated for Reservation #' + booking.reservationId + ' (' + source + ', ' + vehicleLabel + ').');
  }
}

/* ====================== CANCELLATIONS ====================== */

function syncTuroCancellations() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('Another sync run is already in progress — skipping this run.');
    return;
  }
  try {
    syncTuroCancellations_();
  } finally {
    lock.releaseLock();
  }
}

function syncTuroCancellations_() {
  const processedLabel = getOrCreateLabel_(CANCEL_PROCESSED_LABEL_NAME);
  const threads = GmailApp.search(CANCEL_GMAIL_SEARCH);
  Logger.log('Found ' + threads.length + ' unprocessed Turo cancellation email(s).');

  threads.forEach(function (thread) {
    const messages = thread.getMessages();
    const msg = messages[messages.length - 1];
    const body = msg.getPlainBody();
    const subject = msg.getSubject();

    const reservationMatch = body.match(/Reservation ID #(\d+)/i);
    if (!reservationMatch) {
      Logger.log('Could not find a Reservation ID in cancellation email "' + subject + '" — skipping, left unlabeled for manual review.');
      return;
    }
    const reservationId = reservationMatch[1];

    // Tracker: keep Status current for partners who still work off that
    // sheet (Cristin, 2026-09-19: "that is still what we are using with
    // our partners").
    const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
    const found = findTrackerRowByContract_(ss, reservationId);
    if (found) {
      setRowValue_(found, 'Status', 'Canceled');
      const existingNotes = getRowValue_(found, 'Notes');
      setRowValue_(found, 'Notes', (existingNotes ? existingNotes + ' | ' : '') + 'Canceled — ' + subject);
    } else {
      Logger.log('No tracker row found for Reservation ID #' + reservationId + ' — nothing to mark there (may predate this integration).');
    }

    removeCalendarEventByReservationId_(reservationId);
    cancelBookingInWordPress_(reservationId, 'turo', subject);

    thread.addLabel(processedLabel);
  });
}

// The calendar event's description embeds "reservation/<id>" (see
// buildEventDescription_, Code.js) — search current/upcoming events for
// that rather than trying to match by guest name a second time.
function removeCalendarEventByReservationId_(reservationId) {
  const now = new Date();
  const events = CalendarApp.getDefaultCalendar().getEvents(now, new Date(now.getTime() + 120 * 24 * 60 * 60 * 1000));
  const needle = 'reservation/' + reservationId;
  const match = events.find(function (e) { return (e.getDescription() || '').indexOf(needle) !== -1; });
  if (match) {
    match.deleteEvent();
    Logger.log('Removed the calendar event for canceled Reservation ID #' + reservationId + '.');
  } else {
    Logger.log('No upcoming calendar event found for canceled Reservation ID #' + reservationId + ' (already gone, or predates this integration).');
  }
}

function cancelBookingInWordPress_(reservationId, source, reasonNote) {
  const key = PropertiesService.getScriptProperties().getProperty('WP_IMPORT_KEY');
  if (!key) {
    Logger.log('WP_IMPORT_KEY is not set — skipping the WordPress cancellation for Reservation #' + reservationId + '.');
    return;
  }
  const res = UrlFetchApp.fetch(WP_IMPORT_CANCEL_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-BF-Key': key },
    payload: JSON.stringify({ source: source, ref: String(reservationId), reason: reasonNote || '' }),
    muteHttpExceptions: true,
  });
  const status = res.getResponseCode();
  if (status < 200 || status >= 300) {
    // A 404 just means this reservation was never pushed to WordPress in
    // the first place (e.g. booked before this integration existed) —
    // not a real error, so this stays a log line, not a thrown exception.
    Logger.log('WordPress cancel for Reservation #' + reservationId + ' returned ' + status + ': ' + res.getContentText().slice(0, 200));
    return;
  }
  Logger.log('WordPress booking canceled for Reservation #' + reservationId + '.');
}
