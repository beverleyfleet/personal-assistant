"""Smartcar US compatibility snapshot for the fleet's models.

Source: Smartcar Compatibility API (public, no auth)
  GET https://compatibility.api.smartcar.com/v3/compatible-vehicles?filter[region]=US&filter[make]=<MAKE>
Bundled snapshot: data/compat_us.json (retrieved 2026-10-24, trimmed to our models).
Smartcar says the data is derived from real connections and refreshed daily, and that trims can
differ, so "not listed" means "not seen working yet", not "impossible".
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from fleet_telematics.config import Vehicle, state_dir

BUNDLED = Path(__file__).parent / "data" / "compat_us.json"
API = "https://compatibility.api.smartcar.com/v3/compatible-vehicles"
MAKES = {"Ford": "FORD", "Audi": "AUDI", "Mercedes-Benz": "MERCEDES-BENZ", "Chevrolet": "CHEVROLET"}

# What each status field needs (signal capability codes, v3) and the Connect permission.
FEATURES: dict[str, tuple[str, str]] = {
    "odometer": ("odometer-traveleddistance", "read_odometer"),
    "fuel_percent": ("internalcombustionengine-fuellevel", "read_fuel"),
    "fuel_range": ("internalcombustionengine-range", "read_fuel"),
    "location": ("location-preciselocation", "read_location"),
    "lock_state": ("closure-islocked", "read_security"),
    "doors": ("closure-doors", "read_security"),
    "tire_pressure": ("wheel-tires", "read_tires"),
    "oil_life": ("internalcombustionengine-oillife", "read_engine_oil"),
    "oil_life_alert": ("diagnostics-oillife", "read_diagnostics"),
    "tire_pressure_alert": ("diagnostics-tirepressure", "read_diagnostics"),
    "vin": ("vehicleidentification-vin", "read_vin"),
    "lock_command": ("closure-lock", "control_security"),
    "unlock_command": ("closure-unlock", "control_security"),
}


def _snapshot_path() -> Path:
    refreshed = state_dir() / "compat_us.json"
    override = os.environ.get("FLEET_COMPAT_FILE")
    if override:
        return Path(override)
    return refreshed if refreshed.exists() else BUNDLED


@lru_cache(maxsize=1)
def _load() -> dict:
    return json.loads(_snapshot_path().read_text())


def entry_for(v: Vehicle) -> dict | None:
    for row in _load()["data"]:
        y = row["years"]
        if (row["make"].lower() == v.make.lower() and row["model"].lower() == v.model.lower()
                and row["powertrainType"] == v.powertrain and y["start"] <= v.year <= y["end"]):
            return row
    return None


def capabilities(v: Vehicle) -> dict:
    row = entry_for(v)
    if row is None:
        return {"matrix_row": None, "note": "model/year/powertrain not in Smartcar's US matrix snapshot",
                "features": {k: None for k in FEATURES}}
    caps = {c["capability"] for c in row["capabilities"]}
    return {
        "matrix_row": row["id"],
        "snapshot": _load().get("retrieved"),
        "features": {k: (code in caps) for k, (code, _) in FEATURES.items()},
    }


def refresh(timeout: float = 60) -> Path:
    """Re-download the matrix for our makes into the state dir (used by `fleet-auth compat --refresh`)."""
    import httpx2

    out = {"source": API + "?filter[region]=US&filter[make]=<MAKE>", "retrieved": None, "data": []}
    from datetime import date

    with httpx2.Client(timeout=timeout) as c:
        for make in MAKES.values():
            r = c.get(API, params={"filter[region]": "US", "filter[make]": make})
            r.raise_for_status()
            for it in r.json()["data"]:
                a = it["attributes"]
                out["data"].append({"id": it["id"], **{k: a[k] for k in ("make", "model", "region", "powertrainType", "years")},
                                    "capabilities": [{k: c_[k] for k in ("type", "capability", "permission")}
                                                     for c_ in a["capabilities"]]})
    out["retrieved"] = date.today().isoformat()
    dest = state_dir() / "compat_us.json"
    dest.write_text(json.dumps(out, indent=1))
    _load.cache_clear()
    return dest
