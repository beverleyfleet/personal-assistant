"""Lock/unlock gating and the append-only, hash-chained audit log."""
import os
import stat

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fleet_telematics import audit, server


def run(fn, *a, **kw):
    return anyio.run(lambda: fn(*a, **kw))


def events():
    return [e["event"] for e in audit.read(100)]


def test_lock_refused_without_env(fleet_env):
    with pytest.raises(server.WriteBlocked, match="FLEET_ALLOW_WRITE"):
        run(server.vehicle_lock, "t24", confirm=True)
    assert events() == ["lock_refused"]


def test_refused_without_confirm(fleet_env, monkeypatch):
    monkeypatch.setenv("FLEET_ALLOW_WRITE", "1")
    with pytest.raises(server.WriteBlocked, match="confirm=True"):
        run(server.vehicle_lock, "t24")


@pytest.mark.parametrize("reason", ["", "   ", "ok"])
def test_unlock_requires_reason(fleet_env, monkeypatch, reason):
    monkeypatch.setenv("FLEET_ALLOW_WRITE", "1")
    with pytest.raises(server.WriteBlocked, match="reason"):
        run(server.vehicle_unlock, "t24", confirm=True, reason=reason)
    assert audit.read(1)[0]["event"] == "unlock_refused"


def test_unlock_and_lock_succeed_and_are_logged(fleet_env, monkeypatch):
    monkeypatch.setenv("FLEET_ALLOW_WRITE", "1")
    r = run(server.vehicle_unlock, "Joker", confirm=True, reason="renter at ParkDIA, keys locked inside")
    assert r["result"]["status"] == "SUCCESS" and r["result"]["command"] == "security-unlock"
    r = run(server.vehicle_lock, "t24", confirm=True)
    assert r["result"]["command"] == "security-lock"
    assert events() == ["unlock_requested", "unlock_done", "lock_requested", "lock_done"]
    done = audit.read(4)[1]
    assert done["vin"] == "1FBAX9Y87RKA27765" and done["reason"].startswith("renter at ParkDIA")
    assert audit.verify() == {"ok": True, "entries": 4}
    assert stat.S_IMODE(os.stat(audit.log_path()).st_mode) == 0o600


def test_unlinked_vehicle_failure_logged(fleet_env, monkeypatch):
    monkeypatch.setenv("FLEET_ALLOW_WRITE", "1")
    with pytest.raises(ToolError, match="not linked"):
        run(server.vehicle_unlock, "sprinter", confirm=True, reason="testing the gate path")
    assert events()[-1] == "unlock_failed"


def test_audit_chain_detects_tampering(fleet_env):
    for i in range(3):
        audit.append("test", n=i)
    assert audit.verify()["ok"]
    p = audit.log_path()
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(lines[0] + lines[2])  # delete the middle entry
    assert audit.verify() == {"ok": False, "entries": 2, "broken_at_line": 2}
