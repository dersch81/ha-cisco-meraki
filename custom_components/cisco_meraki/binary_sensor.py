"""Binary sensor platform for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .entity import (
    MerakiEntity,
    MerakiLatestReadingEntity,
    MerakiPortEntity,
    async_setup_port_entities,
)
from .readings import BINARY_METRICS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator
    from .readings import MerakiBinaryMetric
    from .sensor_coordinator import MerakiSensorCoordinator

# The coordinator polls; entities never request their own updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up port links, MT40 lockouts, and MT door and water states."""
    async_setup_port_entities(entry, async_add_entities, MerakiPortLinkSensor)

    coordinator = entry.runtime_data
    known_mt40s: set[str] = set()

    @callback
    def _async_add_new_mt40s() -> None:
        if new := coordinator.data.power_controls.keys() - known_mt40s:
            known_mt40s.update(new)
            async_add_entities(
                MerakiRemoteLockoutSensor(coordinator, serial) for serial in sorted(new)
            )

    _async_add_new_mt40s()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_mt40s))

    if sensors := coordinator.sensors:
        _async_setup_reading_binary_sensors(entry, sensors, async_add_entities)


@callback
def _async_setup_reading_binary_sensors(
    entry: MerakiConfigEntry,
    coordinator: MerakiSensorCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a binary sensor per MT door or water state once it is first reported."""
    known: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_states() -> None:
        new = {
            key for key in (coordinator.data or {}) if key[1] in BINARY_METRICS
        } - known
        if not new:
            return
        known.update(new)
        async_add_entities(
            MerakiReadingBinarySensor(coordinator, serial, BINARY_METRICS[metric])
            for serial, metric in sorted(new)
        )

    _async_add_new_states()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_states))


class MerakiPortLinkSensor(MerakiPortEntity, BinarySensorEntity):
    """Reports whether something is connected to a switch port."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "port_link"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, port_id: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, port_id, "link")

    @property
    def is_on(self) -> bool | None:
        """Return True if the port has a link."""
        if self.port.status is None:
            return None
        return self.port.status == "Connected"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the link details and what is connected to the port."""
        port = self.port
        return {
            "connector": port.connector,
            "speed": port.speed,
            "duplex": port.duplex,
            "poe": port.poe_active,
            "neighbor": port.neighbor,
            "clients": port.clients,
        }


class MerakiRemoteLockoutSensor(MerakiEntity, BinarySensorEntity):
    """The lockout switch on an MT40; while locked, it ignores remote commands."""

    # On means unlocked: remote power commands work.
    _attr_device_class = BinarySensorDeviceClass.LOCK
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "remote_lockout"

    def __init__(self, coordinator: MerakiDataUpdateCoordinator, serial: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, "remote_lockout")

    @property
    def is_on(self) -> bool | None:
        """Return True if the lockout switch is unlocked."""
        locked = self.coordinator.data.power_controls[self._serial].locked
        return None if locked is None else not locked


class MerakiReadingBinarySensor(MerakiLatestReadingEntity, BinarySensorEntity):
    """
    The door (MT20) or water (MT12) state of a Meraki MT sensor.

    Over MQTT, a change arrives within seconds. Through the API it shows up with the
    next poll, as the sensor uploads its readings. A sensor that has gone silent may
    have missed a change, so its state becomes unknown.
    """

    def __init__(
        self,
        coordinator: MerakiSensorCoordinator,
        serial: str,
        metric: MerakiBinaryMetric,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial, metric.key)
        self._attr_device_class = metric.device_class

    @property
    def is_on(self) -> bool | None:
        """Return True while the door is open or water is detected."""
        reading = self._reading
        if reading is None or self.coordinator.is_silent(self._serial):
            return None
        return reading.value > 0
