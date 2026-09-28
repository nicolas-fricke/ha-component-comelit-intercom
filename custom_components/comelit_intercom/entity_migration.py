"""Stable entity identities and compatibility mappings."""

from __future__ import annotations

from .control_discovery import (
    CONTROL_TYPE_ACTUATOR,
    CONTROL_TYPE_OPENDOOR,
    control_identity,
)
from .video.models import Door


def door_control(door: Door) -> dict[str, object]:
    """Convert a door model to the stable legacy identity fields."""
    control: dict[str, object] = {
        "control-type": (
            CONTROL_TYPE_ACTUATOR if door.is_actuator else CONTROL_TYPE_OPENDOOR
        ),
        "apt-address": door.apt_address,
        "output-index": door.output_index,
    }
    if door.module_index is not None:
        control["module-index"] = door.module_index
    return control


def door_unique_id(entry_unique_id: str, door: Door) -> str:
    """Return the stable unique ID used by releases before camera support."""
    control_type, apt_address, output_index, module_index = control_identity(
        door_control(door)
    )
    module_suffix = f"_{module_index}" if module_index is not None else ""
    return (
        f"{entry_unique_id}_{control_type}_{apt_address}_{output_index}"
        f"{module_suffix}"
    )


def beta_door_unique_id(entry_unique_id: str, door: Door) -> str:
    """Return the short-lived v1.3 beta identity so it can be migrated."""
    control_type = CONTROL_TYPE_ACTUATOR if door.is_actuator else "door"
    module_index = door.module_index if door.module_index is not None else 0
    return (
        f"{entry_unique_id}_{control_type}_{door.apt_address}_"
        f"{door.output_index}_{module_index}"
    )


def door_unique_id_migrations(
    entry_unique_id: str, doors: list[Door]
) -> dict[str, str]:
    """Map each changed v1.3 beta ID to its stable predecessor."""
    migrations: dict[str, str] = {}
    for door in doors:
        stable_id = door_unique_id(entry_unique_id, door)
        beta_id = beta_door_unique_id(entry_unique_id, door)
        if beta_id != stable_id:
            migrations[beta_id] = stable_id
    return migrations
