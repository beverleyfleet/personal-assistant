"""Append-only, hash-chained local audit log (JSON Lines, file mode 0600).

Every write attempt (lock/unlock, allowed or refused) and every deliberate reveal of an
on-trip location is recorded. Each entry carries `prev` = sha256 of the previous line, so a
deleted or edited line breaks the chain; `verify()` checks it. The code only ever opens the
file with O_APPEND; it never truncates or rewrites it. (Root or the file owner can still
delete it: this is tamper-evidence, not tamper-proofing. Back it up if it matters.)
"""

from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

from fleet_telematics.config import state_dir

GENESIS = "0" * 64


def log_path() -> Path:
    return Path(os.environ.get("FLEET_AUDIT_LOG") or state_dir() / "audit.jsonl").expanduser()


def _last_hash(f) -> str:
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size == 0:
        return GENESIS
    back = min(size, 65536)
    f.seek(size - back)
    tail = f.read(back).rstrip(b"\n").rsplit(b"\n", 1)[-1]
    return hashlib.sha256(tail).hexdigest()


def append(event: str, **fields) -> dict:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            entry = {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "event": event,
                "os_user": getpass.getuser(),
                "host": socket.gethostname(),
                **fields,
                "prev": _last_hash(f),
            }
            line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            f.write(line)  # O_APPEND: always lands at end of file
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return entry


def read(limit: int = 50) -> list[dict]:
    p = log_path()
    if not p.exists():
        return []
    lines = p.read_bytes().splitlines()
    return [json.loads(x) for x in lines[-limit:] if x.strip()]


def verify() -> dict:
    p = log_path()
    if not p.exists():
        return {"ok": True, "entries": 0}
    prev = GENESIS
    lines = [x for x in p.read_bytes().splitlines() if x.strip()]
    for i, line in enumerate(lines):
        if json.loads(line).get("prev") != prev:
            return {"ok": False, "entries": len(lines), "broken_at_line": i + 1}
        prev = hashlib.sha256(line).hexdigest()
    return {"ok": True, "entries": len(lines)}
