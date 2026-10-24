"""Documented, NOT implemented providers. They exist so a vehicle can be pointed at them in
fleet.toml and get a clear explanation instead of a crash. See README "Other providers"."""

from __future__ import annotations

from fleet_telematics.config import Vehicle
from fleet_telematics.providers.base import Provider, ProviderNotImplemented


class FordProTelematicsProvider(Provider):
    """Ford Pro Telematics (Ford's commercial-fleet product; developer portal developer.ford.com).

    Status: stub. Access to Ford's fleet data APIs is partner/approval-gated and Ford Pro
    Telematics itself is a paid per-vehicle subscription (a third-party source quotes ~$20/vehicle/month
    for the paid tier and a free "Telematics Essentials" tier; not verified against Ford). Ford's portal
    is a JavaScript app that could not be read for this build, so endpoints/auth are NOT documented here.

    Worth it when: the Transit count grows (Ford Pro adds driver behaviour, fuel/idle reports,
    maintenance scheduling, geofences and remote lock inside Ford's own fleet UI), or when Smartcar's
    Ford coverage degrades. For 2-4 Transits, Smartcar's per-vehicle price is far lower.

    To implement: get Ford Pro partner API access, then implement status()/lock()/unlock() mapping
    their payload to the normalized dict documented in providers/base.py.
    """

    name = "ford_pro"

    async def status(self, v: Vehicle):
        raise ProviderNotImplemented(
            f"{v.key}: provider 'ford_pro' (Ford Pro Telematics) is a documented stub, not implemented. "
            "It needs Ford Pro partner API access (developer.ford.com, approval-gated, paid). "
            "Set provider = \"smartcar\" for this vehicle in fleet.toml to use Smartcar instead."
        )


class MercedesProConnectProvider(Provider):
    """Mercedes PRO connect (Mercedes-Benz Vans fleet services; Sprinter/Metris in the US).

    Status: stub. Mercedes PRO connect offers fleet managers location, fuel level and maintenance
    intervals via its web portal using the van's built-in LTE module. No public US developer API was
    found for this build (developer.mercedes-benz.com is a JavaScript app that could not be read);
    a "fleet data interface" for third parties appears to require a Mercedes PRO contract. Unverified.

    Worth it when: the fleet has several Sprinters and you want Mercedes-native fleet tools
    (digital logbook, maintenance management, driver assignment), or if Smartcar loses Mercedes access.
    For one Sprinter, Smartcar (via the owner's Mercedes me login) is simpler.
    """

    name = "mercedes_pro"

    async def status(self, v: Vehicle):
        raise ProviderNotImplemented(
            f"{v.key}: provider 'mercedes_pro' (Mercedes PRO connect) is a documented stub, not implemented. "
            "It needs a Mercedes PRO connect contract/data-interface access. "
            "Set provider = \"smartcar\" for this vehicle in fleet.toml to use Smartcar (Mercedes me login) instead."
        )
