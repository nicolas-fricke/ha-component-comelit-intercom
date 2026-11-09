"""Tests for Comelit config validation and stable identity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN

from custom_components.comelit_intercom.config_flow import validate_input
from custom_components.comelit_intercom.const import CONF_DEVICE_ID


@pytest.mark.asyncio
async def test_validation_uses_device_identity_and_custom_port() -> None:
    """New entries follow the device across DHCP address changes."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.authenticate = AsyncMock(return_value=200)
    client.get_config = AsyncMock(
        return_value={"vip": {"apt-address": "SB000006", "apt-subaddress": 1}}
    )
    client.shutdown = AsyncMock()

    with patch(
        "custom_components.comelit_intercom.config_flow.IconaBridgeClient",
        return_value=client,
    ) as client_class:
        result = await validate_input(
            MagicMock(),
            {CONF_HOST: " 192.0.2.10 ", CONF_PORT: 64101, CONF_TOKEN: "secret"},
        )

    client_class.assert_called_once_with("192.0.2.10", 64101)
    assert result[CONF_HOST] == "192.0.2.10"
    assert result[CONF_PORT] == 64101
    assert result[CONF_DEVICE_ID] == "SB000006:1"
    client.shutdown.assert_awaited_once()
