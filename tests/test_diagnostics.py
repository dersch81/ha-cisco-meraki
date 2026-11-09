"""Tests for the diagnostics."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.cisco_meraki.const import CONF_CLIENT_VLANS

from .conftest import API_KEY


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Diagnostics list the clients by VLAN and hide the API key."""
    mock_client.networks.getNetworkClients.return_value = [
        {"mac": "aa:bb:cc:10:00:02", "status": "Offline", "vlan": "20"},
        {"mac": "aa:bb:cc:10:00:01", "status": "Online", "vlan": "1"},
    ]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CLIENT_VLANS: ["1"]}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )
    assert API_KEY not in str(diagnostics)
    assert diagnostics["entry"]["options"] == {CONF_CLIENT_VLANS: ["1"]}
    assert [
        (client["mac"], client["vlan"], client["tracked"])
        for client in diagnostics["clients"]
    ] == [("aa:bb:cc:10:00:01", "1", True), ("aa:bb:cc:10:00:02", "20", False)]
