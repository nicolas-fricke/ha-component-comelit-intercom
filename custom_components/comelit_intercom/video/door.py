"""Door open sequences via the shared CTPP channel.

Single public entry point: open_door — reuses an existing CTPP channel when
one is open (VIP listener ON / video active), otherwise opens a transient one.

Per-door sequence:
    regular door:  OPEN + CONFIRM  →  door_init + drain 2 resps  →  OPEN + CONFIRM
    actuator:      actuator_init + drain 2 resps  →  actuator_open + actuator_confirm

The during-call door-open path lives in video_call.py (single 0x1840/0x000D
message on the video CTPP channel) and is NOT used here.
"""

from __future__ import annotations

import asyncio
import logging

from .auth import authenticate
from .channels import Channel, ChannelType
from .client import IconaBridgeClient
from .const import DOMAIN
from .ctpp import ctpp_init_sequence
from .exceptions import DoorOpenError
from .models import DeviceConfig, Door
from .protocol import (
    MessageType,
    encode_actuator_init,
    encode_actuator_open,
    encode_door_init,
    encode_open_door,
)

_LOGGER = logging.getLogger(__name__)

# Timeout for the per-door (door_init / actuator_init).
DOOR_TIMEOUT = 2.0


async def open_door(
    host: str,
    port: int,
    token: str,
    client: IconaBridgeClient,
    config: DeviceConfig,
    door: Door,
) -> None:
    opened_channel = False
    try:
        ctpp = client.get_channel("CTPP")
        opened_channel = ctpp is None
        if ctpp is None:
            client = IconaBridgeClient(host, port)
            await client.connect()
            await authenticate(client, token)
            ctpp = await open_ctpp_channel(client, config)
        await _open_door_on_channel(client, ctpp, config.apt_address, door)
        extra_message = "(regular path)"
        if opened_channel is False:
            extra_message = "(fast path)"
        _LOGGER.info("Door '%s' opened successfully " + extra_message, door.name)
    except Exception as e:
        raise DoorOpenError(
            translation_domain=DOMAIN,
            translation_key="door_open_failed",
            translation_placeholders={"door": door.name},
        ) from e
    finally:
        if opened_channel:
            client.remove_channel("CTPP")
            await client.disconnect()


async def open_ctpp_channel(
    client: IconaBridgeClient,
    config: DeviceConfig,
) -> Channel:
    """Open a transient CTPP_DOOR channel and run ctpp_init_sequence.

    Used when no CTPP channel is currently open (notifications OFF, no active
    video). Caller is responsible for removing the channel when done.
    """
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress
    our_addr = f"{apt_addr}{apt_sub}"

    try:
        channel = await client.open_channel(
            "CTPP", ChannelType.CTPP, extra_data=our_addr
        )
        await ctpp_init_sequence(
            client,
            channel,
            apt_addr,
            apt_sub,
            our_addr,
            None,
            response_timeout=DOOR_TIMEOUT,
            send_ack=False,
        )
        return channel
    except Exception as e:
        raise DoorOpenError(f"Failed to open door: {e}") from e


async def _open_door_on_channel(
    client: IconaBridgeClient, channel: Channel, apt_addr: str, door: Door
) -> None:
    """Regular-door open sequence on an already-initialized CTPP channel.

    OPEN + CONFIRM  →  door_init + drain 2 resps  →  OPEN + CONFIRM.
    """
    # Phase B: Open door + confirm
    init_open = encode_actuator_init(apt_addr, door.output_index, door.apt_address)
    if door.is_actuator is False:
        await _send_open_and_confirm(client, channel, apt_addr, door)
        init_open = encode_door_init(apt_addr, door.output_index, door.apt_address)
    # Phase C: Door-specific init
    await client.send_binary(channel, init_open)
    for i in range(2):
        try:
            async with asyncio.timeout(DOOR_TIMEOUT):
                resp = await client.read_response(channel)
            _LOGGER.debug("door_init resp %d: %s", i + 1, resp.hex())
        except TimeoutError:
            _LOGGER.debug("door_init resp %d: timeout", i + 1)

    # Phase D: Open door + confirm again
    if door.is_actuator is False:
        await _send_open_and_confirm(client, channel, apt_addr, door)
    else:
        await _send_open_and_confirm_for_actuator(client, channel, apt_addr, door)


async def _send_open_and_confirm(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    door: Door,
) -> None:
    """Send OPEN_DOOR followed by OPEN_DOOR_CONFIRM."""
    await client.send_binary(
        channel,
        encode_open_door(
            MessageType.OPEN_DOOR, apt_addr, door.output_index, door.apt_address
        ),
    )
    await client.send_binary(
        channel,
        encode_open_door(
            MessageType.OPEN_DOOR_CONFIRM, apt_addr, door.output_index, door.apt_address
        ),
    )


async def _send_open_and_confirm_for_actuator(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    door: Door,
) -> None:
    open_payload = encode_actuator_open(
        apt_addr, door.output_index, door.apt_address, confirm=False
    )
    await client.send_binary(channel, open_payload)
    confirm_payload = encode_actuator_open(
        apt_addr, door.output_index, door.apt_address, confirm=True
    )
    await client.send_binary(channel, confirm_payload)
