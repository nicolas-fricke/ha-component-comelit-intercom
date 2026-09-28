"""Helpers for discovering Comelit door and actuator controls."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

CONTROL_TYPE_OPENDOOR = "opendoor"
CONTROL_TYPE_ACTUATOR = "actuator"


def extract_controls_from_vip(vip_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract Home Assistant controls from the VIP configuration."""
    user_parameters = vip_config.get("user-parameters", {})
    if not isinstance(user_parameters, dict):
        return []

    controls: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int | None]] = set()

    for control in _normalize_opendoor_entries(
        user_parameters.get("opendoor-address-book", [])
    ):
        _add_control(controls, seen, control)

    for control in _normalize_actuator_entries(
        user_parameters.get("actuator-address-book", []),
        user_parameters.get("additional-actuator", []),
    ):
        _add_control(controls, seen, control)

    return controls


def control_identity(control: dict[str, Any]) -> tuple[str, str, int, int | None]:
    """Return a stable identity for a discovered control."""
    return (
        str(control.get("control-type", CONTROL_TYPE_OPENDOOR)),
        str(control.get("apt-address", "")),
        int(control.get("output-index", -1)),
        _coerce_int(control.get("module-index")),
    )


def _normalize_opendoor_entries(entries: Any) -> list[dict[str, Any]]:
    """Normalize standard open-door entries."""
    controls: list[dict[str, Any]] = []
    for index, entry in enumerate(_as_entries(entries), start=1):
        apt_address = _first_string(entry, "apt-address")
        output_index = _coerce_int(entry.get("output-index"))
        if apt_address is None or output_index is None:
            continue

        controls.append(
            {
                "control-type": CONTROL_TYPE_OPENDOOR,
                "name": _first_string(entry, "name") or f"Door {index}",
                "apt-address": apt_address,
                "output-index": output_index,
                "secure-mode": bool(entry.get("secure-mode", False)),
            }
        )

    return controls


def _normalize_actuator_entries(
    actuator_entries: Any,
    additional_entries: Any,
) -> list[dict[str, Any]]:
    """Normalize actuator entries using the same data model as comelit-client."""
    actuator_entries = _as_entries(actuator_entries)
    supplemental_by_index = _as_entries(additional_entries)
    supplemental_by_address: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in supplemental_by_index:
        apt_address = _first_string(entry, "apt-address")
        if apt_address is not None:
            supplemental_by_address[apt_address].append(entry)

    controls: list[dict[str, Any]] = []
    for index, entry in enumerate(actuator_entries, start=1):
        if not isinstance(entry, dict):
            continue

        merged: dict[str, Any] = {}
        _merge_non_empty(merged, entry)

        apt_address = _first_string(merged, "apt-address")
        if apt_address is None:
            continue

        supplemental = _find_matching_additional_actuator(
            entry,
            apt_address,
            (
                supplemental_by_index[index - 1]
                if index - 1 < len(supplemental_by_index)
                else None
            ),
            supplemental_by_address,
        )
        if supplemental is not None:
            _merge_non_empty(merged, supplemental)

        output_index = _coerce_int(merged.get("output-index"))
        if output_index is None:
            continue

        control = {
            "control-type": CONTROL_TYPE_ACTUATOR,
            "name": _first_string(merged, "name") or f"Actuator {index}",
            "apt-address": apt_address,
            "output-index": output_index,
        }

        module_index = _coerce_int(merged.get("module-index"))
        if module_index is not None:
            control["module-index"] = module_index

        if "enabled" in merged:
            control["enabled"] = bool(merged["enabled"])

        controls.append(control)

    return controls


def _find_matching_additional_actuator(
    actuator_entry: dict[str, Any],
    apt_address: str,
    indexed_entry: dict[str, Any] | None,
    supplemental_by_address: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Select the supplemental actuator entry that belongs to this actuator."""
    if indexed_entry is not None and _additional_actuator_matches(
        actuator_entry, apt_address, indexed_entry
    ):
        return indexed_entry

    candidates = [
        entry
        for entry in supplemental_by_address.get(apt_address, [])
        if _additional_actuator_matches(actuator_entry, apt_address, entry)
    ]
    if len(candidates) == 1:
        return candidates[0]

    return None


def _additional_actuator_matches(
    actuator_entry: dict[str, Any],
    apt_address: str,
    supplemental_entry: dict[str, Any],
) -> bool:
    """Return whether a supplemental actuator entry is compatible."""
    if _first_string(supplemental_entry, "apt-address") != apt_address:
        return False

    for key in ("output-index", "module-index"):
        actuator_value = _coerce_int(actuator_entry.get(key))
        supplemental_value = _coerce_int(supplemental_entry.get(key))
        if (
            actuator_value is not None
            and supplemental_value is not None
            and actuator_value != supplemental_value
        ):
            return False

    return True


def _add_control(
    controls: list[dict[str, Any]],
    seen: set[tuple[str, str, int, int | None]],
    control: dict[str, Any],
) -> None:
    """Append a control if its identity has not already been seen."""
    identity = control_identity(control)
    if identity in seen:
        return

    seen.add(identity)
    controls.append(control)


def _first_string(entry: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string value for the provided keys."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _coerce_int(value: Any) -> int | None:
    """Convert values to integers when possible."""
    if isinstance(value, int):
        return value

    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None

    return None


def _merge_non_empty(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge non-empty values from ``source`` into ``target``."""
    for key, value in source.items():
        if value in (None, "", [], {}):
            continue
        target[key] = value


def _as_entries(value: Any) -> list[dict[str, Any]]:
    """Return address-book entries from list and singleton firmware shapes."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []
