"""Tests for the door (MT20) and water (MT12) states of Meraki MT sensors."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cisco_meraki.mqtt_source import parse_message
from custom_components.cisco_meraki.readings import parse_reading

from .conftest import NETWORK_ID

NOW = datetime(2026, 10, 20, 12, 20, tzinfo=UTC)
DOOR = "Q3DD-DDDD-DDDD"
DOOR_MAC = "6C:7F:0C:11:2B:D0"
LEAK = "Q3WW-WWWW-WWWW"
LEAK_MAC = "6C:7F:0C:11:2B:E0"
DOOR_PREFIX = f"meraki/v1/mt/{NETWORK_ID}/ble/{DOOR_MAC}"


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _api_readings() -> list[dict[str, Any]]:
    """Return the door closed at 12:00 and open at 12:10, the floor dry, batteries."""
    return [
        {
            "serial": DOOR,
            "ts": _iso(NOW - timedelta(minutes=20)),
            "metric": "door",
            "door": {"open": False},
        },
        {
            "serial": DOOR,
            "ts": _iso(NOW - timedelta(minutes=10)),
            "metric": "door",
            "door": {"open": True},
        },
        {
            "serial": DOOR,
            "ts": _iso(NOW - timedelta(minutes=10)),
            "metric": "battery",
            "battery": {"percentage": 91},
        },
        {
            "serial": LEAK,
            "ts": _iso(NOW - timedelta(minutes=15)),
            "metric": "water",
            "water": {"present": False},
        },
        {
            "serial": LEAK,
            "ts": _iso(NOW - timedelta(minutes=15)),
            "metric": "battery",
            "battery": {"percentage": 97},
        },
    ]


@pytest.fixture
def mock_states(mock_client: MagicMock) -> MagicMock:
    """Add an MT20 and an MT12 that the API knows."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        {
            "serial": DOOR,
            "name": "Front Door",
            "model": "MT20",
            "mac": DOOR_MAC.lower(),
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
        {
            "serial": LEAK,
            "name": "Garage Leak",
            "model": "MT12",
            "mac": LEAK_MAC.lower(),
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
    ]
    readings = _api_readings()
    # Like the real API, newest first.
    mock_client.sensor.getOrganizationSensorReadingsHistory = AsyncMock(
        return_value=sorted(readings, key=lambda r: r["ts"], reverse=True)
    )
    mock_client.sensor.getOrganizationSensorReadingsLatest = AsyncMock(
        return_value=[
            {"serial": DOOR, "readings": [readings[1], readings[2]]},
            {"serial": LEAK, "readings": [readings[3], readings[4]]},
        ]
    )
    return mock_client


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"metric": "door", "door": {"open": True}}, ("door", 1.0)),
        ({"metric": "door", "door": {"open": False}}, ("door", 0.0)),
        ({"metric": "water", "water": {"present": True}}, ("water", 1.0)),
    ],
)
def test_parse_api_reading(raw: dict[str, Any], expected: tuple[str, float]) -> None:
    """Door and water readings from the API become on/off readings."""
    reading = parse_reading(DOOR, {"ts": "2026-10-20T12:00:00Z", **raw})
    assert reading is not None
    assert (reading.metric, reading.value) == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"metric": "door", "door": {}},
        # A number is not a door state.
        {"metric": "door", "door": {"open": 1}},
        {"metric": "water", "water": {"present": "yes"}},
    ],
)
def test_parse_api_reading_ignores(raw: dict[str, Any]) -> None:
    """Door and water readings without a boolean state are ignored."""
    assert parse_reading(DOOR, {"ts": "2026-10-20T12:00:00Z", **raw}) is None


@pytest.mark.parametrize(
    ("metric", "payload", "expected"),
    [
        ("door", '{"ts":"2023-08-12T18:00:54Z","open":false}', ("door", 0.0)),
        ("door", '{"ts":"2023-08-12T18:00:54Z","open":true}', ("door", 1.0)),
        ("waterDetection", '{"ts":"2023-08-12T17:59:38Z","wet":true}', ("water", 1.0)),
        (
            "batteryPercentage",
            '{"ts":"2023-08-12T17:59:38Z","batteryPercentage":88}',
            ("battery", 88.0),
        ),
    ],
)
def test_parse_mqtt_message(
    metric: str, payload: str, expected: tuple[str, float]
) -> None:
    """Meraki's MQTT payloads for doors, leaks and batteries are understood."""
    parsed = parse_message(f"{DOOR_PREFIX}/{metric}", payload)
    assert parsed is not None
    mac, reading = parsed
    assert mac == DOOR_MAC.lower()
    assert (reading.metric, reading.value) == expected


def test_parse_mqtt_message_ignores_non_boolean() -> None:
    """A door message without a boolean state is ignored."""
    assert parse_message(f"{DOOR_PREFIX}/door", '{"ts":"2023-08-12T18:00:54Z"}') is None


@pytest.mark.usefixtures("mock_states")
async def test_binary_sensors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Doors and leaks get binary sensors with their latest state, no statistics."""
    freezer.move_to(NOW)
    await _setup(hass, mock_config_entry)

    door = hass.states.get("binary_sensor.front_door_door")
    assert door.state == STATE_ON
    assert door.attributes["device_class"] == "door"
    assert door.attributes["measured_at"] == (NOW - timedelta(minutes=10)).isoformat()
    water = hass.states.get("binary_sensor.garage_leak_water")
    assert water.state == STATE_OFF
    assert water.attributes["device_class"] == "moisture"

    # Battery stays a sensor; door and water states get no sensor of their own.
    assert hass.states.get("sensor.front_door_battery").state == "91.0"
    assert hass.states.get("sensor.front_door_door") is None
    assert hass.states.get("sensor.garage_leak_water") is None
    assert entity_registry.async_get_entity_id(
        "binary_sensor", "cisco_meraki", f"{DOOR}_door"
    )

    statistic_ids = {
        row["statistic_id"]
        for row in await get_instance(hass).async_add_executor_job(
            list_statistic_ids, hass
        )
    }
    assert not {s for s in statistic_ids if s.endswith(("_door", "_water"))}


@pytest.mark.usefixtures("mqtt_mock", "mock_states")
async def test_door_over_mqtt(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A door that closes is reported over MQTT within seconds."""
    freezer.move_to(NOW)
    await _setup(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.front_door_door").state == STATE_ON

    async_fire_mqtt_message(
        hass, f"{DOOR_PREFIX}/door", json.dumps({"ts": _iso(NOW), "open": False})
    )
    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    door = hass.states.get("binary_sensor.front_door_door")
    assert door.state == STATE_OFF
    assert door.attributes["measured_at"] == NOW.isoformat()
