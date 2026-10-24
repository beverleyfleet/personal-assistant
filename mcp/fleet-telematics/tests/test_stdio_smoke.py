"""Start the server as a subprocess and speak MCP over stdio (replay fixtures, fake `op`)."""
import json
import os
import sys

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from conftest import SECRET

EXPECTED = {"fleet_list", "vehicle_status", "fleet_snapshot", "vehicle_odometer_history",
            "fleet_config_status", "vehicle_lock", "vehicle_unlock"}


def params(**extra):
    env = {k: v for k, v in os.environ.items()}
    env.update(extra)
    return StdioServerParameters(command=sys.executable, args=["-m", "fleet_telematics"], env=env)


def text(r):
    return " ".join(getattr(c, "text", "") for c in r.content)


def test_stdio_read_tools_and_write_gate(fleet_env):
    async def run():
        async with Client(params(FLEET_ALLOW_WRITE="")) as client:
            tools = await client.list_tools()
            by = {t.name: t for t in tools.tools}
            assert EXPECTED <= set(by), set(by)
            assert by["vehicle_status"].annotations.read_only_hint is True
            assert by["vehicle_unlock"].annotations.destructive_hint is True

            r = await client.call_tool("fleet_snapshot", {"vehicles_on_trip": ["Joker"]})
            assert not r.is_error, text(r)
            sc = r.structured_content
            assert sorted(sc["flag_summary"]["low_fuel"]) == ["t25"]
            assert sc["flag_summary"]["outside_geofence"] == ["audi"]
            joker = next(v for v in sc["vehicles"] if v["vehicle"]["key"] == "t24")
            assert joker["trip_status"] == "on_trip" and joker["location"]["redacted"] is True

            r = await client.call_tool("vehicle_status", {"vehicle": "Heaps"})
            assert r.structured_content["fuel"]["percent"] == 18

            r = await client.call_tool("vehicle_status", {"vehicle": "tacoma"})
            assert r.is_error and "unknown vehicle" in text(r)

            r = await client.call_tool("vehicle_unlock", {"vehicle": "t24", "confirm": True, "reason": "renter locked out"})
            assert r.is_error and "FLEET_ALLOW_WRITE" in text(r)

            r = await client.call_tool("fleet_config_status", {})
            blob = json.dumps(r.structured_content)
            assert SECRET not in blob and r.structured_content["audit_chain"]["ok"]
            return blob

    anyio.run(run)


def test_stdio_unlock_allowed_with_env_confirm_reason(fleet_env):
    async def run():
        async with Client(params(FLEET_ALLOW_WRITE="1")) as client:
            r = await client.call_tool("vehicle_unlock", {"vehicle": "t25", "confirm": True})
            assert r.is_error and "reason" in text(r)
            r = await client.call_tool("vehicle_unlock", {"vehicle": "t25", "confirm": True,
                                                          "reason": "renter at ParkDIA bay 4, keys inside"})
            assert not r.is_error, text(r)
            assert r.structured_content["result"]["status"] == "SUCCESS"
            r = await client.call_tool("vehicle_odometer_history", {"vehicle": "t25"})
            return r.structured_content

    anyio.run(run)
    log = (fleet_env["state"] / "audit.jsonl").read_text().splitlines()
    assert [json.loads(x)["event"] for x in log] == ["unlock_refused", "unlock_requested", "unlock_done"]
    assert SECRET not in "".join(log)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("FLEET_LIVE") != "1", reason="set FLEET_LIVE=1 (needs op signed in + linked vehicles)")
def test_live_snapshot_read_only():
    async def run():
        env = {k: v for k, v in os.environ.items() if k not in ("FLEET_SMARTCAR_REPLAY_DIR", "FLEET_ALLOW_WRITE")}
        async with Client(StdioServerParameters(command=sys.executable, args=["-m", "fleet_telematics"], env=env),
                          read_timeout_seconds=120) as client:
            r = await client.call_tool("fleet_snapshot", {})
            assert not r.is_error, text(r)
            print(json.dumps(r.structured_content["flag_summary"], indent=1))

    anyio.run(run)
