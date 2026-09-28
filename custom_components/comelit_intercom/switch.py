"""Switch entities for Comelit dashboard previews."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import ComelitDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the automatic still-preview switch."""
    coordinator: ComelitDataUpdateCoordinator = entry.runtime_data
    async_add_entities([ComelitAutomaticStillPreviewsSwitch(coordinator)])


class ComelitAutomaticStillPreviewsSwitch(RestoreEntity, SwitchEntity):
    """Enable automatic previews in visible Comelit dashboard cards."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:camera-timer"
    _attr_translation_key = "automatic_still_previews"
    _attr_should_poll = False

    def __init__(self, coordinator: ComelitDataUpdateCoordinator) -> None:
        """Initialize the preview switch, disabled for privacy by default."""
        entry_unique_id = coordinator.entry.unique_id or coordinator.host
        self._attr_unique_id = f"{entry_unique_id}_automatic_still_previews"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_unique_id)},
            name=f"Comelit Intercom ({coordinator.host})",
            manufacturer="Comelit",
            model="ICONA Bridge",
        )

    async def async_added_to_hass(self) -> None:
        """Restore the user's previous preference."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable dashboard-controlled previews."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable dashboard-controlled previews."""
        self._attr_is_on = False
        self.async_write_ha_state()
