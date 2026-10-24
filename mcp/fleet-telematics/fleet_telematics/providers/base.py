"""Provider abstraction. A provider turns a configured Vehicle into a normalized status dict
and (optionally) executes lock/unlock. The MCP layer never talks HTTP directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from fleet_telematics.config import Vehicle


class ProviderError(RuntimeError):
    """User-facing provider failure (message is safe to show; never contains secrets)."""


class NotLinked(ProviderError):
    """Vehicle has no provider id yet (owner has not connected the account)."""


class ProviderNotImplemented(ProviderError, NotImplementedError):
    """Documented stub provider."""


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    async def status(self, v: Vehicle) -> dict[str, Any]:
        """Normalized status. Keys (each may be None when unsupported/unavailable):
        odometer {miles, km, as_of}, fuel {percent, range_miles, as_of}, location {lat, lon, heading,
        location_type, as_of}, lock {is_locked, doors_open, as_of}, tires {unit:"psi", pressures:{FL,FR,RL,RR}, as_of},
        oil {life_percent, alert, as_of}, battery_12v {percent, status, as_of}, motion {gear, speed_mph, is_online},
        signals {code: status}, retrieved_at"""

    async def lock(self, v: Vehicle) -> dict[str, Any]:
        raise ProviderNotImplemented(f"{self.name}: lock is not supported")

    async def unlock(self, v: Vehicle) -> dict[str, Any]:
        raise ProviderNotImplemented(f"{self.name}: unlock is not supported")

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}
