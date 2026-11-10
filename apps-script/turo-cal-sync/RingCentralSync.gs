/**
 * RingCentralSync.js
 * ---------------------------------------------------------------
 * Feeds RingCentral SMS traffic into the tracker's Notes column the
 * same way GuestMessages.js does for Turo's in-app guest messages —
 * a genuine, current operational update (a delay, a changed pickup,
 * a lockbox problem) gets logged; routine back-and-forth doesn't.
 * Reuses classifyGuestMessage_ (GuestMessages.js) as-is rather than
 * duplicating that prompt.
 *
 * Unlike the Gmail-based syncs, RingCentral messages have no
 * per-message "already processed" label to lean on, so this tracks
 * its own cursor (the RINGCENTRAL_LAST_SYNC script property, an ISO
 * timestamp) and only ever looks at messages newer than that.
 *
 * Matches an incoming text to a tracker row by PHONE NUMBER (the
 * "Phone" column — see logBookingToTracker_, Code.js) rather than by
 * guest name: RingCentral's message-store API gives a phone number,
 * not a name, so phone is the reliable side of this match.
 *
 * SETUP (one time):
 *   1. In the RingCentral Developer Console, create a "Server-only
 *      (REST API app)" app with the SMS / Read Call Log / Read
 *      Messages permissions, and generate a JWT credential for it.
 *   2. Project Settings > Script Properties > add:
 *        RINGCENTRAL_CLIENT_ID
 *        RINGCENTRAL_CLIENT_SECRET
 *        RINGCENTRAL_JWT
 *      (never paste these into chat — same rule as ANTHROPIC_API_KEY.)
 *   3. Run `syncRingCentralMessagesToTracker` manually a few times
 *      and check the tracker + execution log before adding it to the
 *      5-minute trigger (installRealtimeTrigger, Code.js) — same
 *      caution PrivateBookings.js takes before trusting a new
 *      classifier-based sync unattended.
 *
 * SAFETY: if any of the three Script Properties above aren't set,
 * this logs why and returns immediately — it never half-runs on a
 * partial credential.
 * ---------------------------------------------------------------
 */

const RINGCENTRAL_API_BASE = 'https://platform.ringcentral.com';
const RINGCENTRAL_LAST_SYNC_PROP = 'RINGCENTRAL_LAST_SYNC';

function syncRingCentralMessagesToTracker() {
  // Same script-wide lock every other sync uses — keeps this from
  // overlapping itself or racing the Turo/Private/guest-message syncs
  // while they all touch the tracker sheet.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) {
    Logger.log('Another sync run is already in progress — skipping this run.');
    return;
  }
  try {
    syncRingCentralMessagesToTracker_();
  } finally {
    lock.releaseLock();
  }
}

function syncRingCentralMessagesToTracker_() {
  const props = PropertiesService.getScriptProperties();
  const clientId = props.getProperty('RINGCENTRAL_CLIENT_ID');
  const clientSecret = props.getProperty('RINGCENTRAL_CLIENT_SECRET');
  const jwt = props.getProperty('RINGCENTRAL_JWT');
  const apiKey = props.getProperty('ANTHROPIC_API_KEY');

  if (!clientId || !clientSecret || !jwt) {
    Logger.log('RINGCENTRAL_CLIENT_ID / RINGCENTRAL_CLIENT_SECRET / RINGCENTRAL_JWT are not all set in Script Properties — see the setup notes at the top of this file. Aborting.');
    return;
  }
  if (!apiKey) {
    Logger.log('ANTHROPIC_API_KEY is not set in Script Properties — this sync reuses the same classifier as GuestMessages.js and needs it too. Aborting.');
    return;
  }

  const accessToken = getRingCentralAccessToken_(clientId, clientSecret, jwt);
  // First run ever (no cursor yet) only looks back 24 hours — deliberately
  // narrow, so turning this on doesn't suddenly reclassify weeks of old
  // texts against bookings that may no longer be current/upcoming.
  const since = props.getProperty(RINGCENTRAL_LAST_SYNC_PROP) || new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const syncStartedAt = new Date().toISOString();

  const messages = fetchRingCentralSms_(accessToken, since);
  Logger.log('Found ' + messages.length + ' RingCentral SMS message(s) since ' + since + '.');

  const ss = SpreadsheetApp.openById(TRACKER_SHEET_ID);

  messages.forEach(function (msg) {
    if (msg.direction !== 'Inbound') return; // only guest/partner-initiated texts carry new info
    const phone = msg.from && msg.from.phoneNumber;
    const text = msg.subject || ''; // RingCentral's message-store API puts the SMS body in "subject"
    if (!phone || !text) return;

    const found = findActiveTrackerRowByPhone_(ss, phone);
    if (!found) {
      Logger.log('No current/upcoming tracker row found for ' + phone + ' — skipping ("' + text.slice(0, 60) + '").');
      return;
    }

    let result;
    try {
      result = classifyGuestMessage_(text, apiKey); // shared with GuestMessages.js — same prompt, same judgment
    } catch (e) {
      Logger.log('Claude call failed for a message from ' + phone + ': ' + e);
      return; // leave it for next run rather than silently drop it
    }

    if (!result || !result.isStatusUpdate || !result.statusNote) return;
    setRowValue_(found, 'Notes', result.statusNote);
    Logger.log('Updated Notes for ' + phone + ' from RingCentral SMS: ' + result.statusNote);
  });

  // Only advance the cursor after a fully successful pass — if
  // fetchRingCentralSms_ or getRingCentralAccessToken_ threw above, this
  // line never runs, so a failed run gets retried from the same "since"
  // next time instead of silently skipping the gap.
  props.setProperty(RINGCENTRAL_LAST_SYNC_PROP, syncStartedAt);
}

function getRingCentralAccessToken_(clientId, clientSecret, jwt) {
  const response = UrlFetchApp.fetch(RINGCENTRAL_API_BASE + '/restapi/oauth/token', {
    method: 'post',
    headers: { Authorization: 'Basic ' + Utilities.base64Encode(clientId + ':' + clientSecret) },
    contentType: 'application/x-www-form-urlencoded',
    payload: {
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: jwt
    },
    muteHttpExceptions: true
  });
  const status = response.getResponseCode();
  if (status !== 200) throw new Error('RingCentral token exchange failed (' + status + '): ' + response.getContentText().slice(0, 300));
  return JSON.parse(response.getContentText()).access_token;
}

function fetchRingCentralSms_(accessToken, sinceIso) {
  const url = RINGCENTRAL_API_BASE + '/restapi/v1.0/account/~/extension/~/message-store'
    + '?messageType=SMS&dateFrom=' + encodeURIComponent(sinceIso) + '&perPage=100';
  const response = UrlFetchApp.fetch(url, {
    headers: { Authorization: 'Bearer ' + accessToken },
    muteHttpExceptions: true
  });
  const status = response.getResponseCode();
  if (status !== 200) throw new Error('RingCentral message-store fetch failed (' + status + '): ' + response.getContentText().slice(0, 300));
  const body = JSON.parse(response.getContentText());
  return body.records || [];
}

// Finds a phone number's current-or-upcoming tracker row (Date Due hasn't
// passed yet) across every tab — the phone-number equivalent of
// findActiveTrackerRowByGuestVehicle_ (GuestMessages.js), needed because
// RingCentral gives a phone number, not a guest name. Compares on the last
// 10 digits only, so formatting differences (+1, dashes, parens) don't
// cause a false miss.
function findActiveTrackerRowByPhone_(ss, phoneNumber) {
  const digits = String(phoneNumber || '').replace(/\D/g, '').slice(-10);
  if (!digits) return null;
  const now = new Date();

  let best = null;
  const sheets = ss.getSheets();
  for (let s = 0; s < sheets.length; s++) {
    const sheet = sheets[s];
    const data = sheet.getDataRange().getValues();
    const headers = data[0];
    const phoneIdx = headers.indexOf('Phone');
    const dateDueIdx = headers.indexOf('Date Due');
    const dateOutIdx = headers.indexOf('Date Out');
    if (phoneIdx === -1 || dateDueIdx === -1) continue;

    for (let r = 1; r < data.length; r++) {
      const rowDigits = String(data[r][phoneIdx] || '').replace(/\D/g, '').slice(-10);
      if (!rowDigits || rowDigits !== digits) continue;

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
