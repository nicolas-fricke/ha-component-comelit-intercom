"""Test service for Comelit connection."""

import logging

from homeassistant.core import HomeAssistant, ServiceCall

from .comelit_client import IconaBridgeClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_test_service(hass: HomeAssistant):
    """Set up test service."""

    async def handle_test_connection(call: ServiceCall):
        """Handle test connection service."""
        ip = call.data.get("ip")
        token = call.data.get("token")

        if not ip:
            _LOGGER.error("IP address is required")
            hass.states.async_set(
                f"{DOMAIN}.test_result", "error", {"message": "IP address is required"}
            )
            return

        if not token:
            _LOGGER.error("Token is required")
            hass.states.async_set(
                f"{DOMAIN}.test_result", "error", {"message": "Token is required"}
            )
            return

        _LOGGER.info(f"Testing connection to {ip}")

        client = IconaBridgeClient(ip)
        try:
            await client.connect()
            _LOGGER.info("Connected successfully!")

            auth_code = await client.authenticate(token)
            if auth_code != 200:
                _LOGGER.error("Authentication failed with code %s", auth_code)
                hass.states.async_set(
                    f"{DOMAIN}.test_result",
                    "auth_failed",
                    {"message": f"Authentication failed with code {auth_code}"},
                )
                return

            _LOGGER.info("Authenticated successfully!")
            doors = await client.list_doors()
            _LOGGER.info("Found %s doors", len(doors))

            hass.states.async_set(
                f"{DOMAIN}.test_result",
                "success",
                {"doors": doors, "message": f"Found {len(doors)} doors"},
            )

        except Exception as e:
            _LOGGER.error(f"Connection failed: {e}")
            hass.states.async_set(
                f"{DOMAIN}.test_result", "connection_failed", {"message": str(e)}
            )
        finally:
            await client.shutdown()

    hass.services.async_register(
        DOMAIN, "test_connection", handle_test_connection, schema=None
    )

    _LOGGER.info("Test service registered")
