"""Fleet config (fleet.toml) and environment settings. Nothing here is secret."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "fleet.toml"


def config_path() -> Path:
    return Path(os.environ.get("FLEET_CONFIG") or DEFAULT_CONFIG).expanduser()


def state_dir() -> Path:
    """Local state (audit log, odometer history, fallback user-id file). Created 0700."""
    base = os.environ.get("FLEET_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "beverley-fleet", "fleet-telematics"
    )
    p = Path(base).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Vehicle:
    key: str
    name: str
    year: int
    make: str
    model: str
    provider: str = "smartcar"
    powertrain: str = "ICE"
    vin: str = ""
    plate: str = ""
    nickname: str = ""
    smartcar_account: str = ""
    smartcar_vehicle_id: str = ""

    def public(self) -> dict:
        return {
            "key": self.key, "name": self.name, "nickname": self.nickname or None, "year": self.year,
            "make": self.make, "model": self.model, "powertrain": self.powertrain, "vin": self.vin or None,
            "plate": self.plate or None, "provider": self.provider,
            "smartcar_account": self.smartcar_account or None,
            "linked": bool(self.smartcar_vehicle_id) if self.provider == "smartcar" else None,
        }


@dataclass
class Geofence:
    name: str
    lat: float
    lon: float
    radius_m: float
    address: str = ""


@dataclass
class FleetConfig:
    vehicles: list[Vehicle]
    geofence: Geofence
    low_fuel_percent: float = 25.0
    path: Path = field(default_factory=config_path)

    def find(self, query: str) -> Vehicle:
        """Match key, name, nickname, plate, full VIN or last 6+ chars of VIN (case-insensitive)."""
        q = _norm(query)
        if not q:
            raise LookupError("empty vehicle name")
        for v in self.vehicles:
            if q in {_norm(v.key), _norm(v.name), _norm(v.nickname), _norm(v.plate), _norm(v.vin)} - {""}:
                return v
        hits = [v for v in self.vehicles if (len(q) >= 6 and v.vin and _norm(v.vin).endswith(q))
                or (q in _norm(v.name) and len(q) >= 3)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise LookupError(f"'{query}' is ambiguous: {', '.join(v.key for v in hits)}")
        raise LookupError(f"unknown vehicle '{query}'. Known: {', '.join(v.key for v in self.vehicles)}")


def _norm(s: str | None) -> str:
    return re.sub(r"[\s\-_]", "", (s or "")).lower()


def load_config(path: Path | None = None) -> FleetConfig:
    p = path or config_path()
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    fields = set(Vehicle.__dataclass_fields__)
    vehicles = [Vehicle(**{k: v for k, v in item.items() if k in fields}) for item in raw.get("vehicle", [])]
    keys = [v.key for v in vehicles]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate vehicle keys in {p}")
    g = raw.get("geofence", {})
    geofence = Geofence(name=g.get("name", "ParkDIA"), lat=float(g["lat"]), lon=float(g["lon"]),
                        radius_m=float(g.get("radius_m", 600)), address=g.get("address", ""))
    low = float(raw.get("thresholds", {}).get("low_fuel_percent", 25))
    return FleetConfig(vehicles=vehicles, geofence=geofence, low_fuel_percent=low, path=p)


def update_vehicle_fields(path: Path, key: str, updates: dict[str, str]) -> None:
    """Set string fields of one [[vehicle]] block in place, preserving comments and layout.

    Only replaces existing `field = "..."` lines inside the block whose key matches; a field
    that does not exist in the block is appended at the end of that block.
    """
    lines = path.read_text().splitlines(keepends=True)
    headers = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")] + [len(lines)]
    start = end = None
    for h, nxt in zip(headers, headers[1:]):
        if lines[h].strip() == "[[vehicle]]" and any(
            re.match(rf'^\s*key\s*=\s*"{re.escape(key)}"', lines[i]) for i in range(h + 1, nxt)
        ):
            start, end = h, nxt
            break
    if start is None:
        raise LookupError(f"vehicle '{key}' not found in {path}")
    for fname, value in updates.items():
        safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
        pat = re.compile(rf'^(\s*{re.escape(fname)}\s*=\s*)"[^"]*"(.*)$', re.S)
        for i in range(start, end):
            m = pat.match(lines[i])
            if m:
                lines[i] = f'{m.group(1)}"{safe}"{m.group(2)}'
                break
        else:
            insert_at = end
            while insert_at > start and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, f'{fname} = "{safe}"\n')
            end += 1
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines))
    os.replace(tmp, path)
