"""Tests for compatibility migrations."""

from __future__ import annotations

from custom_components.comelit_intercom.entity_migration import (
    door_unique_id_migrations,
)
from custom_components.comelit_intercom.video.models import Door


def test_v13_beta_door_id_maps_back_to_stable_id() -> None:
    """Upgrade a beta door without changing its existing entity_id."""
    door = Door(
        id=0,
        index=0,
        name="Entrance",
        apt_address="SB100001",
        output_index=1,
    )

    assert door_unique_id_migrations("legacy-host", [door]) == {
        "legacy-host_door_SB100001_1_0": "legacy-host_opendoor_SB100001_1"
    }
