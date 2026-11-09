"""Tests for privacy-aware dashboard preview settings."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "homeassistant.components.number",
    reason="Home Assistant is required for preview entity tests",
)

from custom_components.comelit_intercom.const import (
    DEFAULT_STILL_PREVIEW_INTERVAL,
    MAX_STILL_PREVIEW_INTERVAL,
    MIN_STILL_PREVIEW_INTERVAL,
    STILL_PREVIEW_INTERVAL_STEP,
)
from custom_components.comelit_intercom.number import ComelitStillPreviewInterval
from custom_components.comelit_intercom.switch import (
    ComelitAutomaticStillPreviewsSwitch,
)


@pytest.fixture
def coordinator() -> MagicMock:
    """Return a coordinator with stable device identity fields."""
    instance = MagicMock()
    instance.entry.unique_id = "comelit-test"
    instance.host = "192.0.2.10"
    return instance


def test_preview_switch_is_disabled_by_default(coordinator: MagicMock) -> None:
    """Automatic captures require an explicit opt-in."""
    entity = ComelitAutomaticStillPreviewsSwitch(coordinator)

    assert entity.is_on is False
    assert entity.unique_id == "comelit-test_automatic_still_previews"


@pytest.mark.asyncio
async def test_preview_switch_can_be_toggled(coordinator: MagicMock) -> None:
    """The switch stores its state without polling the panel."""
    entity = ComelitAutomaticStillPreviewsSwitch(coordinator)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()
    assert entity.is_on is True

    await entity.async_turn_off()
    assert entity.is_on is False
    assert entity.async_write_ha_state.call_count == 2


def test_preview_interval_metadata(coordinator: MagicMock) -> None:
    """The selector exposes the requested range and half-minute resolution."""
    entity = ComelitStillPreviewInterval(coordinator)

    assert entity.native_value == DEFAULT_STILL_PREVIEW_INTERVAL
    assert entity.native_min_value == MIN_STILL_PREVIEW_INTERVAL
    assert entity.native_max_value == MAX_STILL_PREVIEW_INTERVAL
    assert entity.native_step == STILL_PREVIEW_INTERVAL_STEP
    assert entity.unique_id == "comelit-test_still_preview_interval"


@pytest.mark.asyncio
async def test_preview_interval_rounds_and_clamps(coordinator: MagicMock) -> None:
    """Values stay in range and on a supported half-minute step."""
    entity = ComelitStillPreviewInterval(coordinator)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(12.26)
    assert entity.native_value == 12.5

    await entity.async_set_native_value(-2)
    assert entity.native_value == 0

    await entity.async_set_native_value(200)
    assert entity.native_value == 180
