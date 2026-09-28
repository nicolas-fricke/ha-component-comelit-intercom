"""Push notification listener via the PUSH channel."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from .channels import ChannelType, ViperMessageId
from .client import IconaBridgeClient
from .models import DeviceConfig, PushEvent

_LOGGER = logging.getLogger(__name__)

BUNDLE_ID = "com.comelitgroup.friendhome"
PROFILE_ID = "3"
DEVICE_TOKEN = "comelit-local-ha-integration"


async def register_push(
    client: IconaBridgeClient,
    config: DeviceConfig,
    callback: Callable[[PushEvent], None],
) -> None:
    """Register for push notifications and wire up the callback.

    Opens the PUSH channel, sends a registration message, and sets up
    the client's push callback to parse incoming events.
    """
    channel = await client.open_channel("PUSH", ChannelType.PUSH)

    msg = {
        "apt-address": config.apt_address,
        "apt-subaddress": config.apt_subaddress,
        "bundle-id": BUNDLE_ID,
        "message": "push-info",
        "message-id": int(ViperMessageId.PUSH),
        "os-type": "ios",
        "profile-id": PROFILE_ID,
        "device-token": DEVICE_TOKEN,
        "message-type": "request",
    }

    response = await client.send_json(channel, msg)
    _LOGGER.debug("Push registration response: %s", response)

    def _on_push(raw_msg: dict[str, Any]) -> None:
        event = _parse_push_event(raw_msg)
        if event:
            callback(event)

    client.set_push_callback(_on_push)
    _LOGGER.info("Push notifications registered")


async def send_push_keepalive(
    client: IconaBridgeClient,
    config: DeviceConfig,
) -> None:
    """Re-send push-info registration on the existing PUSH channel as a keepalive probe.

    The device responds to push-info with a JSON acknowledgement, which causes
    a packet to arrive on the TCP connection and resets the 120s receive-loop
    idle timer.  If the device is unreachable (gone to sleep, half-open socket)
    the underlying send_json call will raise — the caller should handle that as
    a dead connection.
    """
    channel = client.get_channel("PUSH")
    if channel is None:
        raise RuntimeError("PUSH channel not open")

    msg = {
        "apt-address": config.apt_address,
        "apt-subaddress": config.apt_subaddress,
        "bundle-id": BUNDLE_ID,
        "message": "push-info",
        "message-id": int(ViperMessageId.PUSH),
        "os-type": "ios",
        "profile-id": PROFILE_ID,
        "device-token": DEVICE_TOKEN,
        "message-type": "request",
    }
    await client.send_json(channel, msg)
    _LOGGER.debug("Push keepalive sent")


def _parse_push_event(raw: dict[str, Any]) -> PushEvent | None:
    """Parse a raw push notification JSON into a PushEvent.

    The exact format varies by firmware version. We look for common patterns
    and log unknown messages for future analysis.
    """
    msg_type = raw.get("message", "")

    # Known event types from community reverse-engineering
    if msg_type in ("incoming-call", "push-incoming-call"):
        return PushEvent(
            event_type="ring",
            apt_address=raw.get("apt-address", ""),
            timestamp=time.time(),
            raw=raw,
        )

    if msg_type in ("missed-call", "push-missed-call"):
        return PushEvent(
            event_type="missed_call",
            apt_address=raw.get("apt-address", ""),
            timestamp=time.time(),
            raw=raw,
        )

    # Log unknown messages for discovery
    _LOGGER.warning("Unknown push message type %r: %s", msg_type, raw)
    return None
