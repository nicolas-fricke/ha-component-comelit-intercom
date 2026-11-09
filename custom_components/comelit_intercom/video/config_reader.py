"""Configuration retrieval and parsing via the UCFG channel."""

from __future__ import annotations

import logging
from typing import Any

from ..control_discovery import CONTROL_TYPE_ACTUATOR, extract_controls_from_vip
from .channels import ChannelType, ViperMessageId
from .client import IconaBridgeClient
from .config import device_config_from_vip
from .exceptions import ProtocolError
from .models import Camera, DeviceConfig, Door

_LOGGER = logging.getLogger(__name__)


def _entries(value: Any) -> list[dict[str, Any]]:
    """Normalize firmware fields returned as a singleton or a list."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


async def get_device_config(client: IconaBridgeClient) -> DeviceConfig:
    """Fetch and parse device configuration from the UCFG channel."""
    channel = await client.open_channel("UCFG", ChannelType.UCFG)

    msg = {
        "message": "get-configuration",
        "addressbooks": "all",
        "message-type": "request",
        "message-id": int(ViperMessageId.UCFG),
    }

    response = await client.send_json(channel, msg)
    _LOGGER.debug("Config response keys: %s", list(response.keys()))

    code = response.get("response-code", 0)
    if code != 200:
        raise ProtocolError(f"Config request returned code {code}")

    return _parse_config(response)


def _parse_config(data: dict[str, Any]) -> DeviceConfig:
    """Parse the raw config JSON into a DeviceConfig."""
    vip = data.get("vip", {})
    if not isinstance(vip, dict):
        vip = {}
    config = device_config_from_vip(vip)
    config.raw = data

    user_params = vip.get("user-parameters", {})
    if not isinstance(user_params, dict):
        user_params = {}

    # Use the fork's hardened discovery so additional/singleton actuators
    # retain the exact behavior already validated on the 6741W.
    for door_index, item in enumerate(extract_controls_from_vip(vip)):
        config.doors.append(
            Door(
                id=door_index,
                index=door_index,
                name=item.get("name", ""),
                apt_address=item.get("apt-address", ""),
                output_index=item.get("output-index", 0),
                secure_mode=item.get("secure-mode", False),
                is_actuator=item.get("control-type") == CONTROL_TYPE_ACTUATOR,
                module_index=item.get("module-index"),
            )
        )

    # Parse cameras from rtsp-camera-address-book
    for item in _entries(user_params.get("rtsp-camera-address-book", [])):
        config.cameras.append(
            Camera(
                id=item.get("id", 0),
                name=item.get("name", ""),
                rtsp_url=item.get("rtsp-url", ""),
                rtsp_user=item.get("rtsp-user", ""),
                rtsp_password=item.get("rtsp-password", ""),
            )
        )

    _LOGGER.info(
        "Parsed config: %d doors, %d cameras", len(config.doors), len(config.cameras)
    )
    return config
