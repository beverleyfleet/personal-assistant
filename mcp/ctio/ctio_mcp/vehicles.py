"""Plate -> vehicle lookup for the Beverley Fleet (config in fleet.toml)."""

from __future__ import annotations

import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path

DEFAULT_FLEET_FILE = Path(__file__).with_name("fleet.toml")


def normalize_plate(plate: str) -> str:
    """Uppercase, drop spaces/hyphens and a leading TEMP marker.

    "Temp 8620436" -> "8620436", "eiuy-09" -> "EIUY09".
    """
    p = re.sub(r"[\s\-]", "", (plate or "").upper())
    if p.startswith("TEMP") and len(p) > 4:
        p = p[4:]
    return p


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict, ...]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return tuple(data.get("vehicle", []))


def fleet() -> list[dict]:
    path = os.environ.get("CTIO_FLEET_FILE") or str(DEFAULT_FLEET_FILE)
    return [dict(v) for v in _load(path)]


def vehicle_for_plate(plate: str) -> dict | None:
    key = normalize_plate(plate)
    for v in fleet():
        if normalize_plate(v["plate"]) == key:
            label = f'{v.get("year", "")} {v.get("make", "")} {v.get("model", "")}'.strip()
            if v.get("nickname"):
                label += f' "{v["nickname"]}"'
            return {**v, "normalized_plate": key, "label": label}
    return None
