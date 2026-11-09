"""Authentication flow via the UAUT channel."""

from __future__ import annotations

import logging

from .channels import ChannelType, ViperMessageId
from .client import IconaBridgeClient
from .const import is_verbose_logging
from .exceptions import AuthenticationError

_LOGGER = logging.getLogger(__name__)


async def authenticate(client: IconaBridgeClient, token: str) -> None:
    """Authenticate with the device using a user token.

    Opens the UAUT channel, sends an access request, and verifies response code 200.
    """
    channel = await client.open_channel("UAUT", ChannelType.UAUT)

    msg = {
        "message": "access",
        "user-token": token,
        "message-type": "request",
        "message-id": int(ViperMessageId.UAUT),
    }

    response = await client.send_json(channel, msg)
    if is_verbose_logging():
        _LOGGER.debug("Auth response: %s", response)

    code = response.get("response-code", 0)
    if code != 200:
        reason = response.get("response-string", "Unknown error")
        raise AuthenticationError(f"Authentication failed: {code} {reason}")

    if is_verbose_logging():
        _LOGGER.info("Authenticated successfully")
