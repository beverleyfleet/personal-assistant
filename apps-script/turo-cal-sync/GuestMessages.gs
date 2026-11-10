/**
 * GuestMessages.js
 * ---------------------------------------------------------------
 * Turo relays a guest's in-app messages as separate notification
 * emails ("<Guest> has sent you a message about your <Vehicle>"),
 * one thread per message. A guest running late for pickup, asking
 * for a different drop location, or flagging a lockbox problem
 * shows up here in free text — no fixed subject/keyword pattern
 * covers it, so (like PrivateBookings.js) this asks Claude to judge
 * each message instead of pattern-matching it.
 *
 * Scope is deliberately narrow: this only ever OVERWRITES the
 * Notes cell of the guest's current/upcoming tracker row with a
 * one-line summary of the latest relevant update (Cristin's call —
 * a running log was the alternative, but latest-status-wins is
 * simpler to scan). It never touches Status, dates, or any other
 * column, and it never creates a row — if there's no matching
 * tracker row yet, it leaves the thread unlabeled to retry once the
 * booking sync has caught up.
 *
 * SETUP: uses the same ANTHROPIC_API_KEY script property as
 * PrivateBookings.js — nothing extra to configure. Re-run
 * `installRealtimeTrigger` (Code.js) once to pick up this sync's
 * trigger; it's additive and won't disturb the existing Turo one.
 * ---------------------------------------------------------------
 */

// ====================== CONFIG ======================

// Contiguous phrase, matches Turo's actual subject verbatim — see the
// GMAIL_SEARCH note in Code.js for why that matters.
const GUEST_MESSAGE_GMAIL_SEARCH = 'subject:"has sent you a message about your" -label:guest-message-processed';
const GUEST_MESSAGE_PROCESSED_LABEL = 'guest-message-processed';

// =====================================================

function syncGuestStatusNotes() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('Another sync run is already in progress — skipping this run.');
    return;
  }
  try {
    syncGuestStatusNotes_();
  } finally {
    lock.releaseLock();
  }
}

function syncGuestStatusNotes_() {
  const apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (!apiKey) {
    Logger.log('ANTHROPIC_API_KEY is not set in Script Properties — aborting guest-status sync.');
    return;
  }

  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const processedLabel = getOrCreateLabel_(GUEST_MESSAGE_PROCESSED_LABEL);
  const threads = GmailApp.search(GUEST_MESSAGE_GMAIL_SEARCH);
  Logger.log('Found ' + threads.length + ' candidate guest-message threads.');

  threads.forEach(function (thread) {
    const subject = thread.getFirstMessageSubject();
    const parsed = parseGuestMessageSubject_(subject);
    if (!parsed) {
      Logger.log('Could not parse guest/vehicle from subject: "' + subject + '" — skipping, left unlabeled for manual review.');
      return;
    }

    let result;
    try {
      result = classifyGuestMessage_(buildThreadText_(thread), apiKey);
    } catch (e) {
      Logger.log('Claude call failed for "' + subject + '": ' + e);
      // Same reasoning as PrivateBookings.js: a 4xx repeats every run
      // (bad key/request), so stop retrying it; anything else may be
      // transient, so leave it unlabeled to retry next run.
      if (e.httpStatus && e.httpStatus >= 400 && e.httpStatus < 500) thread.addLabel(processedLabel);
      return;
    }

    if (!result || !result.isStatusUpdate || !result.statusNote) {
      thread.addLabel(processedLabel); // read it, nothing actionable — don't re-spend an API call on it
      return;
    }

    const found = findActiveTrackerRowByGuestVehicle_(ss, parsed.guestName, parsed.vehicleText);
    if (!found) {
      Logger.log('No current/upcoming tracker row found for ' + parsed.guestName + ' (' + parsed.vehicleText + ') — leaving unlabeled to retry once the booking is logged.');
      return;
    }

    setRowValue_(found, 'Notes', result.statusNote);
    thread.addLabel(processedLabel);
    Logger.log('Updated Notes for ' + parsed.guestName + ': ' + result.statusNote);
  });
}

// Subject looks like: "Paul has sent you a message about your Ford Transit"
function parseGuestMessageSubject_(subject) {
  const m = subject.match(/^(.+?) has sent you a message about your (.+)$/i);
  if (!m) return null;
  return { guestName: m[1].trim(), vehicleText: m[2].trim() };
}

// Asks Claude whether a guest's message thread contains a real operational
// update — a delay, a pickup/drop-off change, a lockbox/access problem, a
// cancellation, anything that changes what the ops team should expect —
// as opposed to routine back-and-forth that needs no action.
function classifyGuestMessage_(threadText, apiKey) {
  const prompt = 'You are reviewing a guest\'s message thread to a small vehicle rental business (Beverley Fleet) ' +
    'about their Turo trip, to decide whether it contains a genuine, current, operationally relevant update the ' +
    'operations team should know about — e.g. a flight delay affecting pickup time, a request for a different ' +
    'pickup/drop-off location or time, an access/lockbox problem, or talk of cancelling. Routine pleasantries, ' +
    'thanks, or questions that were already fully answered earlier in the thread are NOT relevant.\n\n' +
    'Respond with ONLY a JSON object, no other text, matching exactly this shape:\n' +
    '{\n' +
    '  "isStatusUpdate": boolean,\n' +
    '  "statusNote": string or null\n' +
    '}\n\n' +
    'Rules:\n' +
    '- statusNote: one short plain-English sentence describing the CURRENT status as of the latest message (e.g. ' +
    '"Flight delayed — running late, not yet picked up"). Null if isStatusUpdate is false.\n' +
    '- Only set isStatusUpdate true for a genuine, current, actionable update — not politeness, not something ' +
    'already resolved earlier in the same thread.\n' +
    '- Never guess: if it\'s genuinely unclear whether this is actionable, set isStatusUpdate false rather than ' +
    'fabricate a note. A missed real update is far less costly than a false one sitting in the ops sheet.\n\n' +
    'Message thread:\n\n' + threadText;

  const response = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01'
    },
    payload: JSON.stringify({
      model: ANTHROPIC_MODEL,
      max_tokens: 512,
      messages: [{ role: 'user', content: prompt }]
    }),
    muteHttpExceptions: true
  });

  const status = response.getResponseCode();
  if (status !== 200) {
    const err = new Error('Claude API returned ' + status + ': ' + response.getContentText().slice(0, 500));
    err.httpStatus = status;
    throw err;
  }

  const body = JSON.parse(response.getContentText());
  const text = body.content && body.content[0] && body.content[0].text ? body.content[0].text : '';
  const match = text.match(/\{[\s\S]*\}/);
  if (!match) throw new Error('Could not find JSON in Claude response: ' + text.slice(0, 300));
  return JSON.parse(match[0]);
}

// Finds the guest's current-or-upcoming tracker row (Date Due hasn't
// passed yet) across every tab, matching by guest first name + vehicle
// make/model words. Picks the soonest-starting match if more than one —
// deliberately loose rather than exact, same tradeoff findDuplicateBookingRow_
// (QuickBooksSync.js) makes, since Turo's message notification gives us a
// name and a bare vehicle description, not a reservation ID.
function findActiveTrackerRowByGuestVehicle_(ss, guestName, vehicleText) {
  const guestFirst = (guestName || '').toLowerCase().split(' ')[0];
  if (!guestFirst) return null;
  const vehicleWords = (vehicleText || '').toLowerCase().split(' ').filter(Boolean);
  const now = new Date();

  let best = null;
  const sheets = ss.getSheets();
  for (let s = 0; s < sheets.length; s++) {
    const sheet = sheets[s];
    const data = sheet.getDataRange().getValues();
    const headers = data[0];
    const nameIdx = headers.indexOf('Name');
    const vehicleIdx = headers.indexOf('Vehicle');
    const dateOutIdx = headers.indexOf('Date Out');
    const dateDueIdx = headers.indexOf('Date Due');
    if (nameIdx === -1 || dateDueIdx === -1) continue;

    for (let r = 1; r < data.length; r++) {
      const rowName = String(data[r][nameIdx] || '').toLowerCase();
      if (!rowName || rowName.indexOf(guestFirst) === -1) continue;

      if (vehicleIdx !== -1 && vehicleWords.length) {
        const rowVehicle = String(data[r][vehicleIdx] || '').toLowerCase();
        const vehicleMatches = vehicleWords.every(function (w) { return rowVehicle.indexOf(w) !== -1; });
        if (!vehicleMatches) continue;
      }

      const dateDue = coerceDate_(data[r][dateDueIdx]);
      if (!dateDue || dateDue.getTime() < now.getTime()) continue; // trip already over

      const dateOut = dateOutIdx !== -1 ? coerceDate_(data[r][dateOutIdx]) : null;
      if (!best || (dateOut && best.dateOut && dateOut < best.dateOut)) {
        best = { sheet: sheet, rowIndex: r + 1, headers: headers, dateOut: dateOut };
      }
    }
  }
  return best;
}
