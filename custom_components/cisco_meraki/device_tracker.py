"""Device trackers for the clients of Meraki networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CLIENT_VLANS, DOMAIN, LOGGER
from .coordinator import MerakiClient, MerakiDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up trackers for the clients of the organization's networks."""
    coordinator = entry.runtime_data
    org_id = coordinator.organization_id
    vlans = frozenset(entry.options.get(CONF_CLIENT_VLANS) or ())
    entity_registry = er.async_get(hass)

    # The API forgets clients after an hour; their trackers stay, as away.
    registry_entries = {
        registry_entry.unique_id: registry_entry
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if registry_entry.domain == Platform.DEVICE_TRACKER
    }

    tracked = set(registry_entries)

    @callback
    def _async_remove_out_of_scope() -> None:
        """Remove the trackers of listed clients outside the selected VLANs."""
        if not vlans:
            return
        for mac, client in coordinator.data.clients.items():
            unique_id = _unique_id(org_id, mac)
            if _in_scope(client, vlans) or unique_id not in tracked:
                continue
            # A client that later joins a selected VLAN gets a new tracker.
            tracked.discard(unique_id)
            registry_entries.pop(unique_id, None)
            if not (
                entity_id := entity_registry.async_get_entity_id(
                    Platform.DEVICE_TRACKER, DOMAIN, unique_id
                )
            ):
                continue
            device_id = entity_registry.entities[entity_id].device_id
            LOGGER.debug(
                "Removing the tracker %s of a client in VLAN %s", entity_id, client.vlan
            )
            entity_registry.async_remove(entity_id)
            _async_remove_device(hass, entry.entry_id, device_id)

    _async_remove_out_of_scope()
    async_add_entities(
        MerakiClientTracker(
            coordinator,
            unique_id.removeprefix(f"{org_id}_"),
            registry_entry.original_name,
        )
        for unique_id, registry_entry in registry_entries.items()
    )

    @callback
    def _async_update_clients() -> None:
        _async_remove_out_of_scope()
        new: list[MerakiClientTracker] = []
        for mac, client in coordinator.data.clients.items():
            if not _in_scope(client, vlans):
                continue
            if (unique_id := _unique_id(org_id, mac)) not in tracked:
                tracked.add(unique_id)
                new.append(MerakiClientTracker(coordinator, mac, client.description))
        async_add_entities(new)

    _async_update_clients()
    entry.async_on_unload(coordinator.async_add_listener(_async_update_clients))


def _in_scope(client: MerakiClient, vlans: frozenset[str]) -> bool:
    """Return True if the client is in one of the VLANs, or no VLAN is selected."""
    return not vlans or client.vlan in vlans


@callback
def _async_remove_device(
    hass: HomeAssistant, entry_id: str, device_id: str | None
) -> None:
    """Remove a tracker's device once the entry has no other entity on it."""
    if device_id is None:
        return
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    # The tracker's own device belongs to this entry; a device of another
    # integration with the same MAC address is left alone.
    if device is None or device.config_entry_id != entry_id:
        return
    if er.async_entries_for_device(
        er.async_get(hass), device_id, include_disabled_entities=True
    ):
        return
    device_registry.async_remove_device(device_id)


def _unique_id(org_id: str, mac: str) -> str:
    # The same device can be a client in several organizations.
    return f"{org_id}_{mac}"


class MerakiClientTracker(
    CoordinatorEntity[MerakiDataUpdateCoordinator], ScannerEntity
):
    """A Wi-Fi or wired client, home while it is connected to the network."""

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, mac: str, name: str | None
    ) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator)
        self.mac = mac
        self._attr_mac_address = mac
        self._attr_name = name or mac
        self._async_update_attrs()

    @property
    @override
    def unique_id(self) -> str:
        """Return the MAC, scoped to the organization."""
        return _unique_id(self.coordinator.organization_id, self.mac)

    @property
    @override
    def is_connected(self) -> bool:
        """Return True if Meraki reports the client as online."""
        client = self.coordinator.data.clients.get(self.mac)
        return client is not None and client.online

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        super()._handle_coordinator_update()

    @callback
    def _async_update_attrs(self) -> None:
        if client := self.coordinator.data.clients.get(self.mac):
            self._attr_hostname = client.description
            self._attr_ip_address = client.ip
            self._attr_extra_state_attributes = {"vlan": client.vlan}
