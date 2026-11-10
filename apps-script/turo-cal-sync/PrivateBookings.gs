/**
 * PrivateBookings.js
 * ---------------------------------------------------------------
 * Private (non-Turo) bookings don't have one consistent "it's
 * booked" email the way Turo does — confirmation shows up as a
 * DocuSign "Completed:" notice, a PandaDoc "completed by all
 * participants" notice, or sometimes just a manual reply thread
 * ending in "signed and attached", with no formal e-sign step at
 * all. A regex can't reliably cover all of that, so this casts a
 * broad net over candidate threads and asks Claude to judge each
 * one: is this actually a confirmed booking, and if so, what are
 * the guest/vehicle/dates/contract number?
 *
 * SETUP (one time):
 *   1. Get an API key at https://console.anthropic.com (API Keys).
 *   2. In this Apps Script project: Project Settings (gear icon) >
 *      Script Properties > Add script property.
 *      Name:  ANTHROPIC_API_KEY
 *      Value: <paste your key here — never in chat/Claude Code>
 *   3. Run `syncPrivateBookingsToCalendar` manually a few times
 *      first and check: the calendar, the execution log, and the
 *      `private-needs-review` Gmail label — before trusting this
 *      enough to put it on the same 5-minute trigger as Turo.
 *
 * SAFETY DESIGN: this intentionally does NOT guess when unsure.
 * If Claude can't confidently pin down both a start and end date,
 * or flags anything as ambiguous, the thread gets labeled
 * `private-needs-review` instead of creating a calendar event —
 * a bad auto-created event is worse than one that needs a human.
 * ---------------------------------------------------------------
 */

// ====================== CONFIG ======================

const ANTHROPIC_MODEL = 'claude-haiku-4-5-20251001';
const TRACKER_SHEET_ID = '1p1IgcIirkPB-fD7sOjT2cKyhtxMzr3qdrdYRl2lQb2I'; // Rental_Agreements_Tracker
// No date bound — see the equivalent note on Code.js's GMAIL_SEARCH.
const PRIVATE_GMAIL_SEARCH = '(from:docusign.net OR from:getpandadoc.com OR subject:"Rental Agreement" OR subject:"Rental Contract") -label:private-calendared -label:private-needs-review';
const PRIVATE_PROCESSED_LABEL = 'private-calendared';
const PRIVATE_REVIEW_LABEL = 'private-needs-review';
const PRIVATE_CONTRACTS_FOLDER_ID = '15W22to5uU2TI6Ri3lAwdF74wVakBF6IY'; // "Private Rentals" — signed contract PDFs

// =====================================================

function syncPrivateBookingsToCalendar() {
  const apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (!apiKey) {
    Logger.log('ANTHROPIC_API_KEY is not set in Script Properties — see the setup notes at the top of this file. Aborting.');
    return;
  }

  // Same script-wide lock the Turo sync uses — keeps this from overlapping
  // with itself or with the Turo sync while both write to the tracker sheet.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('Another sync run is already in progress — skipping this run.');
    return;
  }
  try {
    syncPrivateBookingsToCalendar_(apiKey);
  } finally {
    lock.releaseLock();
  }
}

function syncPrivateBookingsToCalendar_(apiKey) {
  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);
  const vehicles = loadVehicleLookup_();
  const contractSources = loadContractSourceMap_();
  const processedLabel = getOrCreateLabel_(PRIVATE_PROCESSED_LABEL);
  const reviewLabel = getOrCreateLabel_(PRIVATE_REVIEW_LABEL);

  const threads = GmailApp.search(PRIVATE_GMAIL_SEARCH);
  Logger.log('Found ' + threads.length + ' candidate private-booking threads.');

  threads.forEach(function (thread) {
    const subject = thread.getFirstMessageSubject();
    let result;
    try {
      result = classifyPrivateBooking_(buildThreadText_(thread), apiKey);
    } catch (e) {
      Logger.log('Claude call failed for thread "' + subject + '": ' + e);
      // A 4xx means something's misconfigured (bad key, bad request) and
      // will fail identically every run — flag it for a human rather than
      // silently retrying forever. Anything else (5xx, network blip) is
      // likely transient, so leave the thread unlabeled to retry next run.
      if (e.httpStatus && e.httpStatus >= 400 && e.httpStatus < 500) {
        thread.addLabel(reviewLabel);
        thread.addLabel(processedLabel);
      }
      return;
    }

    if (!result || !result.isConfirmedBooking) {
      Logger.log('Not a confirmed booking: "' + subject + '" — ' + (result && result.reason));
      thread.addLabel(processedLabel); // evaluated, rejected — don't re-spend an API call on it next run
      return;
    }

    const contractNumber = (result.contractNumber || '').toUpperCase().trim();
    const knownSource = contractNumber ? contractSources[contractNumber] : null;

    if (knownSource === 'Turo') {
      // This thread (a direct email negotiation, not the Turo notification)
      // is often the ONLY place accessory/special-request info like a car
      // seat ever gets mentioned, and reaching this branch at all means
      // isConfirmedBooking was true above — i.e. there IS a signed
      // contract for this booking. Cristin's rule: "anyone with a
      // contract is private," full stop, regardless of which channel the
      // booking originated on — so this flips Source to Private on the
      // existing row too, not just merges the accessory/request info.
      const accessories = [result.childSeatDetail || null, result.prepaidFuel ? 'Prepaid fuel' : null, result.unlimitedMiles ? 'Unlimited miles' : null].filter(Boolean).join(', ');
      const found = findTrackerRowByContract_(ss, contractNumber);
      if (found) {
        setRowValue_(found, 'Source', 'Private');
        const vehicle = matchVehicle_(result.vehicleText || getRowValue_(found, 'Vehicle'), result.vehicleYear, vehicles);
        if (result.vehicleText) {
          setRowValue_(found, 'Vehicle', result.vehicleText + (result.vehicleYear ? ' ' + result.vehicleYear : ''));
          // The vehicle just changed (e.g. a swap agreed over email after the
          // original Turo booking) — Lockbox/Door Code was set for whatever
          // vehicle this row had BEFORE, so it has to be recomputed for the
          // new one rather than left stale.
          setRowValue_(found, 'Lockbox/Door Code', lockboxInfoFor_(vehicle));
        }
        if (accessories && !getRowValue_(found, 'Accessories')) setRowValue_(found, 'Accessories', accessories);
        if (result.specialRequests && !getRowValue_(found, 'Special Requests')) setRowValue_(found, 'Special Requests', result.specialRequests);

        // Reaching this branch means the row already existed before this
        // thread — it might have a calendar event from the original Turo
        // sync, or (if that row was hand-entered/migrated rather than
        // synced) it might never have gotten one at all. Either way, a
        // tracker row must never be left with no calendar event, so check
        // and either update the existing one or create it now.
        const guestName = getRowValue_(found, 'Name');
        const start = coerceDate_(getRowValue_(found, 'Date Out'));
        const end = coerceDate_(getRowValue_(found, 'Date Due'));
        const mergedAccessories = getRowValue_(found, 'Accessories');
        const bookingForCal = { guestName: guestName, guestPhone: '', start: start, end: end, reservationId: contractNumber };

        if (!start || !end) {
          Logger.log(contractNumber + ' ("' + subject + '") — tracker row has no usable Date Out/Date Due, could not verify or create its calendar event.');
        } else {
          const existingEvent = findCalendarEventForGuest_(guestName, start, end);
          if (existingEvent) {
            updateCalendarEventForBooking_(existingEvent, bookingForCal, vehicle, 'Private', mergedAccessories);
            Logger.log(contractNumber + ' ("' + subject + '") has a signed contract — switched Source to Private, merged accessory/request/vehicle info, and updated its existing calendar event.');
          } else {
            createCalendarEvent_(bookingForCal, vehicle, 'Private', mergedAccessories);
            Logger.log(contractNumber + ' ("' + subject + '") has a signed contract — switched Source to Private and merged accessory/request/vehicle info; no calendar event existed for it yet, so created one now.');
          }
          pushBookingToWordPress_(bookingForCal, vehicle, 'private');
        }
      } else {
        Logger.log(contractNumber + ' ("' + subject + '") has a signed contract but its row could not be found to update — skipping.');
      }
      thread.addLabel(processedLabel);
      return;
    }

    if (result.needsReview || !result.start || !result.end) {
      Logger.log('Needs manual review: "' + subject + '" — ' + (result.reason || 'missing start/end date'));
      thread.addLabel(reviewLabel);
      thread.addLabel(processedLabel);
      return;
    }

    const vehicle = matchVehicle_(result.vehicleText, result.vehicleYear, vehicles);
    const bookingObj = {
      guestName: result.guestName || 'Unknown guest',
      guestPhone: result.guestPhone || '',
      start: new Date(result.start),
      end: new Date(result.end),
      reservationId: contractNumber,
      vehicleText: result.vehicleText || '',
      vehicleYear: result.vehicleYear || ''
    };

    if (findDuplicateBookingRow_(ss, bookingObj, 'Private')) {
      Logger.log('Already tracked — skipping calendar event for ' + subject);
      thread.addLabel(processedLabel);
      return;
    }

    // Same car, same dates, already tracked as Turo — the guest most
    // likely converted from Turo to this direct arrangement. Drop the
    // stale Turo row rather than carry both; this new Private row becomes
    // the record of the booking going forward.
    const vehicleLabel = vehicle
      ? (vehicle.year + ' ' + vehicle.make + ' ' + vehicle.model)
      : (result.vehicleText + ' ' + result.vehicleYear);
    const turoConflict = findSameVehicleDateConflict_(ss, bookingObj, vehicleLabel, 'Turo');
    if (turoConflict) {
      Logger.log('Existing Turo row for the same vehicle/dates as "' + subject + '" — treating as a Turo-to-Private conversion and removing the Turo row.');
      turoConflict.sheet.deleteRow(turoConflict.rowIndex);
    }

    const accessories = [result.childSeatDetail || null, result.prepaidFuel ? 'Prepaid fuel' : null, result.unlimitedMiles ? 'Unlimited miles' : null].filter(Boolean).join(', ');
    createCalendarEvent_(bookingObj, vehicle, 'Private', accessories);
    logBookingToTracker_(bookingObj, vehicle, 'Private', {
      accessories: accessories,
      specialRequests: result.specialRequests || '',
      sourceLink: findPrivateContractDriveUrl_(bookingObj.guestName) || thread.getPermalink()
    });
    pushBookingToWordPress_(bookingObj, vehicle, 'private');

    Logger.log('Calendared private booking: "' + subject + '" (' + (contractNumber || 'no contract #') + ')');
    thread.addLabel(processedLabel);
  });
}

// ====================== ONE-TIME SHEET SETUP ======================

// Inserts the "Carvergent Fee" and "Accessories" columns right after
// "Source" in Rental_Agreements_Tracker (pushing the existing "Notes"
// column two spots right). Run this once, manually, before either sync
// tries to log a booking — it's idempotent, so re-running it is harmless
// once the columns already exist.
function restructureRentalTrackerColumns() {
  const sheet = SpreadsheetApp.openById(TRACKER_SHEET_ID).getSheets()[0];
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];

  if (headers.indexOf('Carvergent Fee') !== -1) {
    Logger.log('Carvergent Fee column already exists — nothing to do.');
    return;
  }

  const sourceCol = headers.indexOf('Source') + 1; // 1-based
  if (!sourceCol) throw new Error('Could not find a "Source" column in Rental_Agreements_Tracker — check the header row before running this.');

  sheet.insertColumnsAfter(sourceCol, 2);
  sheet.getRange(1, sourceCol + 1).setValue('Carvergent Fee');
  sheet.getRange(1, sourceCol + 2).setValue('Accessories');

  Logger.log('Inserted "Carvergent Fee" (column ' + (sourceCol + 1) + ') and "Accessories" (column ' + (sourceCol + 2) + ') after Source.');
}

// Concatenates subject + body of the first several messages in a thread
// (capped, to keep the Claude request small and cheap).
function buildThreadText_(thread) {
  const messages = thread.getMessages();
  const parts = messages.slice(0, 6).map(function (m) {
    return 'From: ' + m.getFrom() + '\nDate: ' + m.getDate() + '\nSubject: ' + m.getSubject() + '\n\n' + m.getPlainBody().slice(0, 3000);
  });
  return parts.join('\n\n---\n\n').slice(0, 12000);
}

// Asks Claude whether a thread represents a confirmed private booking, and
// if so, to extract the structured fields needed to calendar it.
function classifyPrivateBooking_(threadText, apiKey) {
  const prompt = 'You are reviewing an email thread from a small vehicle rental business (Beverley Fleet) to decide ' +
    'whether it represents a CONFIRMED private (non-Turo) rental booking — i.e. the customer has signed/returned a ' +
    'rental contract and the booking is going ahead. Threads matched here may be false positives: partnership emails, ' +
    'Turo-sourced bookings that also happen to use e-sign, contracts still in progress, or cancelled bookings.\n\n' +
    'Respond with ONLY a JSON object, no other text, matching exactly this shape:\n' +
    '{\n' +
    '  "isConfirmedBooking": boolean,\n' +
    '  "needsReview": boolean,\n' +
    '  "reason": string,\n' +
    '  "contractNumber": string or null,\n' +
    '  "guestName": string or null,\n' +
    '  "guestPhone": string or null,\n' +
    '  "vehicleText": string or null,\n' +
    '  "vehicleYear": string or null,\n' +
    '  "start": string or null,\n' +
    '  "end": string or null,\n' +
    '  "childSeatDetail": string or null,\n' +
    '  "prepaidFuel": boolean,\n' +
    '  "unlimitedMiles": boolean,\n' +
    '  "specialRequests": string or null\n' +
    '}\n\n' +
    'Rules:\n' +
    '- contractNumber is the "CO-####" style contract number if mentioned anywhere (subject or body).\n' +
    '- start/end must be ISO 8601 datetimes (e.g. "2026-09-17T15:00:00"), Denver local time, if you can confidently ' +
    'determine both. If you cannot confidently determine BOTH a start and an end datetime, set needsReview true and ' +
    'leave start/end null rather than guessing.\n' +
    '- isConfirmedBooking should be false for: partnership/marketing emails, cancelled bookings, contracts not yet ' +
    'fully signed, or anything where "rental agreement" is mentioned only in passing.\n' +
    '- childSeatDetail/prepaidFuel/unlimitedMiles are ONLY for these three accessories, never fees or dollar ' +
    'amounts. childSeatDetail is a short phrase with the count and size/facing if stated (e.g. "2 car seats ' +
    '(forward-facing)", "1 car seat"), or null if none was requested. prepaidFuel/unlimitedMiles: true only if ' +
    'explicitly requested/agreed to somewhere in the thread. Default false/null — never guess these.\n' +
    '- specialRequests: a short plain-English summary (e.g. "Requested terminal delivery") of any explicit ' +
    'operational request the guest made, or null if there is none. This is NOT for pricing, fees, or anything ' +
    'already captured in the other fields.\n' +
    '- Set needsReview true whenever you are not fully confident, even if isConfirmedBooking is true — a human will ' +
    'double check anything flagged.\n\n' +
    'Email thread:\n\n' + threadText;

  const response = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01'
    },
    payload: JSON.stringify({
      model: ANTHROPIC_MODEL,
      max_tokens: 1024,
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

// Maps "CO-1027" -> "Private" / "Turo" from the Rental_Agreements_Tracker
// sheet's Contract # and Source columns, so Turo contracts that also went
// through DocuSign don't get double-calendared.
function loadContractSourceMap_() {
  const sheet = SpreadsheetApp.openById(TRACKER_SHEET_ID).getSheets()[0];
  const data = sheet.getDataRange().getValues();
  const headers = data[0];
  const contractIdx = headers.indexOf('Contract #');
  const sourceIdx = headers.indexOf('Source');

  const map = {};
  data.slice(1).forEach(function (row) {
    const key = String(row[contractIdx] || '').toUpperCase().trim();
    if (key) map[key] = String(row[sourceIdx] || '').trim();
  });
  return map;
}

// The signed-contract PDFs in the "Private Rentals" Drive folder don't
// reliably encode the deal's own contract number in the filename — most
// still carry "CO-105", the form template's revision number, not the
// deal-specific one (e.g. Luke Kuepfer's file is literally named "CO-105
// Rental Contract - 2025 Ford Transit - Luke Kuepfer.pdf", not "CO-1027").
// Every file does end with the guest's full name, though, so that's the
// reliable match. If more than one file matches (re-signed or renamed
// copies happen — Georgette Zenk has three), this takes the most recently
// modified one rather than guessing further.
function findPrivateContractDriveUrl_(guestName) {
  if (!guestName) return null;
  const lastName = guestName.trim().split(' ').pop().toLowerCase();
  if (!lastName) return null;

  const files = DriveApp.getFolderById(PRIVATE_CONTRACTS_FOLDER_ID).getFiles();
  let best = null;
  while (files.hasNext()) {
    const file = files.next();
    if (file.getName().toLowerCase().indexOf(lastName) === -1) continue;
    if (!best || file.getLastUpdated() > best.getLastUpdated()) best = file;
  }
  return best ? best.getUrl() : null;
}
