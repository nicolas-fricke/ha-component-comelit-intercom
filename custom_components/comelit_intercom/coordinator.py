"""DataUpdateCoordinator for Comelit."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .comelit_client import IconaBridgeClient
from .const import (
    CONF_CALL_TARGET,
    CONF_DOOR_COUNT,
    CONF_DOOR_DELAY,
    CONF_HOST,
    CONF_RESET_INTERVAL,
    CONF_TOKEN,
    DEFAULT_DOOR_COUNT,
    DEFAULT_DOOR_DELAY,
    DEFAULT_PORT,
    DEFAULT_RESET_INTERVAL,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .peer_door_opener import PeerDoorConfig, PeerDoorOpener

_LOGGER = logging.getLogger(__name__)


class ComelitDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Comelit data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        self.host = entry.data[CONF_HOST]
        self.token = entry.data[CONF_TOKEN]
        self.client = IconaBridgeClient(self.host)
        self.vip_config: dict[str, Any] = {}
        self._peer_door_opener = PeerDoorOpener(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Comelit."""
        try:
            await self.client.connect()

            # Authenticate
            auth_code = await self.client.authenticate(self.token)
            if auth_code != 200:
                raise ConfigEntryAuthFailed(
                    f"Authentication failed with code {auth_code}"
                )

            # Get configuration
            config = await self.client.get_config("all")
            if not config or "vip" not in config:
                raise UpdateFailed("Failed to get configuration from device")

            self.vip_config = config["vip"]
            doors = self.vip_config.get("user-parameters", {}).get(
                "opendoor-address-book", []
            )

            return {"doors": doors, "vip": self.vip_config}

        except ConfigEntryAuthFailed:
            # Re-raise auth errors
            raise
        except Exception as err:
            _LOGGER.error("Error communicating with Comelit device: %s", err)
            raise UpdateFailed(f"Error communicating with device: {err}") from err
        finally:
            # Always close the connection after update
            await self.client.shutdown()

    async def async_open_door(self, door_name: str) -> None:
        """Open a specific door."""
        # Create a separate client instance for door operations
        # to avoid interfering with the coordinator's update cycle
        door_client = IconaBridgeClient(self.host)
        try:
            await door_client.connect()

            # Authenticate
            auth_code = await door_client.authenticate(self.token)
            if auth_code != 200:
                raise Exception(f"Authentication failed with code {auth_code}")

            # Find the door
            doors = self.data.get("doors", [])
            door = next((d for d in doors if d.get("name") == door_name), None)
            if not door:
                raise Exception(f"Door '{door_name}' not found")

            # Open the door
            await door_client.open_door(self.vip_config, door)

        except Exception as err:
            _LOGGER.error("Error opening door %s: %s", door_name, err)
            raise
        finally:
            # Always clean up the door client connection
            await door_client.shutdown()

    def _door_toggle_config(self) -> PeerDoorConfig:
        call_target = str(self.entry.options.get(CONF_CALL_TARGET, "")).strip()
        door_count = int(self.entry.options.get(CONF_DOOR_COUNT, DEFAULT_DOOR_COUNT))
        reset_interval = float(
            self.entry.options.get(CONF_RESET_INTERVAL, DEFAULT_RESET_INTERVAL)
        )
        door_delay = float(self.entry.options.get(CONF_DOOR_DELAY, DEFAULT_DOOR_DELAY))

        return PeerDoorConfig(
            call_target=call_target,
            door_count=min(max(1, door_count), 2),
            cam2_reuse_window=max(0.0, reset_interval),
            door_delay=max(0.0, door_delay),
        )

    def _door_item_for_index(self, door_index: int) -> dict[str, Any]:
        doors = self.data.get("doors", [])
        if not isinstance(doors, list) or not doors:
            raise RuntimeError("No doors found in device configuration")
        if len(doors) == 1:
            first = doors[0]
            if not isinstance(first, dict):
                raise RuntimeError("Invalid door entry in configuration")
            return first

        for door in doors:
            if not isinstance(door, dict):
                continue
            try:
                if int(door.get("output-index")) == int(door_index):
                    return door
            except Exception:
                continue
        first = doors[0]
        if not isinstance(first, dict):
            raise RuntimeError("Invalid door entry in configuration")
        return first

    async def async_open_door_index(self, door_index: int) -> None:
        """Open a door by index.

        Multi-door mode uses a peer-aware strategy:
        - Door open targets the currently active peer/stream.
        - Door 2 is reached by a short call + restart sequence to select the cam2 peer.
        - Door 1 after Door 2 may need to wait for fallback to avoid switching back.
        """
        idx = int(door_index)
        cfg = self._door_toggle_config()
        _LOGGER.info(
            "Door open requested: door_index=%s door_count=%s call_target=%s",
            idx,
            int(cfg.door_count),
            cfg.call_target,
        )

        if idx < 1 or idx > int(cfg.door_count):
            raise ValueError("door_index must be in 1..door_count")

        if not cfg.call_target:
            raise RuntimeError(
                "Multi-door is enabled but no entrance unit address is configured"
            )

        # Peer-aware door opening is handled by a dedicated opener which
        # implements the proven call-restart + door-open-only sequences.
        await self._peer_door_opener.async_open_door_index(
            host=self.host,
            port=int(DEFAULT_PORT),
            token=self.token,
            config=cfg,
            door_index=int(idx),
            output_index=1,
        )
        return
