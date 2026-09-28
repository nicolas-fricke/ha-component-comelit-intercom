"""Number entities for Comelit dashboard previews."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_STILL_PREVIEW_INTERVAL,
    DOMAIN,
    MAX_STILL_PREVIEW_INTERVAL,
    MIN_STILL_PREVIEW_INTERVAL,
    STILL_PREVIEW_INTERVAL_STEP,
)
from .coordinator import ComelitDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the still-preview interval number."""
    coordinator: ComelitDataUpdateCoordinator = entry.runtime_data
    async_add_entities([ComelitStillPreviewInterval(coordinator)])


class ComelitStillPreviewInterval(RestoreNumber):
    """Select minutes between stills, with zero meaning live video."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"
    _attr_translation_key = "still_preview_interval"
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False
    _attr_native_min_value = MIN_STILL_PREVIEW_INTERVAL
    _attr_native_max_value = MAX_STILL_PREVIEW_INTERVAL
    _attr_native_step = STILL_PREVIEW_INTERVAL_STEP
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: ComelitDataUpdateCoordinator) -> None:
        """Initialize the interval with a conservative default."""
        entry_unique_id = coordinator.entry.unique_id or coordinator.host
        self._attr_unique_id = f"{entry_unique_id}_still_preview_interval"
        self._attr_native_value = DEFAULT_STILL_PREVIEW_INTERVAL
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_unique_id)},
            name=f"Comelit Intercom ({coordinator.host})",
            manufacturer="Comelit",
            model="ICONA Bridge",
        )

    async def async_added_to_hass(self) -> None:
        """Restore the user's previous interval."""
        await super().async_added_to_hass()
        if (last_number := await self.async_get_last_number_data()) is not None:
            if last_number.native_value is not None:
                self._attr_native_value = self._normalized(last_number.native_value)

    async def async_set_native_value(self, value: float) -> None:
        """Store a valid half-minute interval."""
        self._attr_native_value = self._normalized(value)
        self.async_write_ha_state()

    @staticmethod
    def _normalized(value: float) -> float:
        """Clamp and round a value to a supported half-minute step."""
        stepped = round(float(value) / STILL_PREVIEW_INTERVAL_STEP)
        normalized = stepped * STILL_PREVIEW_INTERVAL_STEP
        return min(
            MAX_STILL_PREVIEW_INTERVAL,
            max(MIN_STILL_PREVIEW_INTERVAL, normalized),
        )
