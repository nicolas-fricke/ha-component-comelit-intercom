"""Config flow for the Comelit integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .comelit_client import IconaBridgeClient
from .const import CONF_DEVICE_ID, DEFAULT_PORT, DOMAIN
from .token_extractor import extract_token
from .video.config import device_config_from_vip

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult
else:
    ConfigFlowResult = FlowResult

_LOGGER = logging.getLogger(__name__)

PORT_SELECTOR = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): PORT_SELECTOR,
        vol.Optional(CONF_TOKEN): str,
    }
)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


def _device_id(config: dict[str, Any]) -> str:
    """Build an address-based identity that survives DHCP changes."""
    vip = config.get("vip")
    if not isinstance(vip, dict):
        raise CannotConnect("Device configuration has no VIP apartment address")
    device_config = device_config_from_vip(vip)
    if not device_config.apt_address:
        raise CannotConnect("Device configuration has no VIP apartment address")
    return f"{device_config.apt_address}:{device_config.apt_subaddress}"


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate credentials and return normalized device information."""
    del hass
    host = data[CONF_HOST].strip()
    port = data.get(CONF_PORT, DEFAULT_PORT)
    token = data.get(CONF_TOKEN)

    if not token:
        try:
            token = await asyncio.wait_for(extract_token(host), timeout=30.0)
        except TimeoutError:
            _LOGGER.error("Token extraction timed out for %s", host)
            token = None
        except Exception:
            _LOGGER.warning("Token extraction failed for %s", host, exc_info=True)
            token = None
        if not token:
            raise InvalidAuth("automatic_token_extraction_failed")

    client = IconaBridgeClient(host, port)
    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=10.0)
        except TimeoutError as err:
            raise CannotConnect("Connection timed out") from err
        except (ConnectionError, OSError) as err:
            raise CannotConnect(str(err)) from err

        auth_code = await asyncio.wait_for(client.authenticate(token), timeout=15.0)
        if auth_code != 200:
            raise InvalidAuth(f"Authentication failed with code {auth_code}")

        config = await asyncio.wait_for(client.get_config("all"), timeout=15.0)
        if not isinstance(config, dict) or not config:
            raise CannotConnect("Device returned no configuration")
        return {
            "title": f"Comelit Intercom ({host})",
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_TOKEN: token,
            CONF_DEVICE_ID: _device_id(config),
        }
    except TimeoutError as err:
        raise CannotConnect("Device communication timed out") from err
    except (CannotConnect, InvalidAuth):
        raise
    except Exception as err:
        _LOGGER.exception("Unexpected error validating %s", host)
        raise CannotConnect(str(err)) from err
    finally:
        await client.shutdown()


def _error_key(err: Exception) -> str:
    """Translate validation exceptions to config-flow error keys."""
    if isinstance(err, InvalidAuth):
        if str(err) == "automatic_token_extraction_failed":
            return "auto_token_failed"
        return "invalid_auth"
    if isinstance(err, CannotConnect):
        return "cannot_connect"
    return "unknown"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Comelit."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect a new Comelit device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except Exception as err:  # noqa: BLE001 - mapped to flow errors
                errors["base"] = _error_key(err)
                if errors["base"] == "unknown":
                    _LOGGER.exception("Unexpected config-flow exception")
            else:
                await self.async_set_unique_id(info[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                if any(
                    entry.data.get(CONF_DEVICE_ID) == info[CONF_DEVICE_ID]
                    for entry in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=info.pop("title"),
                    data=info,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after a rejected token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and replace an expired token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = dict(entry.data)
            candidate[CONF_TOKEN] = user_input.get(CONF_TOKEN, "")
            try:
                info = await validate_input(self.hass, candidate)
            except Exception as err:  # noqa: BLE001 - mapped to flow errors
                errors["base"] = _error_key(err)
            else:
                known_id = entry.data.get(CONF_DEVICE_ID)
                if known_id and info[CONF_DEVICE_ID] != known_id:
                    errors["base"] = "wrong_device"
                else:
                    title = info.pop("title")
                    return self.async_update_reload_and_abort(
                        entry,
                        title=title,
                        data_updates=info,
                        reason="reauth_successful",
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_TOKEN): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the network location while preserving device identity."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(
                    CONF_PORT,
                    default=entry.data.get(CONF_PORT, DEFAULT_PORT),
                ): PORT_SELECTOR,
            }
        )
        if user_input is not None:
            candidate = {**entry.data, **user_input}
            try:
                info = await validate_input(self.hass, candidate)
            except Exception as err:  # noqa: BLE001 - mapped to flow errors
                errors["base"] = _error_key(err)
            else:
                known_id = entry.data.get(CONF_DEVICE_ID)
                if known_id and info[CONF_DEVICE_ID] != known_id:
                    errors["base"] = "wrong_device"
                else:
                    title = info.pop("title")
                    return self.async_update_reload_and_abort(
                        entry,
                        title=title,
                        data_updates=info,
                        reason="reconfigure_successful",
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )
