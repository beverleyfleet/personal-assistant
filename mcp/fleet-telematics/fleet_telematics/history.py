"""Local odometer history.

Smartcar v3 has no odometer-history endpoint (only the latest `odometer-traveleddistance`
signal; history arrives as webhooks, which need a public HTTPS receiver this local server
does not run). So every successful odometer read is appended here, and
`vehicle_odometer_history` reports what this machine has seen. JSONL, mode 0600.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fleet_telematics.config import state_dir


def path() -> Path:
    return state_dir() / "odometer.jsonl"


def record(vehicle_key: str, miles: float | None, as_of: str | None) -> None:
    if miles is None:
        return
    last = latest(vehicle_key)
    if last and last.get("as_of") == as_of and last.get("miles") == miles:
        return  # same reading as last time
    entry = {"vehicle": vehicle_key, "miles": miles, "as_of": as_of,
             "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")}
    fd = os.open(path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry) + "\n")


def readings(vehicle_key: str, since: str | None = None) -> list[dict]:
    p = path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("vehicle") == vehicle_key and (not since or (e.get("as_of") or e["recorded_at"]) >= since):
            out.append(e)
    return out


def latest(vehicle_key: str) -> dict | None:
    r = readings(vehicle_key)
    return r[-1] if r else None
