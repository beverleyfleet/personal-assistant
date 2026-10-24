"""Smartcar Vehicle API v3 provider.

Implemented against the Smartcar docs as read on 2026-10-24 (see README "Sources"):
  * Auth: OAuth2 client credentials at POST https://iam.smartcar.com/oauth2/token
    (form: grant_type=client_credentials, client_id, client_secret) -> {access_token, token_type,
    expires_in=3600}. No refresh token; request a new one when it expires.
  * Data: GET https://vehicle.api.smartcar.com/v3/vehicles/{id}/signals  (header sc-user-id required)
          GET .../v3/vehicles/{id}/signals/{signalCode}
  * Commands: POST .../v3/vehicles/{id}/commands/security/lock | security/unlock  (no body;
    may answer 202 and stream the final JSON on the same connection, up to a few minutes)
  * Connections: GET .../v3/connections?filter[userId]=...&page[number]=..&page[size]=..
    vehicle id lives at data[].relationships.vehicle.data.id
  * Errors: {"errors":[{status,type,code,title,detail,resolution{type},suggestedUserMessage}]}
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx2

from fleet_telematics import credentials
from fleet_telematics.config import Vehicle
from fleet_telematics.providers.base import NotLinked, Provider, ProviderError

log = logging.getLogger(__name__)

TOKEN_URL = "https://iam.smartcar.com/oauth2/token"
VEHICLE_API = "https://vehicle.api.smartcar.com/v3"
CONNECT_URL = "https://connect.smartcar.com/oauth/authorize"

# Signals this server reads (v3 signal codes). Permission in brackets.
WANTED_SIGNALS = (
    "odometer-traveleddistance",            # [read_odometer]
    "internalcombustionengine-fuellevel",   # [read_fuel]
    "internalcombustionengine-range",       # [read_fuel]
    "internalcombustionengine-amountremaining",  # [read_fuel]
    "location-preciselocation",             # [read_location]
    "closure-islocked",                     # [read_security]
    "closure-doors",                        # [read_security]
    "wheel-tires",                          # [read_tires]
    "internalcombustionengine-oillife",     # [read_engine_oil]
    "diagnostics-oillife",                  # [read_diagnostics]
    "diagnostics-tirepressure",             # [read_diagnostics]
    "lowvoltagebattery-stateofcharge",
    "lowvoltagebattery-status",
    "transmission-gearstate",
    "motion-currentspeed",
    "connectivitystatus-isonline",
    "vehicleidentification-vin",            # [read_vin]
)

# Connect permissions requested by `fleet-auth connect` (least privilege for the tools above).
DEFAULT_SCOPES = ("read_vehicle_info", "read_vin", "read_odometer", "read_fuel", "read_location",
                  "read_security", "read_tires", "read_engine_oil", "read_diagnostics")
WRITE_SCOPES = ("control_security",)

KM_PER_MI = 1.609344
KPA_PER_PSI = 6.894757


def _to_miles(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    u = (unit or "km").lower()
    if u in ("mi", "mile", "miles"):
        return round(float(value), 1)
    return round(float(value) / KM_PER_MI, 1)


def _to_psi(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    u = (unit or "kpa").lower()
    if u == "psi":
        return round(float(value), 1)
    if u == "bar":
        return round(float(value) * 100 / KPA_PER_PSI, 1)
    return round(float(value) / KPA_PER_PSI, 1)


def _to_mph(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1) if (unit or "").lower() in ("mph", "mi/h") else round(float(value) / KM_PER_MI, 1)


class SmartcarAPIError(ProviderError):
    def __init__(self, status: int, err: dict[str, Any] | None = None, message: str | None = None):
        err = err or {}
        self.status = status
        self.type = err.get("type")
        self.code = err.get("code")
        self.resolution = (err.get("resolution") or {}).get("type")
        detail = message or err.get("detail") or err.get("title") or f"HTTP {status}"
        hint = {
            "REAUTHENTICATE": " Re-run `fleet-auth connect` for this brand account.",
            "RETRY_LATER": " Retry later.",
        }.get(self.resolution or "", "")
        super().__init__(f"Smartcar {status} {self.type or ''}:{self.code or ''} - {detail}{hint}".replace(" :", " "))


class SmartcarClient:
    """Thin async client. Pass `transport` (httpx2.MockTransport or a replay transport) in tests."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None,
                 client_id: str | None = None, client_secret: str | None = None):
        self._transport = transport
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()
        self._http: httpx2.AsyncClient | None = None

    def _client(self) -> httpx2.AsyncClient:
        if self._http is None:
            # Commands can stay open ~3+ minutes (202 keep-alive), so allow a long read timeout.
            self._http = httpx2.AsyncClient(transport=self._transport,
                                            timeout=httpx2.Timeout(30.0, read=300.0),
                                            headers={"User-Agent": "beverley-fleet-telematics/0.1"})
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _creds(self) -> tuple[str, str]:
        if not self._client_id:
            self._client_id = credentials.op_read(credentials.ref("client_id"))
        if not self._client_secret:
            self._client_secret = credentials.op_read(credentials.ref("client_secret"))
        return self._client_id, self._client_secret

    async def token(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.monotonic() < self._token_exp:
                return self._token
            cid, secret = await asyncio.to_thread(self._creds)
            r = await self._client().post(TOKEN_URL, data={
                "grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
            if r.status_code != 200:
                code = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error")
                raise SmartcarAPIError(r.status_code, message=f"token request failed ({code or 'no detail'}); "
                                       "check client_id/client_secret in 1Password")
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 120)
            return self._token

    async def request(self, method: str, path: str, user_id: str | None = None, **kw) -> dict[str, Any]:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {await self.token(force=attempt == 1)}"}
            if user_id:
                headers["sc-user-id"] = user_id
            url = path if path.startswith("http") else f"{VEHICLE_API}{path}"
            try:
                r = await self._client().request(method, url, headers=headers, **kw)
            except httpx2.TimeoutException:
                raise ProviderError(f"Smartcar request timed out: {method} {path}") from None
            except httpx2.TransportError as e:
                raise ProviderError(f"network error talking to Smartcar: {type(e).__name__}") from None
            if r.status_code == 401 and attempt == 0:
                continue  # token expired early or was revoked: fetch a new one once
            break
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            err = (body.get("errors") or [{}])[0] if isinstance(body, dict) else {}
            exc = SmartcarAPIError(r.status_code, err)
            if r.status_code == 429 and r.headers.get("retry-after"):
                exc.args = (f"{exc.args[0]} (retry after {r.headers['retry-after']}s)",)
            raise exc
        return body

    # ---- endpoints ---------------------------------------------------------------------------

    async def list_connections(self, user_id: str | None = None, external_id: str | None = None,
                               mode: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page[size]": 50}
        if user_id:
            params["filter[userId]"] = user_id
        if external_id:
            params["filter[user.externalId]"] = external_id
        if mode:
            params["filter[vehicle.mode]"] = mode
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self.request("GET", "/connections", params={**params, "page[number]": page})
            out.extend(body.get("data", []))
            total = (body.get("meta") or {}).get("totalCount", len(out))
            if not body.get("data") or len(out) >= total or page >= 20:
                return out
            page += 1

    async def get_vehicle(self, vehicle_id: str) -> dict[str, Any]:
        return (await self.request("GET", f"/vehicles/{vehicle_id}"))["data"]

    async def signals(self, vehicle_id: str, user_id: str,
                      wanted: tuple[str, ...] = WANTED_SIGNALS) -> dict[str, dict[str, Any]]:
        """All signals keyed by code. Falls back to per-signal GETs for wanted codes missing from a
        truncated list response (the list endpoint documents paging metadata but no page params)."""
        body = await self.request("GET", f"/vehicles/{vehicle_id}/signals", user_id=user_id)
        res = {s["attributes"]["code"]: s for s in body.get("data", [])}
        total = (body.get("meta") or {}).get("totalCount", len(res))
        if total > len(res):
            for code in [c for c in wanted if c not in res]:
                try:
                    one = await self.request("GET", f"/vehicles/{vehicle_id}/signals/{code}", user_id=user_id)
                    res[code] = one["data"]
                except SmartcarAPIError as e:
                    if e.status not in (400, 403, 404, 501):
                        raise
        return res

    async def command(self, vehicle_id: str, user_id: str, command: str) -> dict[str, Any]:
        body = await self.request("POST", f"/vehicles/{vehicle_id}/commands/{command}", user_id=user_id)
        data = body.get("data", {})
        attrs = data.get("attributes", {})
        st = attrs.get("status", {})
        if st.get("value") == "FAILURE":
            raise SmartcarAPIError(int((st.get("error") or {}).get("status", 500)), st.get("error"))
        return {"execution_id": data.get("id"), "command": attrs.get("commandType"),
                "status": st.get("value"), **(data.get("meta") or {})}


def _sig(signals: dict[str, dict], code: str) -> tuple[dict | None, dict]:
    """(body, info) for a signal; body None unless status SUCCESS."""
    s = signals.get(code)
    if not s:
        return None, {"status": "NOT_RETURNED", "as_of": None, "retrieved_at": None}
    a = s.get("attributes", {})
    st = a.get("status", {})
    meta = s.get("meta", {}) or {}
    info = {"status": st.get("value"), "as_of": meta.get("oemUpdatedAt") or meta.get("retrievedAt"),
            "retrieved_at": meta.get("retrievedAt")}
    if st.get("value") != "SUCCESS":
        err = st.get("error") or {}
        info["error"] = f"{err.get('type', '')}:{err.get('code', '')}".strip(":") or None
        return None, info
    return a.get("body") or {}, info


def normalize(signals: dict[str, dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    b, i = _sig(signals, "odometer-traveleddistance")
    out["odometer"] = None if b is None else {"miles": _to_miles(b.get("value"), b.get("unit")),
                                              "raw": b, "as_of": i["as_of"]}

    fl, fi = _sig(signals, "internalcombustionengine-fuellevel")
    rg, ri = _sig(signals, "internalcombustionengine-range")
    am, _ = _sig(signals, "internalcombustionengine-amountremaining")
    if fl is None and rg is None:
        out["fuel"] = None
    else:
        gal = None
        if am and am.get("value") is not None:
            gal = round(am["value"] / 3.785411784, 1) if (am.get("unit") or "liters").lower().startswith("l") else am["value"]
        out["fuel"] = {"percent": None if fl is None else fl.get("value"),
                       "range_miles": None if rg is None else _to_miles(rg.get("value"), rg.get("unit")),
                       "gallons_remaining": gal, "as_of": fi["as_of"] or ri["as_of"]}

    b, i = _sig(signals, "location-preciselocation")
    out["location"] = None if b is None or b.get("latitude") is None else {
        "lat": b["latitude"], "lon": b["longitude"], "heading": b.get("heading"),
        "direction": b.get("direction"), "location_type": b.get("locationType"), "as_of": i["as_of"]}

    lk, li = _sig(signals, "closure-islocked")
    dr, di = _sig(signals, "closure-doors")
    doors = (dr or {}).get("values") or (dr or {}).get("doors") or []
    door_locked = [d.get("isLocked") for d in doors if d.get("isLocked") is not None]
    is_locked = (lk or {}).get("value")
    source = "closure-islocked"
    if is_locked is None and door_locked:
        is_locked, source = all(door_locked), "closure-doors"
    out["lock"] = None if is_locked is None and not doors else {
        "is_locked": is_locked, "source": source if is_locked is not None else None,
        "doors_open": [f"row{d.get('row')}_col{d.get('column')}" for d in doors if d.get("isOpen")],
        "as_of": li["as_of"] or di["as_of"]}

    b, i = _sig(signals, "wheel-tires")
    if b is None:
        out["tires"] = None
    else:
        vals = b.get("values") or []
        rows, cols = b.get("rowCount"), b.get("columnCount")
        names = {(0, 0): "front_left", (0, 1): "front_right", (1, 0): "rear_left", (1, 1): "rear_right"}
        pressures = {}
        for w in vals:
            key = (w.get("row"), w.get("column"))
            label = names[key] if rows == 2 and cols == 2 and key in names else f"row{key[0]}_col{key[1]}"
            pressures[label] = _to_psi(w.get("tirePressure"), b.get("unit"))
        out["tires"] = {"unit": "psi", "pressures": pressures, "as_of": i["as_of"]}
    ta, _ = _sig(signals, "diagnostics-tirepressure")
    if ta is not None:
        out["tires"] = {**(out["tires"] or {}), "alert": ta.get("status")}

    ol, oi = _sig(signals, "internalcombustionengine-oillife")
    oa, oai = _sig(signals, "diagnostics-oillife")
    out["oil"] = None if ol is None and oa is None else {
        "life_percent": None if ol is None else ol.get("value"),
        "alert": None if oa is None else oa.get("status"), "as_of": oi["as_of"] or oai["as_of"]}

    bs, bi = _sig(signals, "lowvoltagebattery-stateofcharge")
    bt, _ = _sig(signals, "lowvoltagebattery-status")
    out["battery_12v"] = None if bs is None and bt is None else {
        "percent": None if bs is None else bs.get("value"), "status": None if bt is None else bt.get("value"),
        "as_of": bi["as_of"]}

    g, _ = _sig(signals, "transmission-gearstate")
    sp, _ = _sig(signals, "motion-currentspeed")
    on, _ = _sig(signals, "connectivitystatus-isonline")
    out["motion"] = {"gear": None if g is None else g.get("value"),
                     "speed_mph": None if sp is None else _to_mph(sp.get("value"), sp.get("unit")),
                     "is_online": None if on is None else on.get("value")}

    vb, _ = _sig(signals, "vehicleidentification-vin")
    out["reported_vin"] = None if vb is None else vb.get("value")
    out["signals"] = {code: _sig(signals, code)[1] for code in WANTED_SIGNALS}
    return out


class SmartcarProvider(Provider):
    name = "smartcar"

    def __init__(self, client: SmartcarClient | None = None):
        self.client = client or SmartcarClient(transport=_transport_from_env())
        self._user_ids: dict[str, str] = {}
        self._cache: dict[str, tuple[float, dict]] = {}
        self.cache_seconds = float(os.environ.get("FLEET_CACHE_SECONDS", "60"))

    async def _user_id(self, v: Vehicle) -> str:
        if not v.smartcar_vehicle_id:
            raise NotLinked(f"{v.key}: not linked to Smartcar yet. Run `fleet-auth connect {v.smartcar_account or '<account>'}` "
                            "and complete the brand login; it fills smartcar_vehicle_id in fleet.toml.")
        acct = v.smartcar_account
        if not acct:
            raise ProviderError(f"{v.key}: smartcar_account is empty in fleet.toml")
        if acct not in self._user_ids:
            try:
                self._user_ids[acct] = await asyncio.to_thread(credentials.get_user_id, acct)
            except credentials.SecretError as e:
                raise ProviderError(str(e)) from None
        return self._user_ids[acct]

    async def status(self, v: Vehicle) -> dict[str, Any]:
        hit = self._cache.get(v.key)
        if hit and time.monotonic() - hit[0] < self.cache_seconds:
            return {**hit[1], "cached": True}
        uid = await self._user_id(v)
        try:
            sigs = await self.client.signals(v.smartcar_vehicle_id, uid)
        except credentials.SecretError as e:
            raise ProviderError(str(e)) from None
        st = normalize(sigs)
        st["retrieved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._cache[v.key] = (time.monotonic(), st)
        return {**st, "cached": False}

    async def _cmd(self, v: Vehicle, command: str) -> dict[str, Any]:
        uid = await self._user_id(v)
        try:
            res = await self.client.command(v.smartcar_vehicle_id, uid, command)
        except credentials.SecretError as e:
            raise ProviderError(str(e)) from None
        self._cache.pop(v.key, None)
        return res

    async def lock(self, v: Vehicle) -> dict[str, Any]:
        return await self._cmd(v, "security/lock")

    async def unlock(self, v: Vehicle) -> dict[str, Any]:
        return await self._cmd(v, "security/unlock")

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "api": VEHICLE_API, "token_url": TOKEN_URL,
                "replay_mode": bool(os.environ.get("FLEET_SMARTCAR_REPLAY_DIR"))}


def _transport_from_env() -> httpx2.AsyncBaseTransport | None:
    d = os.environ.get("FLEET_SMARTCAR_REPLAY_DIR")
    if not d:
        return None
    from fleet_telematics.providers.replay import ReplayTransport

    log.warning("Smartcar REPLAY mode: serving recorded fixtures from %s (no real API calls)", d)
    return ReplayTransport(d)
