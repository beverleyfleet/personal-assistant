"""Provider registry."""

from __future__ import annotations

from fleet_telematics.providers.base import NotLinked, Provider, ProviderError, ProviderNotImplemented
from fleet_telematics.providers.smartcar import SmartcarProvider
from fleet_telematics.providers.stubs import FordProTelematicsProvider, MercedesProConnectProvider

PROVIDERS: dict[str, type[Provider]] = {
    "smartcar": SmartcarProvider,
    "ford_pro": FordProTelematicsProvider,
    "mercedes_pro": MercedesProConnectProvider,
}

__all__ = ["PROVIDERS", "Provider", "ProviderError", "ProviderNotImplemented", "NotLinked", "SmartcarProvider"]
