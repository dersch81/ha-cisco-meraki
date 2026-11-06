"""Base entity for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MerakiDataUpdateCoordinator
from .sensor_coordinator import MerakiSensorCoordinator, reading_unique_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import (
        MerakiConfigEntry,
        MerakiDevice,
        MerakiSsid,
        MerakiSwitchPort,
    )
    from .readings import MerakiReading


class MerakiEntity(CoordinatorEntity[MerakiDataUpdateCoordinator]):
    """An entity belonging to a single Meraki device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"

        device = self.device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            connections={(CONNECTION_NETWORK_MAC, device.mac)} if device.mac else set(),
            manufacturer=MANUFACTURER,
            model=device.model,
            name=device.name,
            serial_number=serial,
            sw_version=device.firmware,
            configuration_url=device.url,
        )

    @property
    def device(self) -> MerakiDevice:
        """Return the latest data for this entity's device."""
        return self.coordinator.data.devices[self._serial]

    @property
    def available(self) -> bool:
        """Return False if the device disappeared from the organization."""
        return super().available and self._serial in self.coordinator.data.devices


class MerakiPortEntity(MerakiEntity):
    """An entity for one port of a Meraki switch."""

    def __init__(
        self,
        coordinator: MerakiDataUpdateCoordinator,
        serial: str,
        port_id: str,
        key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, serial, f"port_{port_id}_{key}")
        self._port_id = port_id
        self._attr_translation_placeholders = {"port": port_id}

    @property
    def port(self) -> MerakiSwitchPort:
        """Return the latest data for this entity's port."""
        return self.coordinator.data.ports[self._serial][self._port_id]

    @property
    def available(self) -> bool:
        """Return False if the port disappeared from the switch."""
        return super().available and self._port_id in self.coordinator.data.ports.get(
            self._serial, {}
        )


@callback
def async_setup_port_entities(
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_factory: Callable[[MerakiDataUpdateCoordinator, str, str], Entity],
    supported: Callable[[MerakiDataUpdateCoordinator, str, str], bool] | None = None,
) -> None:
    """Add an entity per switch port now and for ports that appear later."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_ports() -> None:
        current = {
            (serial, port_id)
            for serial, ports in coordinator.data.ports.items()
            for port_id in ports
            if supported is None or supported(coordinator, serial, port_id)
        }
        if new := current - known:
            known.update(new)
            async_add_entities(
                entity_factory(coordinator, serial, port_id)
                for serial, port_id in sorted(new)
            )

    _async_add_new_ports()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_ports))


class MerakiSsidEntity(CoordinatorEntity[MerakiDataUpdateCoordinator]):
    """An entity for one SSID, on a device for its network's Wi-Fi."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MerakiDataUpdateCoordinator,
        network_id: str,
        number: int,
        key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._ssid_key = (network_id, number)
        # By slot number, so renaming the SSID in the Dashboard keeps the entity.
        self._attr_unique_id = f"{network_id}_ssid_{number}_{key}"
        network = coordinator.networks.get(network_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{network_id}_wireless")},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer=MANUFACTURER,
            model="Wireless network",
            name=f"{network.name if network else network_id} Wi-Fi",
        )
        self._attr_translation_placeholders = {"ssid": self.ssid.name}

    @property
    def ssid(self) -> MerakiSsid:
        """Return the latest data for this entity's SSID."""
        return self.coordinator.data.ssids[self._ssid_key]

    @property
    def available(self) -> bool:
        """Return False if the SSID was reset to unconfigured."""
        return super().available and self._ssid_key in self.coordinator.data.ssids

    @callback
    def _handle_coordinator_update(self) -> None:
        if (
            self._ssid_key in self.coordinator.data.ssids
            and self._attr_translation_placeholders["ssid"] != self.ssid.name
        ):
            self._attr_translation_placeholders = {"ssid": self.ssid.name}
            # The cached name is built from the placeholders, but changing them does
            # not clear it; this is how Home Assistant clears it for _attr_name.
            self.__dict__.pop("name", None)
        super()._handle_coordinator_update()


@callback
def async_setup_ssid_entities(
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_factory: Callable[[MerakiDataUpdateCoordinator, str, int], Entity],
    supported: Callable[[MerakiDataUpdateCoordinator, str], bool] | None = None,
) -> None:
    """Add an entity per configured SSID now and for SSIDs configured later."""
    coordinator = entry.runtime_data
    known: set[tuple[str, int]] = set()

    @callback
    def _async_add_new_ssids() -> None:
        current = {
            key
            for key in coordinator.data.ssids
            if supported is None or supported(coordinator, key[0])
        }
        if new := current - known:
            known.update(new)
            async_add_entities(
                entity_factory(coordinator, network_id, number)
                for network_id, number in sorted(new)
            )

    _async_add_new_ssids()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_ssids))


@callback
def async_remove_unwritable(
    hass: HomeAssistant, entry: MerakiConfigEntry, domain: Platform
) -> None:
    """
    Remove the domain's entities in networks the API key can no longer change.

    Entities that change the configuration are only added with write access; this
    removes those added while the key still had it.
    """
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.domain == domain and not coordinator.can_write(
            _network_of(coordinator, registry_entry.unique_id)
        ):
            registry.async_remove(registry_entry.entity_id)


def _network_of(coordinator: MerakiDataUpdateCoordinator, unique_id: str) -> str | None:
    """Return the network of an entity by its unique ID."""
    if "_ssid_" in unique_id:
        return unique_id.split("_ssid_", maxsplit=1)[0]
    # Device entities start with the serial, which has no underscores.
    device = coordinator.data.devices.get(unique_id.split("_", maxsplit=1)[0])
    return device.network_id if device else None


class MerakiReadingEntity(CoordinatorEntity[MerakiSensorCoordinator]):
    """An entity fed by the readings of a Meraki MT sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MerakiSensorCoordinator, serial: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._serial = serial
        self._key = key
        self._attr_unique_id = reading_unique_id(serial, key)
        self._attr_translation_key = _snake(key)
        # The device is registered with its details by the device status sensor.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, serial)})


class MerakiLatestReadingEntity(MerakiReadingEntity):
    """An entity showing the latest reading of one metric of a Meraki MT sensor."""

    @property
    def _reading(self) -> MerakiReading | None:
        return (self.coordinator.data or {}).get((self._serial, self._key))

    @property
    def available(self) -> bool:
        """Return True once the metric has a reading."""
        return super().available and self._reading is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the reading was measured."""
        reading = self._reading
        return {"measured_at": reading.ts.isoformat() if reading else None}


def _snake(key: str) -> str:
    """Return a camelCase metric key as snake_case for translation keys."""
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in key)
