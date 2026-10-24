"""MCP server (stdio): real-time vehicle data for Beverley Fleet.

Read-only by default. Lock/unlock exist but refuse unless env FLEET_ALLOW_WRITE=1 AND the call
passes confirm=True; unlock also needs a `reason`. Every write attempt goes to the audit log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from fleet_telematics import audit, compat, credentials, history
from fleet_telematics.config import FleetConfig, Vehicle, env_flag, load_config
from fleet_telematics.geo import in_geofence
from fleet_telematics.providers import PROVIDERS, Provider, ProviderError

# stdout is the MCP channel: log to stderr only, never secrets.
logging.basicConfig(stream=sys.stderr, level=os.environ.get("FLEET_LOG_LEVEL", "WARNING"))

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

mcp = MCPServer(
    name="fleet-telematics",
    instructions=(
        "Real-time vehicle data for Beverley Fleet (Denver car rental; home base ParkDIA near DEN) via Smartcar. "
        "Read tools: fleet_list, vehicle_status, fleet_snapshot, vehicle_odometer_history, fleet_config_status. "
        "Vehicles can be named by key (audi, sub, t24, t25, sprinter), nickname (Joker, Heaps), plate or VIN. "
        "For fleet_snapshot, pass vehicles_on_trip (e.g. from the booking calendar) so 'unlocked' and "
        "'outside ParkDIA' flags mean something. Renter location during trips is private: do not reveal it "
        "unless the owner states a reason (theft, overdue return, accident). vehicle_lock/vehicle_unlock "
        "physically act on a car: never call them without the owner's explicit instruction in this conversation."
    ),
)

_providers: dict[str, Provider] = {}


def _provider(name: str) -> Provider:
    if name not in PROVIDERS:
        raise ToolError(f"unknown provider '{name}' in fleet.toml (known: {', '.join(PROVIDERS)})")
    if name not in _providers:
        _providers[name] = PROVIDERS[name]()
    return _providers[name]


def _cfg() -> FleetConfig:
    try:
        return load_config()
    except FileNotFoundError as e:
        raise ToolError(f"fleet config not found: {e.filename}. Set FLEET_CONFIG or create fleet.toml") from None
    except (ValueError, KeyError) as e:
        raise ToolError(f"fleet config is invalid: {e}") from None


def _vehicle(cfg: FleetConfig, name: str) -> Vehicle:
    try:
        return cfg.find(name)
    except LookupError as e:
        raise ToolError(str(e)) from None


# ---- trips ------------------------------------------------------------------------------------

def _trip_keys(cfg: FleetConfig, vehicles_on_trip: list[str] | None) -> set[str] | None:
    """Vehicles currently on a rental. None = unknown (no information given)."""
    keys: set[str] | None = None
    if vehicles_on_trip is not None:
        keys = {_vehicle(cfg, n).key for n in vehicles_on_trip}
    trips_file = os.environ.get("FLEET_TRIPS_FILE")
    if trips_file and Path(trips_file).expanduser().exists():
        keys = keys or set()
        now = datetime.now(UTC)
        for t in json.loads(Path(trips_file).expanduser().read_text()):
            start = datetime.fromisoformat(t["start"]).astimezone(UTC)
            end = datetime.fromisoformat(t["end"]).astimezone(UTC)
            if start <= now <= end:
                keys.add(_vehicle(cfg, t["vehicle"]).key)
    return keys


def _trip_state(key: str, trips: set[str] | None) -> str:
    if trips is None:
        return "unknown"
    return "on_trip" if key in trips else "available"


# ---- status shaping ---------------------------------------------------------------------------

def _geo(cfg: FleetConfig, loc: dict | None) -> dict | None:
    if not loc:
        return None
    inside, d = in_geofence(loc["lat"], loc["lon"], cfg.geofence.lat, cfg.geofence.lon, cfg.geofence.radius_m)
    return {"name": cfg.geofence.name, "inside": inside, "distance_km": round(d / 1000, 2)}


def _redact_location(st: dict, geo: dict | None) -> None:
    if st.get("location"):
        st["location"] = {"redacted": True, "as_of": st["location"].get("as_of"),
                          "why": "vehicle is on a rental trip; pass reveal_location_reason to vehicle_status to see it"}
    if geo:
        geo["distance_km"] = None if not geo["inside"] else geo["distance_km"]


async def _status(cfg: FleetConfig, v: Vehicle) -> dict[str, Any]:
    st = await _provider(v.provider).status(v)
    st = json.loads(json.dumps(st))  # copy: never mutate the provider cache
    odo = st.get("odometer") or {}
    try:
        history.record(v.key, odo.get("miles"), odo.get("as_of"))
    except OSError:
        logging.getLogger(__name__).warning("could not write odometer history")
    if st.get("reported_vin") and v.vin and st["reported_vin"].upper() != v.vin.upper():
        st["warning"] = f"VIN mismatch: fleet.toml has {v.vin}, vehicle reports {st['reported_vin']}"
    caps = compat.capabilities(v)["features"]
    st["unsupported_per_smartcar_matrix"] = sorted(k for k, ok in caps.items() if ok is False)
    return st


def _flags(cfg: FleetConfig, v: Vehicle, st: dict, geo: dict | None, trip: str) -> list[dict]:
    flags = []
    fuel = st.get("fuel") or {}
    pct = fuel.get("percent")
    if pct is not None and pct < cfg.low_fuel_percent:
        flags.append({"flag": "low_fuel", "severity": "warn", "detail": f"fuel {pct}% (< {cfg.low_fuel_percent:g}%)"})
    lock = st.get("lock") or {}
    if lock.get("is_locked") is False and trip != "on_trip":
        flags.append({"flag": "unlocked_not_on_trip", "severity": "warn" if trip == "available" else "info",
                      "detail": "unlocked and not on a rental" if trip == "available"
                      else "unlocked; trip status unknown (pass vehicles_on_trip)"})
    if lock.get("doors_open") and trip != "on_trip":
        flags.append({"flag": "door_open", "severity": "warn", "detail": ", ".join(lock["doors_open"])})
    if st.get("lock") is None and "lock_state" in st.get("unsupported_per_smartcar_matrix", []):
        flags.append({"flag": "lock_state_unavailable", "severity": "info",
                      "detail": "Smartcar does not report lock state for this model (US matrix)"})
    if geo and not geo["inside"] and trip != "on_trip":
        flags.append({"flag": "outside_geofence", "severity": "warn" if trip == "available" else "info",
                      "detail": f"{geo['distance_km']} km from {geo['name']}"})
    tires = st.get("tires") or {}
    if tires.get("alert") == "ALERT":
        flags.append({"flag": "tire_pressure_alert", "severity": "warn", "detail": "TPMS alert"})
    oil = st.get("oil") or {}
    if oil.get("alert") == "ALERT" or (oil.get("life_percent") is not None and oil["life_percent"] <= 10):
        flags.append({"flag": "oil_service", "severity": "warn", "detail": f"oil life {oil.get('life_percent')}%"})
    stamps = [x.get("as_of") for x in (st.get("odometer"), st.get("fuel"), st.get("location"), st.get("lock")) if x]
    stamps = [s for s in stamps if s]
    if stamps:
        newest = max(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
        if datetime.now(UTC) - newest > timedelta(hours=24):
            flags.append({"flag": "stale_data", "severity": "info",
                          "detail": f"newest vehicle timestamp {newest.isoformat()} (vehicle asleep/offline?)"})
    return flags


# ---- tools -------------------------------------------------------------------------------------

@mcp.tool(annotations=LOCAL, structured_output=True)
def fleet_list() -> dict[str, Any]:
    """List the fleet from fleet.toml: key, name, nickname, VIN, plate, provider, whether it is linked
    to Smartcar, and which status fields Smartcar's US compatibility matrix says the model supports.
    Local only; no API calls."""
    cfg = _cfg()
    out = []
    for v in cfg.vehicles:
        caps = compat.capabilities(v)
        out.append({**v.public(), "smartcar_matrix_row": caps["matrix_row"], "supports": caps["features"]})
    return {"vehicles": out, "geofence": vars(cfg.geofence), "config": str(cfg.path)}


@mcp.tool(annotations=READ, structured_output=True)
async def vehicle_status(vehicle: str, reveal_location_reason: str | None = None,
                         vehicles_on_trip: list[str] | None = None) -> dict[str, Any]:
    """Live status for one vehicle: odometer (miles), fuel % / range, GPS location + distance to the
    ParkDIA geofence, lock state and open doors, tire pressures (psi), oil life, 12V battery where the
    model supports it, each with the OEM timestamp (`as_of`). `vehicle` = key, nickname, plate or VIN.
    If the vehicle is on a rental trip (per vehicles_on_trip or FLEET_TRIPS_FILE) its precise location
    is withheld unless `reveal_location_reason` is given (e.g. "overdue return 26h"); reveals are audit-logged.
    Results are cached for FLEET_CACHE_SECONDS (default 60) to spare the vehicle's 12V battery."""
    cfg = _cfg()
    v = _vehicle(cfg, vehicle)
    try:
        st = await _status(cfg, v)
    except ProviderError as e:
        raise ToolError(str(e)) from None
    trips = _trip_keys(cfg, vehicles_on_trip)
    trip = _trip_state(v.key, trips)
    geo = _geo(cfg, st.get("location"))
    if trip == "on_trip" and env_flag_default("FLEET_REDACT_ON_TRIP", True):
        if reveal_location_reason and reveal_location_reason.strip():
            audit.append("location_reveal", vehicle=v.key, vin=v.vin, reason=reveal_location_reason.strip())
        else:
            _redact_location(st, geo)
    return {"vehicle": v.public(), "trip_status": trip, "geofence": geo,
            "flags": _flags(cfg, v, st, geo, trip), **st}


def env_flag_default(name: str, default: bool) -> bool:
    return default if os.environ.get(name) is None else env_flag(name)


@mcp.tool(annotations=READ, structured_output=True)
async def fleet_snapshot(vehicles_on_trip: list[str] | None = None) -> dict[str, Any]:
    """Status of every vehicle at once, with flags: low_fuel (< 25%), unlocked_not_on_trip, door_open,
    outside_geofence (ParkDIA, 25200 E 68th Ave, Aurora CO), tire/oil alerts, stale_data. Pass
    vehicles_on_trip (names/keys of cars currently rented, e.g. from the booking calendar); without it,
    trip status is 'unknown' and unlock/geofence flags are only 'info'. On-trip vehicles' precise
    locations are always withheld here. A failure on one vehicle does not fail the snapshot."""
    cfg = _cfg()
    trips = _trip_keys(cfg, vehicles_on_trip)

    async def one(v: Vehicle) -> dict[str, Any]:
        trip = _trip_state(v.key, trips)
        try:
            st = await _status(cfg, v)
        except ProviderError as e:
            return {"vehicle": v.public(), "trip_status": trip, "error": str(e), "flags": [
                {"flag": "no_data", "severity": "error", "detail": str(e)}]}
        geo = _geo(cfg, st.get("location"))
        flags = _flags(cfg, v, st, geo, trip)
        if trip == "on_trip":
            _redact_location(st, geo)
        st.pop("signals", None)
        return {"vehicle": v.public(), "trip_status": trip, "geofence": geo, "flags": flags, **st}

    rows = await asyncio.gather(*(one(v) for v in cfg.vehicles))
    summary = {f["flag"]: [] for r in rows for f in r["flags"]}
    for r in rows:
        for f in r["flags"]:
            summary[f["flag"]].append(r["vehicle"]["key"])
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "trip_info": "unknown" if trips is None else sorted(trips),
            "flag_summary": summary, "vehicles": rows}


@mcp.tool(annotations=LOCAL, structured_output=True)
def vehicle_odometer_history(vehicle: str, since: str | None = None) -> dict[str, Any]:
    """Odometer readings this server has recorded for a vehicle (Smartcar has no history endpoint;
    every vehicle_status / fleet_snapshot read is logged locally). `since` = ISO date, e.g. 2026-10-01.
    Also returns miles driven between the first and last reading in range."""
    cfg = _cfg()
    v = _vehicle(cfg, vehicle)
    r = history.readings(v.key, since)
    driven = round(r[-1]["miles"] - r[0]["miles"], 1) if len(r) >= 2 else None
    return {"vehicle": v.key, "source": "local log of Smartcar odometer reads", "readings": r,
            "miles_driven_in_range": driven, "count": len(r)}


@mcp.tool(annotations=LOCAL, structured_output=True)
def fleet_config_status() -> dict[str, Any]:
    """Diagnostics (no secrets): config path, which vehicles are linked, 1Password refs and whether `op`
    is installed, write-gate state, audit-log path and hash-chain check, replay mode."""
    cfg = _cfg()
    return {
        "config": str(cfg.path),
        "vehicles": {v.key: {"provider": v.provider, "linked": bool(v.smartcar_vehicle_id),
                             "account": v.smartcar_account} for v in cfg.vehicles},
        "credentials": credentials.status(),
        "write_enabled": env_flag("FLEET_ALLOW_WRITE"),
        "audit_log": str(audit.log_path()), "audit_chain": audit.verify(),
        "smartcar": _provider("smartcar").describe(),
        "redact_location_on_trip": env_flag_default("FLEET_REDACT_ON_TRIP", True),
    }


class WriteBlocked(ToolError, PermissionError):
    """Write refused by the gate."""


def _gate(action: str, v: Vehicle, confirm: bool, reason: str | None) -> None:
    def refuse(msg: str):
        audit.append(f"{action}_refused", vehicle=v.key, vin=v.vin, reason=reason, detail=msg)
        raise WriteBlocked(f"{action} {v.key}: {msg}")

    if not env_flag("FLEET_ALLOW_WRITE"):
        refuse("write actions are disabled (FLEET_ALLOW_WRITE is not set to 1 for this server)")
    if confirm is not True:
        refuse("confirm=True was not passed")
    if action == "unlock" and (not reason or len(reason.strip()) < 5):
        refuse("unlock requires a `reason` (at least 5 characters), e.g. 'renter locked keys inside, booking BF-2026-11-02-t24'")


async def _write(action: str, vehicle: str, confirm: bool, reason: str | None) -> dict[str, Any]:
    cfg = _cfg()
    v = _vehicle(cfg, vehicle)
    _gate(action, v, confirm, reason)
    audit.append(f"{action}_requested", vehicle=v.key, vin=v.vin, provider=v.provider, reason=reason)
    try:
        res = await getattr(_provider(v.provider), action)(v)
    except ProviderError as e:
        audit.append(f"{action}_failed", vehicle=v.key, vin=v.vin, reason=reason, detail=str(e))
        raise ToolError(str(e)) from None
    audit.append(f"{action}_done", vehicle=v.key, vin=v.vin, reason=reason, result=res)
    return {"vehicle": v.key, "action": action, "result": res,
            "note": "Smartcar commands do not return new state; call vehicle_status after a minute to confirm.",
            "audit_log": str(audit.log_path())}


@mcp.tool(annotations=WRITE, structured_output=True)
async def vehicle_lock(vehicle: str, confirm: bool = False) -> dict[str, Any]:
    """LOCK a vehicle's doors (real command to the car). Refused unless the server runs with
    FLEET_ALLOW_WRITE=1 and confirm=True. Only call when the owner explicitly asks. Every attempt is
    audit-logged. Smartcar Build plan includes 100 commands/month across all vehicles."""
    return await _write("lock", vehicle, confirm, None)


@mcp.tool(annotations=WRITE, structured_output=True)
async def vehicle_unlock(vehicle: str, confirm: bool = False, reason: str = "") -> dict[str, Any]:
    """UNLOCK a vehicle's doors (real command to the car). Refused unless FLEET_ALLOW_WRITE=1,
    confirm=True AND a `reason` is given. Only call when the owner explicitly asks in this conversation.
    Every attempt (including refused ones) is written to the append-only audit log with the reason."""
    return await _write("unlock", vehicle, confirm, reason)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
