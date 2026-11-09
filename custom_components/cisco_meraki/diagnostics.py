"""Diagnostics for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY

from .const import CONF_CLIENT_VLANS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import MerakiConfigEntry

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MerakiConfigEntry,
) -> dict[str, Any]:
    """Return the entry's options and the clients with the VLAN they are in."""
    coordinator = entry.runtime_data
    vlans = set(entry.options.get(CONF_CLIENT_VLANS) or ())
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "devices": len(coordinator.data.devices),
        # Shows which VLANs to select, and which clients a selection tracks.
        "clients": [
            {
                "mac": client.mac,
                "description": client.description,
                "network_id": client.network_id,
                "vlan": client.vlan,
                "online": client.online,
                "tracked": not vlans or client.vlan in vlans,
            }
            for client in sorted(coordinator.data.clients.values(), key=lambda c: c.mac)
        ],
    }
