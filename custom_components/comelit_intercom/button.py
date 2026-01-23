"""Button platform for Comelit integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DOOR_COUNT, DEFAULT_DOOR_COUNT, DOMAIN
from .coordinator import ComelitDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Comelit button entities."""
    coordinator: ComelitDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    door_count = min(
        int(entry.options.get(CONF_DOOR_COUNT, DEFAULT_DOOR_COUNT)),
        2,
    )

    entities: list[ButtonEntity] = []

    # Backward compatible: door_count=1 uses address-book doors as before.
    if door_count <= 1:
        doors = coordinator.data.get("doors", [])
        for door in doors:
            entities.append(ComelitDoorButton(coordinator, door))
        async_add_entities(entities)
        return

    # Multi-door: create Door 1..N buttons which use the door index action.
    for idx in range(1, door_count + 1):
        entities.append(ComelitDoorIndexButton(coordinator, idx))
    async_add_entities(entities)


class ComelitDoorButton(CoordinatorEntity[ComelitDataUpdateCoordinator], ButtonEntity):
    """Representation of a Comelit door button."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: ComelitDataUpdateCoordinator,
        door: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._door = door
        self._attr_name = door.get("name", "Unknown Door")

        # Create unique ID based on host and door details
        door_id = f"{door.get('apt-address', '')}_{door.get('output-index', '')}"
        self._attr_unique_id = f"{coordinator.entry.unique_id}_{door_id}"

        # Set device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.unique_id)},
            name=f"Comelit Intercom ({coordinator.host})",
            manufacturer="Comelit",
            model="ICONA Bridge",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_open_door(self._door.get("name", ""))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self._door.get("name") in [
            d.get("name") for d in self.coordinator.data.get("doors", [])
        ]


class ComelitDoorIndexButton(
    CoordinatorEntity[ComelitDataUpdateCoordinator], ButtonEntity
):
    """Representation of a Comelit door button using a door index."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self, coordinator: ComelitDataUpdateCoordinator, door_index: int
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._door_index = int(door_index)
        self._attr_name = f"Door {self._door_index}"
        self._attr_unique_id = f"{coordinator.entry.unique_id}_door_{self._door_index}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.unique_id)},
            name=f"Comelit Intercom ({coordinator.host})",
            manufacturer="Comelit",
            model="ICONA Bridge",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_open_door_index(self._door_index)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success
