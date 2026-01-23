"""Config flow for Comelit integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .comelit_client import IconaBridgeClient
from .const import (
    CONF_CALL_TARGET,
    CONF_DOOR_COUNT,
    CONF_DOOR_DELAY,
    CONF_RESET_INTERVAL,
    DEFAULT_DOOR_COUNT,
    DEFAULT_DOOR_DELAY,
    DEFAULT_RESET_INTERVAL,
    DOMAIN,
)
from .token_extractor import extract_token

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_TOKEN): str,
    }
)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    _LOGGER.info("Starting validation for Comelit device at %s", data[CONF_HOST])

    # If no token provided, try to extract it automatically
    token = data.get(CONF_TOKEN)
    if not token:
        _LOGGER.info("No token provided, attempting automatic extraction")

        try:
            token = await asyncio.wait_for(extract_token(data[CONF_HOST]), timeout=30.0)
        except TimeoutError:
            _LOGGER.error("Token extraction timed out after 30 seconds")
            token = None
        except Exception as e:
            _LOGGER.error(f"Token extraction failed with error: {e}")
            token = None

        if not token:
            raise InvalidAuth(
                "Failed to extract token automatically. Please check that the device is accessible and using the default 'comelit' password, or enter your token manually."
            )

        _LOGGER.info("Successfully extracted token automatically")
        # Update data with extracted token
        data = dict(data)
        data[CONF_TOKEN] = token

    client = IconaBridgeClient(data[CONF_HOST])

    try:
        # Add timeout to prevent hanging
        _LOGGER.info("Attempting to connect to Comelit device at %s", data[CONF_HOST])
        await asyncio.wait_for(client.connect(), timeout=10.0)
        _LOGGER.info("Successfully connected to device")
    except TimeoutError as e:
        _LOGGER.error("Connection timeout to device at %s", data[CONF_HOST])
        raise CannotConnect("Connection timeout - device not responding") from e
    except OSError as err:
        # Special handling for macOS "No route to host" error
        if err.errno == 65:  # EHOSTUNREACH on macOS
            _LOGGER.error(
                "Cannot reach device at %s:%s - possible firewall or wrong port",
                data[CONF_HOST],
                64100,
            )
            raise CannotConnect(
                "Cannot reach device - check firewall settings"
            ) from err
        _LOGGER.error("Network error connecting to device: %s", err)
        raise CannotConnect(f"Network error: {err}") from err
    except Exception as err:
        _LOGGER.error("Cannot connect to device: %s", err)
        raise CannotConnect from err

    try:
        _LOGGER.info("Authenticating with device")
        auth_code = await asyncio.wait_for(
            client.authenticate(data[CONF_TOKEN]), timeout=15.0
        )

        if auth_code != 200:
            _LOGGER.error("Authentication failed with code %s", auth_code)
            raise InvalidAuth(f"Authentication failed with code {auth_code}")

        _LOGGER.info("Authentication successful, getting configuration")

        # Get configuration to verify everything works
        config = await asyncio.wait_for(client.get_config("all"), timeout=15.0)

        if not config:
            raise CannotConnect("Failed to get configuration")

        # Suggest a default call target from the first known door address, if present.
        vip = config.get("vip", {}) if isinstance(config, dict) else {}
        user_params = vip.get("user-parameters", {}) if isinstance(vip, dict) else {}
        doors = (
            user_params.get("opendoor-address-book", [])
            if isinstance(user_params, dict)
            else []
        )
        default_call_target = None
        if isinstance(doors, list):
            for door in doors:
                if not isinstance(door, dict):
                    continue
                addr = door.get("apt-address")
                if isinstance(addr, str) and addr.strip():
                    default_call_target = addr.strip()
                    break

        _LOGGER.info("Configuration retrieved successfully")
        await client.shutdown()

        # Return info that you want to store in the config entry
        return {
            "title": f"Comelit Intercom ({data[CONF_HOST]})",
            "token": data.get(CONF_TOKEN),  # Include token in case it was extracted
            "default_call_target": default_call_target,
        }

    except TimeoutError as err:
        _LOGGER.error("Operation timeout while communicating with device: %s", err)
        _LOGGER.error("This could be during connect, auth, or config retrieval")
        await client.shutdown()
        raise CannotConnect("Device communication timeout") from err
    except InvalidAuth:
        await client.shutdown()
        raise
    except CannotConnect:
        await client.shutdown()
        raise
    except Exception as err:
        _LOGGER.exception("Unexpected error during validation: %s", err)
        await client.shutdown()
        raise CannotConnect(f"Unexpected error: {err}") from err


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comelit."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._validated_data: dict[str, Any] | None = None
        self._title: str | None = None
        self._default_call_target: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                description_placeholders={
                    "token_help": "Leave token empty to try automatic extraction"
                },
            )

        errors = {}
        validated_data = None

        try:
            info = await validate_input(self.hass, user_input)
            # Get the potentially updated data (with extracted token)
            validated_data = user_input.copy()
            if not user_input.get(CONF_TOKEN) and info.get("token"):
                validated_data[CONF_TOKEN] = info["token"]
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth as e:
            errors["base"] = "invalid_auth"
            # If auto-extraction failed, show more helpful error
            if "extract token automatically" in str(e):
                errors["base"] = "auto_token_failed"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            # Check if already configured
            await self.async_set_unique_id(validated_data[CONF_HOST])
            self._abort_if_unique_id_configured()

            self._validated_data = validated_data
            self._title = str(info["title"])
            self._default_call_target = info.get("default_call_target")
            return await self.async_step_doors()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "token_help": "Leave token empty to try automatic extraction"
            },
        )

    async def async_step_doors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure multi-door behavior."""
        if self._validated_data is None or self._title is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}

        default_call_target = (self._default_call_target or "").strip()
        schema = vol.Schema(
            {
                vol.Required(CONF_CALL_TARGET, default=default_call_target): str,
                vol.Required(
                    CONF_DOOR_COUNT, default=DEFAULT_DOOR_COUNT
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=2, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_RESET_INTERVAL, default=DEFAULT_RESET_INTERVAL
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0)),
                vol.Required(CONF_DOOR_DELAY, default=DEFAULT_DOOR_DELAY): vol.All(
                    vol.Coerce(float), vol.Range(min=0.0, max=10.0)
                ),
            }
        )

        if user_input is None:
            return self.async_show_form(step_id="doors", data_schema=schema)

        call_target = str(user_input.get(CONF_CALL_TARGET, "")).strip()
        if not call_target:
            errors["base"] = "invalid_call_target"

        if errors:
            try:
                door_count_default = int(user_input.get(CONF_DOOR_COUNT))
            except Exception:
                door_count_default = DEFAULT_DOOR_COUNT
            door_count_default = min(max(1, door_count_default), 2)
            # Keep the user's input in the form defaults.
            schema = vol.Schema(
                {
                    vol.Required(CONF_CALL_TARGET, default=call_target): str,
                    vol.Required(
                        CONF_DOOR_COUNT, default=door_count_default
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=2, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_RESET_INTERVAL, default=user_input.get(CONF_RESET_INTERVAL)
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0)),
                    vol.Required(
                        CONF_DOOR_DELAY, default=user_input.get(CONF_DOOR_DELAY)
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0)),
                }
            )
            return self.async_show_form(
                step_id="doors",
                data_schema=schema,
                errors=errors,
            )

        options = {
            CONF_CALL_TARGET: call_target,
            CONF_DOOR_COUNT: int(user_input[CONF_DOOR_COUNT]),
            CONF_RESET_INTERVAL: float(user_input[CONF_RESET_INTERVAL]),
            CONF_DOOR_DELAY: float(user_input[CONF_DOOR_DELAY]),
        }
        return self.async_create_entry(
            title=self._title,
            data=self._validated_data,
            options=options,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Comelit Intercom."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        try:
            existing_door_count = int(
                self._config_entry.options.get(CONF_DOOR_COUNT, DEFAULT_DOOR_COUNT)
            )
        except Exception:
            existing_door_count = DEFAULT_DOOR_COUNT
        existing_door_count = min(max(1, existing_door_count), 2)

        if user_input is None:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_CALL_TARGET,
                            default=self._config_entry.options.get(
                                CONF_CALL_TARGET, ""
                            ),
                        ): str,
                        vol.Required(
                            CONF_DOOR_COUNT,
                            default=existing_door_count,
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1,
                                max=2,
                                step=1,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(
                            CONF_RESET_INTERVAL,
                            default=self._config_entry.options.get(
                                CONF_RESET_INTERVAL, DEFAULT_RESET_INTERVAL
                            ),
                        ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0)),
                        vol.Required(
                            CONF_DOOR_DELAY,
                            default=self._config_entry.options.get(
                                CONF_DOOR_DELAY, DEFAULT_DOOR_DELAY
                            ),
                        ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0)),
                    }
                ),
            )

        call_target = str(user_input.get(CONF_CALL_TARGET, "")).strip()
        if not call_target:
            try:
                default_door_count = int(user_input.get(CONF_DOOR_COUNT))
            except Exception:
                default_door_count = existing_door_count
            default_door_count = min(max(1, default_door_count), 2)
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_CALL_TARGET, default=call_target): str,
                        vol.Required(
                            CONF_DOOR_COUNT, default=default_door_count
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1,
                                max=2,
                                step=1,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(
                            CONF_RESET_INTERVAL,
                            default=user_input.get(CONF_RESET_INTERVAL),
                        ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=600.0)),
                        vol.Required(
                            CONF_DOOR_DELAY, default=user_input.get(CONF_DOOR_DELAY)
                        ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0)),
                    }
                ),
                errors={"base": "invalid_call_target"},
            )

        return self.async_create_entry(
            title="",
            data={
                CONF_CALL_TARGET: call_target,
                CONF_DOOR_COUNT: int(user_input[CONF_DOOR_COUNT]),
                CONF_RESET_INTERVAL: float(user_input[CONF_RESET_INTERVAL]),
                CONF_DOOR_DELAY: float(user_input[CONF_DOOR_DELAY]),
            },
        )
