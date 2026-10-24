# fleet-telematics: live vehicle data for Beverley Fleet

A local MCP server (stdio) that gives Claude real-time data for the Beverley Fleet rental cars:
odometer, fuel, GPS location against the ParkDIA home base, lock state, tire pressure, oil life and
12V battery. It also has a gated lock/unlock. It runs on Cristin's machine. The primary data source is
**Smartcar** (OEM connected-car APIs, authorised once per brand login, no hardware), using the
current **Vehicle API v3**. **Ford Pro Telematics** and **Mercedes PRO connect** are documented stubs.

Read-only by default. Lock/unlock refuse unless the server runs with `FLEET_ALLOW_WRITE=1` and the call
passes `confirm=True`. Unlock also needs a `reason`. Every write attempt is recorded in an append-only,
hash-chained local audit log.

| Vehicle (key) | VIN | Plate | Brand login used by Smartcar |
|---|---|---|---|
| 2022 Audi Q5 (`audi`) | WA1GAAFY6N2085527 | EIUY09 | myAudi |
| 2021 Chevrolet Suburban (`sub`) | *(not on record; filled after connect)* | FNYI83 | myChevrolet |
| 2024 Ford Transit-350 "Joker" (`t24`) | 1FBAX9Y87RKA27765 | Temp 8620436 | FordPass / Ford app |
| 2025 Ford Transit-350 "Heaps" (`t25`) | 1FBAX9CGXSKA05649 | Temp 8632431 | FordPass / Ford app |
| 2026 Mercedes-Benz Sprinter (`sprinter`) | *(unknown; filled after connect)* | — | Mercedes me |

Plates come from the CTIO server's `fleet.toml`. The map lives in [`fleet.toml`](fleet.toml), and
`fleet-auth` fills in `smartcar_vehicle_id`, plus any missing VIN, after the brand login.

---

## Auth model: why there are no refresh tokens

The brief asked for stored per-vehicle refresh tokens. **Smartcar's current API (v3, introduced
2025-08-26) does not use them.** The v2 API, which used per-vehicle access and refresh tokens, is
deprecated as of **2026-12-01**, about five weeks after this build. In v3:

1. The server gets an **application access token** with the OAuth2 *client credentials* grant:
   `POST https://iam.smartcar.com/oauth2/token` with `grant_type=client_credentials`, `client_id` and
   `client_secret`. The token is valid for 1 hour and has **no refresh token**. When it expires, the
   server requests a new one.
2. Each brand login completes **Smartcar Connect** once. The redirect returns a **`user_id`**.
3. Every signal read and command sends `Authorization: Bearer <app token>` plus the header
   **`sc-user-id: <user_id>`**.

So what 1Password stores long-term is the **client secret** plus **one `user_id` per brand login**.
`fleet-auth` runs Connect locally with a `localhost` redirect, captures the `user_id` and saves it with
`op item edit`. If `op` is missing, it saves to a 0600 file instead and prints a warning. This is the
v3 equivalent of "save the refresh tokens back into 1Password".

## Install

```bash
cd ~/beverley-ops/mcp/fleet-telematics
python3.13 -m venv --system-site-packages .venv   # the system already has mcp 2.3.0 + httpx2
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q                              # offline: replay fixtures + fake `op`
```

Register it with Claude Code. Use the venv's Python so the dependencies resolve:

```bash
claude mcp add fleet-telematics -- ~/beverley-ops/mcp/fleet-telematics/.venv/bin/python -m fleet_telematics
# to allow lock/unlock (each call still needs confirm=True; unlock also needs a reason):
claude mcp add fleet-telematics -e FLEET_ALLOW_WRITE=1 -- ~/beverley-ops/mcp/fleet-telematics/.venv/bin/python -m fleet_telematics
```

## One-time setup

### 1. Smartcar account and application
1. Sign up at https://dashboard.smartcar.com and choose the **Build** plan (see Costs). The Free plan
   allows only 1 connected vehicle and 0 commands.
2. **Configuration → Vehicle Access**: select the signals below, which derive the permissions.
   `fleet-auth` also passes an explicit `scope`, and per Smartcar's docs a `scope` in the URL overrides
   the dashboard setting.
   Read set: `read_vehicle_info read_vin read_odometer read_fuel read_location read_security read_tires read_engine_oil read_diagnostics`.
   Add `control_security` only if you want lock/unlock (`fleet-auth connect … --allow-write-scope`).
3. **Redirect URIs**: add `http://localhost:8000/callback`. Smartcar allows plain http only for localhost.
4. **Privacy policy URL**: Connect shows it to the person granting access. Use the beverleyfleet.com policy.
5. **API Credentials → Create Secret**. Copy the client id and client secret. The secret is shown only
   once. Also copy the **application id** from Configuration → Application details. The Connect URL uses
   `application_id`; the token endpoint uses `client_id`.

### 2. 1Password item
In vault **Beverley Fleet**, create an item named **Smartcar** with these fields: `client_id`,
`client_secret` (concealed) and `application_id`. Creating it in the 1Password app avoids putting the
secret in your shell history. `fleet-auth` adds `user_id_ford`, `user_id_audi`, `user_id_mercedes` and
`user_id_chevrolet`. The vault, item and refs can be changed with environment variables (see below).
For headless use, export `OP_SERVICE_ACCOUNT_TOKEN`; `op` reads it itself.

### 3. Connect each brand login (owner consent)
```bash
.venv/bin/fleet-auth connect ford        # FordPass / Ford app login that has both Transits
.venv/bin/fleet-auth connect audi        # myAudi
.venv/bin/fleet-auth connect chevrolet   # myChevrolet
.venv/bin/fleet-auth connect mercedes    # Mercedes me (Sprinter)
.venv/bin/fleet-auth status              # what is linked (never prints secret values)
.venv/bin/fleet-auth token-test          # client credentials work; count of connections
```
Each `connect` opens `https://connect.smartcar.com/oauth/authorize?response_type=none&application_id=…&redirect_uri=http://localhost:8000/callback&scope=…&state=…&make=<brand>&external_id=beverley-fleet-<brand>&flags=country:US`.
Cristin logs in to the brand account and approves the permissions. Smartcar then redirects to localhost
with `user_id` and `state`. `fleet-auth` checks `state`, stores the `user_id`, lists that user's
connections (`GET /v3/connections?filter[userId]=…`) and matches them to `fleet.toml`: first by VIN
(from the `vehicleidentification-vin` signal), otherwise by make, model and year for a vehicle with no
VIN. It writes `smartcar_vehicle_id` and fills a missing VIN, keeping the file's comments. If a vehicle
can't be matched: `fleet-auth link <account> --assign sprinter=<vehicle_id>`.

Consent notes by brand. These are general Smartcar/OEM behaviour and were **not tested** with these
specific accounts:
- **Ford**: one Ford account login can cover both Transits if both are in that account. Each van's modem
  must be activated in the Ford app. If the Transits are enrolled in Ford Pro instead, check that they
  also show up in the consumer Ford app.
- **Audi (myAudi)**, **Mercedes (Mercedes me)**, **Chevrolet (myChevrolet)**: the vehicle must be added
  to the brand app and its connected services must be active. Some OEMs only allow the primary or owner
  account, not a secondary driver. Chevrolet remote commands may need an active OnStar or Connected
  Services plan.
- Smartcar can require re-authentication after OEM migrations or password changes. Errors that carry
  `resolution: REAUTHENTICATE` tell you to re-run `fleet-auth connect <brand>`.
- To remove access: Smartcar Dashboard, `DELETE /v3/connections` (not wrapped here), or the brand app.

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `fleet_list()` | local | Vehicles, VIN, plate, link status, and what Smartcar's US matrix says each model supports |
| `vehicle_status(vehicle, reveal_location_reason=None, vehicles_on_trip=None)` | read, API | Odometer (mi), fuel %/range/gallons, location + distance to ParkDIA, lock state/open doors, tire psi, oil life, 12V battery, gear/speed, each with the OEM `as_of` timestamp; per-signal status |
| `fleet_snapshot(vehicles_on_trip=None)` | read, API | All vehicles in parallel, with flags (below) and a `flag_summary` |
| `vehicle_odometer_history(vehicle, since=None)` | local | Odometer readings this server has recorded, plus miles driven in the range |
| `fleet_config_status()` | local | Config, link status, 1Password refs, write gate, audit-chain check (no secrets) |
| `vehicle_lock(vehicle, confirm=False)` | **write** | `POST /v3/vehicles/{id}/commands/security/lock`, gated |
| `vehicle_unlock(vehicle, confirm=False, reason="")` | **write** | `POST …/security/unlock`, gated, requires a reason, audit-logged |

`vehicle` accepts a key (`t24`), nickname (`Joker`), plate (`EIUY09`), full VIN, or the last 6+ VIN characters.

**Flags in `fleet_snapshot`**
- `low_fuel`: fuel below 25% (`[thresholds] low_fuel_percent`).
- `unlocked_not_on_trip`: unlocked and not on a rental.
- `door_open`.
- `outside_geofence`: more than 600 m from the ParkDIA lot centre.
- `tire_pressure_alert`, `oil_service`: oil life at or below 10%, or an OEM alert.
- `stale_data`: newest OEM timestamp older than 24 h.
- `lock_state_unavailable`: the Suburban; see the matrix.
- `no_data`: an error for that one vehicle.

**Trip status.** This server can't see bookings itself. Pass `vehicles_on_trip`, which Claude can build
from the Google Calendar bookings, or set `FLEET_TRIPS_FILE` to a JSON list of
`{"vehicle","start","end"}` (ISO times). Without trip info, status is `unknown`, and the unlock and
geofence flags are only `info`.

**Geofence check.** The brief gave lat 39.8155, lon -104.7270. Reverse geocoding puts that point **on
the E-470 highway, about 3.3 km west of the ParkDIA lot**, so it is not used. `fleet.toml` instead uses
the centroid of the OpenStreetMap ParkDIA lot (way 1131040629, `operator=ParkDIA`): **39.8180,
-104.6888**, radius 600 m, which covers the lot's whole bounding box (39.81603–39.82002,
-104.69235 to -104.68512). The US Census geocoder interpolates "25200 E 68th Ave" to 39.8201, -104.6718,
which is a point on the road, not the lot. **Verify this against the actual pickup spot before
relying on geofence flags.** A test pins the box.

**Freshness and battery.** Reads are cached for 60 s (`FLEET_CACHE_SECONDS`). Smartcar throttles per
vehicle to protect the 12V battery (`429 RATE_LIMIT:VEHICLE`, with `retry-after`). Smartcar recommends
webhooks over polling, but webhooks need a public HTTPS receiver, which a local stdio server does not
have. This server reads on demand only.

**Odometer history.** Smartcar v3 has no history endpoint, only the latest `odometer-traveleddistance`.
Every successful read is appended to `odometer.jsonl` in the state directory, so history starts from
the first use.

## Per-brand capability matrix (Smartcar, US)

Source: Smartcar's public Compatibility API
`https://compatibility.api.smartcar.com/v3/compatible-vehicles?filter[region]=US&filter[make]=<MAKE>`,
retrieved **2026-10-24**. It is bundled in `fleet_telematics/data/compat_us.json`; refresh with
`fleet-auth compat --refresh`. Smartcar builds this from real connections, so "no" means it has **not
been seen working**, not that it can't work. Trims can differ.

| Capability (v3 signal / command) | Ford Transit ICE 2020-26 (t24, t25) | Audi Q5 ICE 2018-26 | Mercedes Sprinter ICE 2017-26 | Chevrolet Suburban ICE 2015-26 |
|---|---|---|---|---|
| Odometer `odometer-traveleddistance` | yes | yes | yes | yes |
| Fuel % `internalcombustionengine-fuellevel` | yes | yes | yes | yes |
| Fuel range `…-range` | yes | yes | yes | yes |
| Location `location-preciselocation` | yes | yes | yes | yes |
| Lock state `closure-islocked` | yes | yes | yes | **no** |
| Door state `closure-doors` | yes | yes | yes | **no** |
| Tire pressure `wheel-tires` | yes | **no** | yes | yes |
| Oil life `internalcombustionengine-oillife` | yes | **no** | **no** | yes |
| Diagnostics (oil/TPMS alerts, DTCs, …) | no | no | brake + washer fluid only | yes (full set) |
| VIN `vehicleidentification-vin` | yes | yes | yes | yes |
| **Lock** command `closure-lock` | yes | yes | yes | yes |
| **Unlock** command `closure-unlock` | yes | yes | yes | yes |

The matrix has separate rows for an Audi Q5 PHEV, which can unlock but has no listed lock command, and
for the BEV Transit and eSprinter, which have no lock or unlock listed. `fleet.toml` sets
`powertrain = "ICE"` for every vehicle. If the Q5 is actually the PHEV ("55 TFSI e"), change it.
**Suburban:** lock state can't be read, so "unlocked while parked" can't be detected for it; it can
still be locked remotely. **Audi:** no tire or oil data.

## Write actions

- Both are refused unless the env `FLEET_ALLOW_WRITE=1` is set **and** `confirm=True` is passed.
  `vehicle_unlock` also needs a `reason` of at least 5 characters.
- The audit log records every attempt: refused, requested, done and failed. Entries include the OS user,
  host, vehicle, VIN, reason and the Smartcar execution id. The log is at
  `~/.local/state/beverley-fleet/fleet-telematics/audit.jsonl` (`FLEET_AUDIT_LOG`), mode 0600, opened
  with `O_APPEND` only. Each line carries the SHA-256 of the previous line, and
  `fleet_config_status().audit_chain` detects any deleted or edited entry. This makes tampering
  evident; it does not prevent it, so back the file up if it matters.
- Smartcar commands can take several minutes. Smartcar holds the connection with a `202` and streams
  the final result, so the client uses a 300 s read timeout. Commands don't return the new state:
  call `vehicle_status` again afterwards. The Build plan includes **100 commands per month across all
  vehicles**.
- Smartcar rejects commands on a moving vehicle (`VEHICLE_STATE:IN_MOTION`).

## Privacy: renters' location

Telematics shows where a renter drives during a paid trip. Recommendations:
- **Disclose it.** Add a clause to the Beverley Fleet rental agreement and the Book page saying the
  vehicles have manufacturer connected services. State that location, odometer, fuel and lock data may
  be read for fleet operations, recovery, overdue returns and safety, and not for marketing. Repeat it
  in any Turo listing.
- **What Turo actually requires (verified 2026-10-24, policy page last updated 2026-10-12).** Turo
  *permits* GPS, telematics and OEM devices. It requires **all hosts** to disclose interior cameras and
  audio recorders and get explicit written consent through Turo messaging. For tracking devices
  generally, it says disclosure is required **"in some locations"**. The US section of that page has
  no general disclosure rule for trackers; it only requires an activated OEM tracker on vehicles worth
  $125k or more. Turo also bans AirTags as active trackers. **So "Turo requires disclosing tracking
  devices in listings" is stricter than Turo's current text for Colorado.** Disclosing anyway is still
  the safe practice, and Turo has publicly said it expects hosts to disclose telematics devices.
- **Colorado law.** No Colorado statute specific to rental-car tracking was found. California
  (Civ. Code §1939.23), Connecticut, Montana and New York do have rules, mainly relevant to renters
  from those states and to the agreement wording. Ask an attorney if you plan to use location for
  anything beyond recovery and fleet operations.
- **Defaults in this server.** `fleet_snapshot` always withholds the precise location of vehicles on a
  trip; it reports only inside or outside ParkDIA. `vehicle_status` shows it only when a
  `reveal_location_reason` is given, and each reveal is audit-logged. Turn this off with
  `FLEET_REDACT_ON_TRIP=0`. Smartcar Connect also shows the vehicle owner (Cristin) a consent screen.
  Renters are not party to that consent, which is why the agreement disclosure matters.

## Costs (Smartcar pricing page, read 2026-10-24)

- **Free**: $0, 1 connected vehicle, 3 simulated, **0 commands per month**, limited signals. Enough to
  trial one vehicle read-only.
- **Build**: "Starting at **$1.99** per vehicle / application / month", up to 500 vehicles, 5
  simulated, **100 commands per month shared**, 7-day logs. It comes in Build Basic, Advanced and
  Premium, and "vehicle signals included will vary between plans. Consuming additional signals may
  incur overages." Only the starting price is published.
  - **Estimate for this fleet:** 5 vehicles × $1.99 ≈ **$10/month**; 6 vehicles ≈ $12/month, plus
    overages. Location, tires and oil may need Advanced or Premium, whose prices aren't published:
    confirm in the Dashboard before connecting. Mid-month connections are prorated, and every connected
    vehicle is charged for the full month at the start of each month.
- **Custom**: 500-vehicle minimum. Not relevant here.
- Note: the pricing page banner says *"Smartcar has been acquired by Everyday Electric (formerly Renew
  Home)"*. That is a vendor-continuity point to watch.

## Other providers (stubs)

Set `provider = "ford_pro"` or `"mercedes_pro"` on a vehicle in `fleet.toml` and every tool returns a
clear "not implemented" explanation instead of data. Implementing one means writing `status()`, `lock()`
and `unlock()` in `fleet_telematics/providers/stubs.py`, returning the normalized dict documented in
`providers/base.py`.

- **Ford Pro Telematics** (developer.ford.com, Ford's commercial-fleet product). API access is
  partner/approval-gated, and the telematics product is a paid subscription. A third-party source quotes
  about $20/vehicle/month, with a free "Essentials" tier; **not verified with Ford**, because Ford's
  developer portal is a JavaScript app that couldn't be read for this build.
  **Worth it when** the van count grows to roughly 5–10+ Transits and you want Ford-native fleet
  features (driver behaviour, idle and fuel reports, maintenance scheduling, Ford's own geofences), or
  if Smartcar's Ford coverage gets worse. For 2–4 vans, Smartcar at about $2/vehicle is much cheaper.
- **Mercedes PRO connect** (Sprinter fleet services, in the US since 2019, through the van's built-in
  LTE module). It offers location, fuel and maintenance intervals in a fleet portal. **No public US
  developer API was found.** Third-party data access appears to need a Mercedes PRO contract
  (unverified).
  **Worth it when** there are several Sprinters and you want Mercedes-native fleet tools (logbook,
  maintenance, driver assignment), or if Smartcar loses Mercedes access. For one Sprinter, Smartcar
  through Mercedes me is simpler.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `FLEET_ALLOW_WRITE` | off | `1` enables lock/unlock (still needs `confirm=True`) |
| `FLEET_CONFIG` | `./fleet.toml` | vehicle map, geofence, thresholds |
| `FLEET_STATE_DIR` | `~/.local/state/beverley-fleet/fleet-telematics` | audit log, odometer history, fallback user-id file, refreshed matrix |
| `FLEET_AUDIT_LOG` | `<state>/audit.jsonl` | audit log path |
| `FLEET_TRIPS_FILE` | — | JSON list of current or upcoming trips |
| `FLEET_REDACT_ON_TRIP` | `1` | withhold on-trip locations unless a reason is given |
| `FLEET_CACHE_SECONDS` | `60` | per-vehicle read cache |
| `FLEET_OP_VAULT` / `FLEET_OP_ITEM` | `Beverley Fleet` / `Smartcar` | 1Password location |
| `FLEET_OP_CLIENT_ID_REF`, `FLEET_OP_CLIENT_SECRET_REF`, `FLEET_OP_APPLICATION_ID_REF` | `op://<vault>/<item>/<field>` | explicit refs |
| `FLEET_OP_BIN` | `op` on PATH | path to the 1Password CLI |
| `FLEET_SMARTCAR_REPLAY_DIR` | — | **test/demo only**: serve recorded fixtures instead of calling Smartcar |
| `FLEET_LOG_LEVEL` | `WARNING` | stderr logging (stdout is the MCP channel) |

Secrets handling: values from `op read` stay in memory only and are never logged, printed or returned.
Error messages name the field, never the value, and tests check this. `op item edit` gets the user id as
an argument, which is op's documented syntax, so the value is briefly visible in the process list of
the same user. A user id alone grants nothing without the client secret.

## Tests

`pytest -q` runs offline. HTTP goes through `httpx2.MockTransport` or a replay transport serving
fixtures in `tests/fixtures/smartcar/`, generated by `tests/make_fixtures.py`. **Those fixtures are
shaped from the documented OpenAPI examples, not recorded from a live account.** The replay transport
also enforces the documented contract: form-encoded client-credentials token request, Bearer token, and
`sc-user-id` on signals and commands. `op` is a fake script.
`tests/test_stdio_smoke.py` starts the real server as a subprocess and speaks MCP over stdio.
`FLEET_LIVE=1 pytest -m live -s` runs one read-only `fleet_snapshot` against the real API, once `op` is
signed in and vehicles are linked.

Smartcar's Vehicle Simulator needs a Smartcar account, so it couldn't be used here. After signing up,
create simulated ICE vehicles in the Dashboard, then run `fleet-auth connect <name> --mode simulated`
and `fleet-auth link <name> --assign <key>=<vehicle_id>`. That exercises the real API, including
lock/unlock, with no rate limits and no real car.

## Verified vs. assumed

**Verified against Smartcar's current docs (2026-10-24) and implemented exactly:**
- v3 base URL `https://vehicle.api.smartcar.com/v3`, token URL, client-credentials form, 1 h tokens, no
  refresh token; `sc-user-id` required on signals and commands.
- Signal codes and body shapes: `odometer-traveleddistance`, `internalcombustionengine-*`,
  `location-preciselocation`, `closure-islocked`, `closure-doors`, `wheel-tires`, `diagnostics-*`,
  `lowvoltagebattery-*`, `transmission-gearstate`, `motion-currentspeed`,
  `connectivitystatus-isonline`, `vehicleidentification-vin`.
- Signal envelope: `data[].attributes{code,status{value,error},body}`, `meta{retrievedAt,oemUpdatedAt}`.
- Command paths `commands/security/lock|unlock`, the `command-execution` response, `202` keep-alive
  behaviour, and FAILURE carried in the body.
- `GET /v3/connections` filters and page parameters; vehicle id at
  `relationships.vehicle.data.id`; connection status values ACTIVE, DEGRADED and DISABLED.
- Connect URL parameters (`response_type=none`, `application_id`, `redirect_uri`, `scope`, `state`,
  `mode`, `make`, `external_id`, `flags=country:US`, `approval_prompt`) and redirect parameters
  (`user_id`, `state`, `error`, `error_description`). Make codes are `ford`, `audi`, `mercedes_benz`
  and `chevrolet`. Localhost http redirects are allowed.
- Permission names, the error envelope, `resolution.type`, and 429 `retry-after`.
- The US compatibility matrix above, taken from the live public API, plus pricing and v2 deprecation.
- Turo's tracking policy text, and the ParkDIA lot location from OpenStreetMap.

**Assumed or not verified:**
- **Never run against a live Smartcar account**, since none was available. Real responses may differ
  from the doc examples in small ways. Known doc inconsistency: `closure-doors` uses `values` in the
  schema and `doors` in the example; the code handles both.
- `GET /signals` is assumed to return all signals in one page. The docs show paging metadata but no
  page parameters, so the code falls back to per-signal GETs for wanted codes if `totalCount` is larger.
- `application_id` is assumed to be a separate value from the API `client_id`; both are stored.
- Which Build sub-plan includes location, tires and oil. Brand-account prerequisites (OnStar plan,
  primary-owner account, Ford Pro vs. Ford app). The Q5 powertrain (ICE vs. PHEV). The Sprinter's
  powertrain and trim.
- Ford Pro and Mercedes PRO access terms and pricing (stubs only).
- The exact ParkDIA pickup point: the OSM lot centroid is used; confirm it.
- Fleet roster: this server has the 5 vehicles from the brief. Other Beverley Fleet records also list a
  2020 and a 2023 Transit (VINs are noted at the bottom of `fleet.toml`), and do not list the 2026
  Sprinter. Reconcile the roster.

## Sources
- Smartcar docs index: https://smartcar.com/docs/llms.txt
- API overview: https://smartcar.com/docs/api-reference/intro
- API authentication: https://smartcar.com/docs/getting-started/how-to/api-authentication and https://smartcar.com/docs/api-reference/authorization/request-access-token
- V2→V3 migration (v2 deprecated 2026-12-01): https://smartcar.com/docs/getting-started/how-to/m2m/migration-guide
- Signals: https://smartcar.com/docs/api-reference/list-signals, https://smartcar.com/docs/api-reference/signals/schema
- Commands: https://smartcar.com/docs/api-reference/commands/overview, https://smartcar.com/docs/api-reference/security/lock-doors
- Connections: https://smartcar.com/docs/api-reference/list-connections
- Connect: https://smartcar.com/docs/connect/redirect-to-connect, https://smartcar.com/docs/connect/handle-the-response, https://smartcar.com/docs/connect/dashboard-config, https://smartcar.com/docs/api-reference/makes
- Permissions: https://smartcar.com/docs/api-reference/permissions
- Rate limits: https://smartcar.com/docs/errors/api-errors/v3/rate-limit-errors
- Simulator: https://smartcar.com/docs/getting-started/dashboard/simulator
- Compatibility API: https://smartcar.com/docs/api-reference/compatible-vehicles (data: https://compatibility.api.smartcar.com/v3/compatible-vehicles)
- Pricing: https://smartcar.com/pricing
- Turo host tracking policy: https://help.turo.com/en_us/tracking-and-technology-devices-SkF48Nl49
- Turo spokesperson on telematics disclosure: https://www.vice.com/en/article/couple-who-rents-cars-brags-about-tracking-customers-on-tiktok/
- ParkDIA lot: https://www.openstreetmap.org/way/1131040629 ; US Census geocoder: https://geocoding.geo.census.gov
- Ford Pro Telematics price (third-party, unverified): https://www.getjerry.com/car-insurance/ford-telematics
- Mercedes PRO connect (US): https://www.freightlinersprinterusa.com/content/mb-vans/en_us/mercedes-pro-connect.html
- State rental-tracking laws overview: https://www.autorentalnews.com/articles/understanding-legalities-around-telematics-rental-fleet-electrification
