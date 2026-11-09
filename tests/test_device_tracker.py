"""Tests for the client device trackers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cisco_meraki.const import (
    CONF_CLIENT_VLANS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import DEVICES, ORG_ID, make_api_error

PHONE = "device_tracker.pixel_9"
PHONE_MAC = "aa:bb:cc:10:00:01"

# As getNetworkClients returns them, trimmed to the fields the trackers use.
CLIENTS = [
    {
        "mac": PHONE_MAC,
        "description": "Pixel 9",
        "ip": "192.168.1.20",
        "lastSeen": "2026-10-20T20:59:00Z",
        "recentDeviceSerial": "Q2AA-AAAA-AAAA",
        "status": "Online",
        "vlan": "1",
    },
    {
        "mac": "aa:bb:cc:10:00:02",
        "description": "NAS",
        "ip": "192.168.1.30",
        "lastSeen": "2026-10-20T20:59:30Z",
        "recentDeviceSerial": "Q2SW-SSSS-SSSS",
        "switchport": "4",
        "status": "Online",
        # The API documents a string, but a number is handled the same way.
        "vlan": 11,
    },
    {
        "mac": "aa:bb:cc:10:00:03",
        "description": None,
        "ip": "192.168.1.40",
        "lastSeen": "2026-10-20T20:15:00Z",
        "recentDeviceSerial": "Q2AA-AAAA-AAAA",
        "status": "Offline",
        "vlan": "20",
    },
]


@pytest.fixture
def clients(mock_client: MagicMock) -> list[dict[str, Any]]:
    """Return the clients the API lists; change them before the next poll."""
    clients = [dict(client) for client in CLIENTS]
    mock_client.networks.getNetworkClients.return_value = clients
    return clients


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients", "freezer")
async def test_trackers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Clients are home while Meraki reports them online."""
    await _setup(hass, mock_config_entry)

    phone = hass.states.get(PHONE)
    assert phone.state == STATE_HOME
    assert phone.attributes["ip"] == "192.168.1.20"
    assert phone.attributes["mac"] == PHONE_MAC
    assert phone.attributes["host_name"] == "Pixel 9"
    assert phone.attributes["vlan"] == "1"
    assert hass.states.get("device_tracker.nas").attributes["vlan"] == "11"
    assert hass.states.get("device_tracker.nas").state == STATE_HOME
    # Clients without a name are named by their MAC address.
    assert hass.states.get("device_tracker.aa_bb_cc_10_00_03").state == STATE_NOT_HOME
    assert entity_registry.async_get(PHONE).unique_id == f"{ORG_ID}_{PHONE_MAC}"


@pytest.mark.usefixtures("clients", "freezer")
async def test_trackers_disabled_by_default(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Only clients known to another integration are tracked by default."""
    other_entry = MockConfigEntry(domain="other")
    other_entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, PHONE_MAC)},
    )
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PHONE).state == STATE_HOME
    nas = entity_registry.async_get("device_tracker.nas")
    assert nas.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_client_leaves(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Clients are away when offline or no longer listed."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(PHONE).state == STATE_HOME

    clients[0]["status"] = "Offline"
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_NOT_HOME

    # After an hour without connecting, the API stops listing a client.
    del clients[0]
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_NOT_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_new_client(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Clients that connect later get a tracker."""
    await _setup(hass, mock_config_entry)
    clients.append({**CLIENTS[0], "mac": "aa:bb:cc:10:00:04", "description": "Tablet"})
    await _poll(hass, freezer)
    assert hass.states.get("device_tracker.tablet").state == STATE_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_restore_unlisted_clients(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
) -> None:
    """Trackers of clients the API no longer lists are away after a reload."""
    await _setup(hass, mock_config_entry)
    clients.clear()
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(PHONE).state == STATE_NOT_HOME
    assert hass.states.get("device_tracker.nas").state == STATE_NOT_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_client_in_several_networks(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A client listed in two networks is home where it is online."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        *(dict(d) for d in DEVICES),
        {
            "serial": "Q2CC-CCCC-CCCC",
            "name": "Lab AP",
            "model": "MR28",
            "productType": "wireless",
            "networkId": "N_2",
        },
    ]
    # Meraki documents lastSeen as a Unix timestamp; the other clients have the
    # ISO strings the API actually returns. 1792528200 is 2026-10-20 20:30 UTC.
    elsewhere = [
        {**client, "status": "Offline", "ip": "10.0.0.1", "lastSeen": 1792528200}
        for client in CLIENTS
    ]
    mock_client.networks.getNetworkClients.side_effect = lambda network_id, **_: (
        [dict(c) for c in CLIENTS] if network_id == "N_1" else elsewhere
    )
    await _setup(hass, mock_config_entry)

    phone = hass.states.get(PHONE)
    assert phone.state == STATE_HOME
    assert phone.attributes["ip"] == "192.168.1.20"
    # Offline in both, the more recent sighting (20:30 rather than 20:15) wins.
    laptop = hass.states.get("device_tracker.aa_bb_cc_10_00_03")
    assert laptop.attributes["ip"] == "10.0.0.1"


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients")
async def test_update_failed(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Trackers become unavailable when the API fails."""
    await _setup(hass, mock_config_entry)
    mock_client.networks.getNetworkClients.side_effect = make_api_error(503)
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_UNAVAILABLE


NAS_MAC = "aa:bb:cc:10:00:02"
LAPTOP = "device_tracker.aa_bb_cc_10_00_03"


async def _set_vlans(
    hass: HomeAssistant, entry: MockConfigEntry, vlans: list[str]
) -> None:
    """Change the VLAN option; the entry reloads as after the options flow."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_CLIENT_VLANS: vlans}
    )
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients", "freezer")
async def test_vlan_filter(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Only clients in the selected VLANs get a tracker."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CLIENT_VLANS: ["1", "11"]}
    )
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PHONE).state == STATE_HOME
    assert hass.states.get("device_tracker.nas").state == STATE_HOME
    assert hass.states.get(LAPTOP) is None
    assert not entity_registry.async_get_entity_id(
        "device_tracker", DOMAIN, f"{ORG_ID}_aa:bb:cc:10:00:03"
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients", "freezer")
async def test_vlan_filter_removes_trackers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Selecting VLANs removes the trackers and devices of clients outside them."""
    other_entry = MockConfigEntry(domain="other")
    other_entry.add_to_hass(hass)
    other_device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, NAS_MAC)},
    )
    await _setup(hass, mock_config_entry)
    nas = entity_registry.async_get("device_tracker.nas")
    # The tracker of a device another integration knows gets its own device.
    assert nas.device_id is not None
    assert nas.device_id != other_device.id
    nas_device_id = nas.device_id

    await _set_vlans(hass, mock_config_entry, ["1"])

    assert hass.states.get(PHONE).state == STATE_HOME
    assert entity_registry.async_get("device_tracker.nas") is None
    assert hass.states.get("device_tracker.nas") is None
    assert entity_registry.async_get(LAPTOP) is None
    assert device_registry.async_get(nas_device_id) is None
    # The other integration's device stays.
    assert device_registry.async_get(other_device.id) is not None
    # The Meraki devices themselves are untouched.
    assert any(
        (DOMAIN, "Q2SW-SSSS-SSSS") in device.identifiers
        for device in dr.async_entries_for_config_entry(
            device_registry, mock_config_entry.entry_id
        )
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_vlan_filter_keeps_unlisted_trackers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
) -> None:
    """Trackers of clients the API no longer lists stay, as their VLAN is unknown."""
    await _setup(hass, mock_config_entry)
    del clients[2]
    await _set_vlans(hass, mock_config_entry, ["1"])

    assert hass.states.get(LAPTOP).state == STATE_NOT_HOME
    assert hass.states.get("device_tracker.nas") is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_client_changes_vlan(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    """A client leaving the selected VLANs loses its tracker until it returns."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CLIENT_VLANS: ["1"]}
    )
    await _setup(hass, mock_config_entry)
    assert hass.states.get(PHONE).state == STATE_HOME

    clients[0]["vlan"] = "20"
    await _poll(hass, freezer)
    assert entity_registry.async_get(PHONE) is None

    clients[0]["vlan"] = "1"
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients", "freezer")
async def test_no_vlan_selected_tracks_all(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
) -> None:
    """Without a VLAN selection, clients without a VLAN are tracked too."""
    del clients[0]["vlan"]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CLIENT_VLANS: []}
    )
    await _setup(hass, mock_config_entry)
    assert hass.states.get(PHONE).state == STATE_HOME
    assert hass.states.get(PHONE).attributes["vlan"] is None
    assert hass.states.get(LAPTOP) is not None
