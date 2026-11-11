// Runs the real Apps Script files in a VM with in-memory stand-ins for Gmail/Calendar/Sheets.
process.env.TZ = 'America/Denver';
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert');
const DIR = process.argv[2] || path.join(__dirname, "..");
const NOW = new Date('2026-11-10T12:00:00-07:00').getTime();

function body(name, startLine, endLine, veh, res) {
  return `${name} has changed their trip\n\nHere’s what ${name} changed:\n\n-\n    New trip start on X\n\n` +
    `    ${veh}\n\n            booked by ${name}\n\n            Trip start: ${startLine}\n        Trip end: ${endLine}\n\n` +
    `            You earn: $253.17\n            ${name}\n                    (412) 480-5352\n\n            Reservation ID #${res}\n`;
}
const MAILS = [
  // newest first, like GmailApp.search
  { subject: 'Claudine has changed their trip with your Mercedes-Benz Sprinter (62950737)', body: body('Claudine', '1/28/27 9:00 AM', '2/1/27 10:00 AM', 'Mercedes-Benz Sprinter 2026', '62950737') },
  { subject: 'Paul has changed their trip with your Chevrolet Suburban (62256487)', body: body('Paul', '2/23/27 7:30 AM', '2/26/27 7:30 AM', 'Chevrolet Suburban 2021', '62256487') },
  { subject: 'Georgette has changed their trip with your Ford Transit (59154055)', body: body('Georgette', '8/4/26 4:30 PM', '8/6/26 7:00 AM', 'Ford Transit 2025', '59154055') },
  { subject: 'Dana has changed their trip with your Audi Q5 (70000001)', body: body('Dana', '3/1/27 9:00 AM', '3/3/27 9:00 AM', 'Audi Q5 2022', '70000001') },
  { subject: 'Eve has changed their trip with your Audi Q5 (70000002)', body: body('Eve', '3/5/27 9:00 AM', '3/7/27 9:00 AM', 'Audi Q5 2022', '70000002') },
  { subject: 'Garbled has changed their trip with your Audi Q5 (1)', body: 'nothing parseable here' },
];

const labels = {};
const threads = MAILS.map(m => ({ m, labels: [],
  getMessages() { return [{ getSubject: () => m.subject, getPlainBody: () => m.body }]; },
  addLabel(l) { this.labels.push(l.name); } }));

const events = [];
function mkEvent(title, start, end, desc) {
  const e = { title, start, end, desc, deleted: false, guests: [],
    getDescription() { return this.desc; }, getTitle() { return this.title; },
    setTime(s, en) { this.start = s; this.end = en; }, setTitle(t) { this.title = t; },
    setDescription(d) { this.desc = d; }, addGuest(g) { this.guests.push(g); },
    deleteEvent() { this.deleted = true; events.splice(events.indexOf(this), 1); } };
  events.push(e); return e;
}
// Paul exists with OLD times; Lopez exists far out (Feb 19) for the cancel test; Claudine has no event.
mkEvent('2021 Chevrolet Suburban — Paul — Pickup 2/23 7:00 AM / Dropoff 2/26 5:00 PM', new Date(2027,1,23,7,0), new Date(2027,1,26,17,0), 'Turo reservation: https://turo.com/reservation/62256487\n');
mkEvent('2021 Chevrolet Suburban — Lopez — Pickup 2/19 10:00 AM / Dropoff 2/22 10:00 AM', new Date(2027,1,19,10), new Date(2027,1,22,10), 'Turo reservation: https://turo.com/reservation/62185669\n');

const sheetRows = { headers: ['Contract #','Name','Vehicle','Lockbox/Door Code','Date Out','Date Due','Status','Notes'],
  rows: [['62256487','Paul','2021 Chevrolet Suburban','Lockbox 1089', new Date(2027,1,23,7), new Date(2027,1,26,17),'Confirmed',''],
                ['70000002','Eve','2022 Audi Q5','Lockbox 1067', new Date(2027,2,5,9), new Date(2027,2,7,9),'Canceled','']] };
const trackerSheet = { getName: () => 'Feb 2027',
  getDataRange() { return { getValues: () => [sheetRows.headers, ...sheetRows.rows] }; },
  getRange(r, c) { return { getValue: () => sheetRows.rows[r-2][c-1], setValue: v => { sheetRows.rows[r-2][c-1] = v; } }; } };
const fleetSheet = { getDataRange() { return { getValues: () => [
  ['Year','Make','Model','Name','Unit #','Lic Plate','Check In','Code'],
  ['2021','Chevrolet','Suburban','','7','FNYI83','Lockbox','1089'],
  ['2026','Mercedes','Sprinter','16491879','6','','Lockbox','0763'],
  ['2025','Ford','Transit-350 Med Roof','Heaps','5','Temp 8632431','Remote Unlock',''] ] }; } };

const wpPushes = [], logs = [];
const ctx = {
  Date: class extends Date { constructor(...a) { if (a.length === 0) super(NOW); else super(...a); } static now() { return NOW; } },
  Logger: { log: s => logs.push(s) },
  LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
  GmailApp: { search: q => { if (q.includes('subject:cancel')) return q.includes('70000001') ? [{}] : [];
    assert(q.includes('has changed their trip')); return threads.filter(t => !t.labels.includes('turo-change-processed')); },
    getUserLabelByName: n => labels[n], createLabel: n => (labels[n] = { name: n }) },
  CalendarApp: { getDefaultCalendar: () => ({
    getEvents: (a, b) => events.filter(e => e.start < b && e.end > a),
    createEvent: (t, s, e, o) => mkEvent(t, s, e, o.description) }) },
  SpreadsheetApp: { openById: id => ({ getSheets: () => id === '1ve7L683k5j_TOWL93BnreU7Av2QXyf605POCXV8kec0' ? [fleetSheet] : [trackerSheet] }) },
  Utilities: { formatDate: (d, tz, f) => {
    const p = n => String(n).padStart(2, '0'); const h = d.getHours() % 12 || 12;
    if (f === 'M/d h:mm a') return `${d.getMonth()+1}/${d.getDate()} ${h}:${p(d.getMinutes())} ${d.getHours() < 12 ? 'AM' : 'PM'}`;
    if (f === 'yyyy-MM-dd') return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
    throw new Error('fmt ' + f); } },
  PropertiesService: { getScriptProperties: () => ({ getProperty: () => 'test-key' }) },
  UrlFetchApp: { fetch: (url, o) => { wpPushes.push({ url, payload: JSON.parse(o.payload) }); return { getResponseCode: () => 200, getContentText: () => '{}' }; } },
};
vm.createContext(ctx);
for (const f of fs.readdirSync(DIR).filter(f => f.endsWith('.gs'))) vm.runInContext(fs.readFileSync(path.join(DIR, f), 'utf8'), ctx, { filename: f });

// --- trip changes ---
vm.runInContext('syncTuroTripChanges()', ctx);
const paul = events.find(e => e.desc.includes('62256487'));
assert.strictEqual(paul.start.getTime(), new Date(2027,1,23,7,30).getTime(), 'Paul start moved');
assert.strictEqual(paul.end.getTime(), new Date(2027,1,26,7,30).getTime(), 'Paul end moved');
assert.strictEqual(paul.title, '2021 Chevrolet Suburban — Paul — Pickup 2/23 7:30 AM / Dropoff 2/26 7:30 AM');
assert.strictEqual(events.filter(e => e.desc.includes('62256487')).length, 1, 'no duplicate Paul event');
const claudine = events.find(e => e.desc.includes('62950737'));
assert(claudine, 'missing Claudine event created');
assert.deepStrictEqual(claudine.guests, ['info@beverleyfleet.com', 'vkallday1996@gmail.com']);
assert(!events.some(e => e.desc.includes('59154055')), 'past Georgette trip not recreated');
assert.strictEqual(sheetRows.rows[0][4].getTime(), new Date(2027,1,23,7,30).getTime(), 'tracker Date Out');
assert.strictEqual(sheetRows.rows[0][5].getTime(), new Date(2027,1,26,7,30).getTime(), 'tracker Date Due');
assert(sheetRows.rows[0][7].startsWith('Changed — Paul has changed'), 'tracker note');
const labeled = t => threads.find(x => x.m.subject.startsWith(t)).labels.includes('turo-change-processed');
assert(labeled('Paul') && labeled('Claudine') && labeled('Georgette'), 'parsed emails labeled');
assert(!labeled('Garbled'), 'unparseable email left for review');
assert.deepStrictEqual(wpPushes.map(p => p.payload.rows[0].ref).sort(), ['62256487', '62950737'], 'WordPress pushed for upcoming trips only');
// second run is a no-op
const before = JSON.stringify(events.map(e => [e.title, +e.start, +e.end]));
vm.runInContext('syncTuroTripChanges()', ctx);
assert.strictEqual(JSON.stringify(events.map(e => [e.title, +e.start, +e.end])), before, 'rerun changes nothing');

// --- cancellation window: Lopez is 101 days out from NOW but would have been >120 from Oct 22 ---
vm.runInContext('removeCalendarEventByReservationId_("62185669")', ctx);
assert(!events.some(e => e.desc.includes('62185669')), 'far-out canceled event removed');
// and something ~300 days out is now found too
mkEvent('Far — Test', new Date(2027,8,1), new Date(2027,8,3), 'reservation/99999999');
vm.runInContext('removeCalendarEventByReservationId_("99999999")', ctx);
assert(!events.some(e => e.desc.includes('99999999')), '300-day-out event removed');

assert(!events.some(e => /70000001|70000002/.test(e.desc)), 'canceled trips not recreated');
assert(!wpPushes.some(p => /7000000/.test(p.payload.rows[0].ref)), 'canceled trips not pushed to WordPress');
assert(labeled('Dana') && labeled('Eve'), 'canceled-trip change emails still labeled');
assert.strictEqual(sheetRows.rows[1][4].getTime(), new Date(2027,2,5,9).getTime(), 'canceled row dates untouched');
console.log('ALL TESTS PASSED'); console.log(logs.filter(l => /Moved|created one|already ended|Could not parse|Removed/.test(l)).join('\n'));
