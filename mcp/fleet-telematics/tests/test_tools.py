"""Tool functions called in-process against the replay fixtures."""
import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fleet_telematics import audit, server

from conftest import SECRET


def run(coro_fn, *a, **kw):
    return anyio.run(lambda: coro_fn(*a, **kw))


def test_fleet_list_has_matrix_and_lookup(fleet_env):
    out = server.fleet_list()
    by = {v["key"]: v for v in out["vehicles"]}
    assert set(by) == {"audi", "sub", "t24", "t25", "sprinter"}
    assert by["sub"]["supports"]["lock_state"] is False          # Suburban: no closure-islocked in US matrix
    assert by["sub"]["supports"]["unlock_command"] is True
    assert by["audi"]["supports"]["tire_pressure"] is False      # Q5 ICE: no wheel-tires in US matrix
    assert by["t24"]["supports"]["oil_life"] is True
    assert by["sprinter"]["linked"] is False


@pytest.mark.parametrize("name,key", [("Joker", "t24"), ("heaps", "t25"), ("EIUY09", "audi"), ("temp 8620436", "t24"),
                                      ("1FBAX9CGXSKA05649", "t25"), ("A05649", "t25"), ("suburban", "sub")])
def test_vehicle_lookup(fleet_env, name, key):
    assert run(server.vehicle_status, name)["vehicle"]["key"] == key


def test_vehicle_status_fields(fleet_env):
    st = run(server.vehicle_status, "t24")
    assert st["odometer"]["miles"] == 17591.9            # 28311.4 km
    assert st["fuel"]["percent"] == 62 and st["fuel"]["range_miles"] == 249.8 and st["fuel"]["gallons_remaining"] == 16.5
    assert st["lock"]["is_locked"] is True and st["lock"]["doors_open"] == []
    assert st["tires"]["pressures"] == {"front_left": 55.0, "front_right": 55.0, "rear_left": 57.0, "rear_right": 57.0}
    assert st["oil"]["life_percent"] == 48
    assert st["geofence"]["inside"] is True and st["geofence"]["distance_km"] < 0.1
    assert st["trip_status"] == "unknown" and st["flags"] == []
    assert st["odometer"]["as_of"].endswith("Z")
    assert "warning" not in st


def test_audi_signal_error_is_reported_not_fatal(fleet_env):
    st = run(server.vehicle_status, "audi")
    assert st["tires"] is None
    assert st["signals"]["wheel-tires"] == {"status": "ERROR", "as_of": st["signals"]["wheel-tires"]["as_of"],
                                            "retrieved_at": st["signals"]["wheel-tires"]["retrieved_at"],
                                            "error": "COMPATIBILITY:VEHICLE_NOT_CAPABLE"}
    assert "tire_pressure" in st["unsupported_per_smartcar_matrix"]


def test_unlinked_vehicle_error(fleet_env):
    with pytest.raises(ToolError, match="fleet-auth connect mercedes"):
        run(server.vehicle_status, "sprinter")


def test_snapshot_flags_unknown_trips(fleet_env):
    snap = run(server.fleet_snapshot)
    f = {k: sorted(v) for k, v in snap["flag_summary"].items()}
    assert f["low_fuel"] == ["t25"]
    assert f["unlocked_not_on_trip"] == ["t25"]
    assert f["outside_geofence"] == ["audi"]
    assert f["no_data"] == ["sprinter"]
    assert f["oil_service"] == ["sub"] and f["stale_data"] == ["sub"] and f["lock_state_unavailable"] == ["sub"]
    t25 = next(r for r in snap["vehicles"] if r["vehicle"]["key"] == "t25")
    sev = {x["flag"]: x["severity"] for x in t25["flags"]}
    assert sev["unlocked_not_on_trip"] == "info"   # trip status unknown -> info only
    assert sev["door_open"] == "warn"


def test_snapshot_with_trips_and_redaction(fleet_env):
    snap = run(server.fleet_snapshot, ["EIUY09", "Heaps"])  # audi + t25 rented
    rows = {r["vehicle"]["key"]: r for r in snap["vehicles"]}
    assert snap["trip_info"] == ["audi", "t25"]
    assert rows["audi"]["trip_status"] == "on_trip"
    assert rows["audi"]["location"]["redacted"] is True and "lat" not in rows["audi"]["location"]
    assert rows["audi"]["geofence"] == {"name": "ParkDIA", "inside": False, "distance_km": None}
    assert not any(x["flag"] in ("outside_geofence", "unlocked_not_on_trip") for x in rows["audi"]["flags"] + rows["t25"]["flags"])
    assert [x["flag"] for x in rows["t25"]["flags"]] == ["low_fuel"]
    assert rows["t24"]["trip_status"] == "available"


def test_trips_file(fleet_env, tmp_path, monkeypatch):
    f = tmp_path / "trips.json"
    f.write_text(json.dumps([{"vehicle": "t24", "start": "2020-01-01T00:00:00-07:00", "end": "2099-01-01T00:00:00-07:00"},
                             {"vehicle": "audi", "start": "2020-01-01T00:00:00-07:00", "end": "2020-01-02T00:00:00-07:00"}]))
    monkeypatch.setenv("FLEET_TRIPS_FILE", str(f))
    snap = run(server.fleet_snapshot)
    assert snap["trip_info"] == ["t24"]
    audi = next(r for r in snap["vehicles"] if r["vehicle"]["key"] == "audi")
    assert {x["flag"]: x["severity"] for x in audi["flags"]}["outside_geofence"] == "warn"


def test_reveal_location_is_audited(fleet_env):
    st = run(server.vehicle_status, "audi", None, ["audi"])
    assert st["location"]["redacted"] is True
    st = run(server.vehicle_status, "audi", "renter 26h overdue, BF-2026-10-20-audi", ["audi"])
    assert st["location"]["lat"] == 39.7392
    last = audit.read(1)[0]
    assert last["event"] == "location_reveal" and last["vehicle"] == "audi" and "overdue" in last["reason"]


def test_odometer_history_records_reads(fleet_env):
    run(server.vehicle_status, "t25")
    run(server.vehicle_status, "t25")  # identical reading -> not duplicated
    h = server.vehicle_odometer_history("Heaps")
    assert h["count"] == 1 and h["readings"][0]["miles"] == 9120


def test_config_status_has_no_secrets(fleet_env):
    run(server.vehicle_status, "t24")
    out = json.dumps(server.fleet_config_status())
    assert SECRET not in out and "test-client-id" not in out
    assert '"write_enabled": false' in out
