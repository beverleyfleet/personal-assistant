/**
 * WebApp.js
 * ---------------------------------------------------------------
 * Lets Claude Code trigger functions in this project over a plain
 * HTTPS request instead of Apps Script's Execution API. A Web App
 * deployed with "Execute as: Me" always runs with your authority
 * regardless of who calls the URL, so there's no OAuth negotiation
 * per call — the secret below is what actually protects it.
 *
 * IMPORTANT: this file is now tracked in the local clasp project,
 * specifically so a future `clasp push` doesn't delete it again
 * (that's what broke the webhook on 2026-09-17 — this file existed
 * only in the browser editor, and clasp push treats the local
 * folder as authoritative, silently removing anything not in it).
 * ---------------------------------------------------------------
 */

function doPost(e) {
  const secret = PropertiesService.getScriptProperties().getProperty('WEBAPP_SECRET');
  let params;
  try {
    params = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonOutput_({ status: 'error', message: 'Invalid JSON body.' });
  }

  if (!secret || params.secret !== secret) {
    return jsonOutput_({ status: 'error', message: 'Unauthorized.' });
  }

  const allowedFunctions = {
    restructureRentalTrackerColumns: restructureRentalTrackerColumns,
    migrateExistingTrackerRowsToMonthTabs: migrateExistingTrackerRowsToMonthTabs,
    restructureTrackerForOpsView: restructureTrackerForOpsView,
    backfillTrackerFromManualCalendarEvents: backfillTrackerFromManualCalendarEvents,
    syncTuroBookingsToCalendar: syncTuroBookingsToCalendar,
    syncPrivateBookingsToCalendar: syncPrivateBookingsToCalendar,
    syncQuickBookPayments: syncQuickBookPayments,
    syncGuestStatusNotes: syncGuestStatusNotes,
    installRealtimeTrigger: installRealtimeTrigger
  };

  const fn = allowedFunctions[params.function];
  if (!fn) {
    return jsonOutput_({ status: 'error', message: 'Unknown or disallowed function: ' + params.function });
  }

  try {
    fn();
    return jsonOutput_({ status: 'ok', function: params.function });
  } catch (err) {
    return jsonOutput_({ status: 'error', function: params.function, message: String(err) });
  }
}

function jsonOutput_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
