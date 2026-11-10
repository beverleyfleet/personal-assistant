/**
 * TripChanges.js
 * ---------------------------------------------------------------
 * Keeps calendar events in step when a guest changes a Turo trip.
 *
 * Turo's change email ("<Guest> has changed their trip with your
 * <Vehicle> (<Reservation ID>)") carries the same body layout as the
 * booking email: "<Make Model> <Year> / booked by <Guest> / Trip start:
 * 2/23/27 7:30 AM / Trip end: 2/26/27 7:30 AM / Reservation ID #NNNN".
 * Those are the trip's full NEW times (with the year), so this reuses
 * parseTuroBooking_ (Code.js) rather than reading the "New trip start
 * on Tue, Feb 23" summary lines, which have no year.
 *
 * On each unprocessed change email it:
 *   - moves the reservation's calendar event to the new times and
 *     rebuilds its title/description (same format as new bookings),
 *   - creates the event instead if none exists yet and the trip isn't
 *     over (a booking email that was missed, e.g. one that only went to
 *     another inbox),
 *   - updates Date Out / Date Due on the tracker row and notes the change,
 *   - re-pushes the booking to WordPress with the new dates.
 *
 * Why WordPress is re-pushed, not canceled and re-pushed: it's unknown
 * whether /bf/v1/import updates an existing ref or adds a second block.
 * Pushing alone is the safe failure — at worst the old dates stay blocked
 * too; canceling first could leave the new dates unblocked if the import
 * ignores a ref it has already seen.
 *
 * Emails are processed oldest first so the latest change wins when a guest
 * changes a trip more than once.
 *
 * SETUP: run installRealtimeTrigger (Code.js) once from the editor to add
 * the 5-minute trigger for syncTuroTripChanges.
 * ---------------------------------------------------------------
 */

const CHANGE_GMAIL_SEARCH = 'subject:"has changed their trip" -label:turo-change-processed';
const CHANGE_PROCESSED_LABEL_NAME = 'turo-change-processed';

// Window searched for a reservation's existing event. Wide on purpose:
// guests book (and change or cancel) months ahead — a 120-day window is
// what let Lopez's Feb 19 event survive an Oct 22 cancellation.
const EVENT_SEARCH_BACK_DAYS = 30;
const EVENT_SEARCH_AHEAD_DAYS = 400;

function syncTuroTripChanges() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('Another sync run is already in progress — skipping this run.');
    return;
  }
  try {
    syncTuroTripChanges_();
  } finally {
    lock.releaseLock();
  }
}

function syncTuroTripChanges_() {
  const processedLabel = getOrCreateLabel_(CHANGE_PROCESSED_LABEL_NAME);
  const threads = GmailApp.search(CHANGE_GMAIL_SEARCH);
  Logger.log('Found ' + threads.length + ' unprocessed Turo trip-change email(s).');

  const vehicles = loadVehicleLookup_();

  // GmailApp.search returns newest first; apply oldest first so the most
  // recent change is the one that sticks.
  threads.slice().reverse().forEach(function (thread) {
    const handled = thread.getMessages().every(function (msg) {
      return applyTripChange_(msg, vehicles);
    });
    // Unparseable emails stay unlabeled so they're retried and can be
    // reviewed by hand; everything else is labeled even when there was
    // nothing to update (e.g. a trip that already ended).
    if (handled) thread.addLabel(processedLabel);
  });
}

// Returns false only when the email couldn't be parsed.
function applyTripChange_(msg, vehicles) {
  const subject = msg.getSubject();
  const body = msg.getPlainBody();

  const booking = parseTuroBooking_(body);
  if (!booking || !booking.reservationId || !booking.start || !booking.end) {
    Logger.log('Could not parse trip change from "' + subject + '" — skipping, left unlabeled for manual review.');
    return false;
  }
  if (booking.end <= booking.start) {
    Logger.log('Trip change "' + subject + '" has its end before its start — skipping, left unlabeled for manual review.');
    return false;
  }

  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const found = findTrackerRowByContract_(ss, String(booking.reservationId));

  // A trip can be changed and later canceled (Claudine, Nov 2026). The
  // change email is still unlabeled the first time this runs, so without
  // this check it would recreate the canceled trip's event.
  if (isReservationCanceled_(found, booking.reservationId)) {
    Logger.log('Reservation #' + booking.reservationId + ' was canceled after this change — leaving it alone.');
    return true;
  }

  const vehicle = matchVehicle_(booking.vehicleText, booking.vehicleYear, vehicles);
  const accessories = accessoriesFromText_(body);

  const event = findCalendarEventByReservationId_(booking.reservationId);
  if (event) {
    event.setTime(booking.start, booking.end);
    updateCalendarEventForBooking_(event, booking, vehicle, 'Turo', accessories);
    Logger.log('Moved the calendar event for Reservation #' + booking.reservationId + ' (' + booking.guestName + ') to the new trip times.');
  } else if (!isBookingOver_(booking)) {
    createCalendarEvent_(booking, vehicle, 'Turo', accessories);
    Logger.log('No event existed for Reservation #' + booking.reservationId + ' — created one from the change email.');
  } else {
    Logger.log('Reservation #' + booking.reservationId + ' has already ended and has no event — nothing to update.');
  }

  if (found) {
    // The row stays on its original month tab even if the new dates fall in
    // another month; Date Out/Date Due are what everything else reads.
    setRowValue_(found, 'Date Out', booking.start);
    setRowValue_(found, 'Date Due', booking.end);
    const existingNotes = getRowValue_(found, 'Notes');
    setRowValue_(found, 'Notes', (existingNotes ? existingNotes + ' | ' : '') + 'Changed — ' + subject);
  } else {
    Logger.log('No tracker row found for Reservation #' + booking.reservationId + ' — nothing to update there.');
  }

  if (!isBookingOver_(booking)) {
    pushBookingToWordPress_(booking, vehicle, 'turo');
  }
  return true;
}

// Canceled if the tracker row says so (set by syncTuroCancellations) or, in
// case that sync hasn't run yet, if a cancellation email mentions the
// reservation.
function isReservationCanceled_(trackerRow, reservationId) {
  if (trackerRow && String(getRowValue_(trackerRow, 'Status')).trim() === 'Canceled') return true;
  const cancelEmails = GmailApp.search('"' + reservationId + '" (subject:cancel OR subject:canceled OR subject:cancelled)');
  return cancelEmails.length > 0;
}

// The event description embeds "reservation/<id>" (see
// buildEventDescription_, Code.js). Shared with the cancellation sync
// (WordPressSync.js) so both search the same wide window.
function findCalendarEventByReservationId_(reservationId) {
  const now = Date.now();
  const day = 24 * 60 * 60 * 1000;
  const events = CalendarApp.getDefaultCalendar().getEvents(
    new Date(now - EVENT_SEARCH_BACK_DAYS * day),
    new Date(now + EVENT_SEARCH_AHEAD_DAYS * day)
  );
  const needle = 'reservation/' + reservationId;
  return events.find(function (e) { return (e.getDescription() || '').indexOf(needle) !== -1; }) || null;
}
