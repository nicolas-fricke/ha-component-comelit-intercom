"""Button platform for Comelit integration."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control_discovery import control_identity
from .coordinator import ComelitDataUpdateCoordinator
from .entity_migration import door_control, door_unique_id
from .video.models import Door

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the original Comelit door button entities."""
    coordinator: ComelitDataUpdateCoordinator = entry.runtime_data
    doors = coordinator.device_config.doors if coordinator.device_config else []
    async_add_entities([ComelitDoorButton(coordinator, door) for door in doors])


class ComelitDoorButton(CoordinatorEntity[ComelitDataUpdateCoordinator], ButtonEntity):
    """Representation of a Comelit door button."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: ComelitDataUpdateCoordinator,
        door: Door,
    ) -> None:
        """Initialize the button with its existing stable entity identity."""
        super().__init__(coordinator)
        self._door = door
        self._attr_name = door.name

        entry_unique_id = coordinator.entry.unique_id or coordinator.host
        self._attr_unique_id = door_unique_id(entry_unique_id, door)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_unique_id)},
            name=f"Comelit Intercom ({coordinator.host})",
            manufacturer="Comelit",
            model="ICONA Bridge",
        )

    async def async_press(self) -> None:
        """Open this door using the proven dedicated legacy connection."""
        await self.coordinator.async_open_door(self._door)

    @property
    def available(self) -> bool:
        """Return whether this door remains present in device configuration."""
        return self.coordinator.last_update_success and any(
            control_identity(door_control(d))
            == control_identity(door_control(self._door))
            for d in (
                self.coordinator.device_config.doors
                if self.coordinator.device_config
                else []
            )
        )
