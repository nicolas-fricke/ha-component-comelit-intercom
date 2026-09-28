"""The Comelit Intercom integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import CONF_DEVICE_ID, DEFAULT_PORT, DOMAIN, INTEGRATION_VERSION
from .coordinator import ComelitDataUpdateCoordinator
from .entity_migration import door_unique_id_migrations
from .video.exceptions import AuthenticationError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SWITCH,
]

CARD_URL = "/comelit_intercom/comelit-intercom-card.js"
CARD_PATH = Path(__file__).parent / "www" / "comelit-intercom-card.js"

OBSOLETE_ENTITY_UNIQUE_ID_SUFFIXES = (
    "video_start",
    "video_stop",
    "call_audio",
    "doorbell",
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the privacy-aware intercom camera card."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=True)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={INTEGRATION_VERSION}")
    # Earlier betas stored this resource in Lovelace. The integration now
    # registers it with the frontend directly, so remove stale stored copies.
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return True
    resources = (
        lovelace.resources if hasattr(lovelace, "resources") else lovelace["resources"]
    )
    await resources.async_get_info()
    if isinstance(resources, ResourceStorageCollection):
        for item in list(resources.async_items()):
            if item.get("url", "").startswith(CARD_URL):
                await resources.async_delete_item(item["id"])
                _LOGGER.debug("Removed stale stored Comelit card resource")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Comelit from a config entry."""
    coordinator = ComelitDataUpdateCoordinator(hass, entry)

    try:
        await coordinator.async_setup_video()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("Authentication failed") from err
    except Exception as err:
        raise ConfigEntryNotReady("Failed to connect to Comelit device") from err

    if (
        CONF_DEVICE_ID not in entry.data
        and coordinator.device_config
        and coordinator.device_config.apt_address
    ):
        device_id = (
            f"{coordinator.device_config.apt_address}:"
            f"{coordinator.device_config.apt_subaddress}"
        )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_ID: device_id},
        )

    entry.runtime_data = coordinator
    _remove_obsolete_intercom_entities(hass, entry, coordinator.host)
    _migrate_beta_door_entity_unique_ids(hass, entry, coordinator)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_shutdown_video()
        entry.runtime_data = None
        raise

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Add explicit defaults without changing legacy entry/entity identities."""
    if entry.version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_PORT: entry.data.get(CONF_PORT, DEFAULT_PORT)},
            version=2,
        )
    return True


def _remove_obsolete_intercom_entities(
    hass: HomeAssistant, entry: ConfigEntry, host: str
) -> None:
    """Remove retired call controls while preserving camera and door buttons."""
    registry = er.async_get(hass)
    entry_unique_id = entry.unique_id or host
    obsolete_unique_ids = {
        f"{entry_unique_id}_{suffix}" for suffix in OBSOLETE_ENTITY_UNIQUE_ID_SUFFIXES
    }
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform == DOMAIN and entity.unique_id in obsolete_unique_ids:
            registry.async_remove(entity.entity_id)


def _migrate_beta_door_entity_unique_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ComelitDataUpdateCoordinator,
) -> None:
    """Restore stable door IDs used before the short-lived v1.3 beta format."""
    if not coordinator.device_config:
        return
    registry = er.async_get(hass)
    entities = {
        entity.unique_id: entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.platform == DOMAIN
    }
    entry_unique_id = entry.unique_id or coordinator.host
    for beta_id, stable_id in door_unique_id_migrations(
        entry_unique_id, coordinator.device_config.doors
    ).items():
        if beta_id not in entities:
            continue
        beta_entity = entities[beta_id]
        if stable_id in entities:
            registry.async_remove(beta_entity.entity_id)
            continue
        registry.async_update_entity(beta_entity.entity_id, new_unique_id=stable_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: ComelitDataUpdateCoordinator = entry.runtime_data
        await coordinator.async_shutdown_video()
        entry.runtime_data = None

    return bool(unload_ok)
