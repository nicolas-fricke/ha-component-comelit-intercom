"""Build the video protocol configuration from the cached VIP response."""

from __future__ import annotations

from typing import Any

from .models import DeviceConfig


def _entries(value: Any) -> list[dict[str, Any]]:
    """Normalize address books returned as either one object or a list."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


def device_config_from_vip(vip: dict[str, Any]) -> DeviceConfig:
    """Return the subset of device configuration required for video calls."""
    raw_subaddress = vip.get("apt-subaddress", 0)
    try:
        apt_subaddress = int(raw_subaddress or 0)
    except (TypeError, ValueError):
        apt_subaddress = 0

    config = DeviceConfig(
        apt_address=str(vip.get("apt-address", "")),
        apt_subaddress=apt_subaddress,
        raw={"vip": vip},
    )

    user_parameters = vip.get("user-parameters", {})
    if not isinstance(user_parameters, dict):
        return config

    entrance_book = _entries(user_parameters.get("entrance-address-book"))
    if entrance_book:
        config.caller_address = str(entrance_book[0].get("apt-address", ""))

    return config
