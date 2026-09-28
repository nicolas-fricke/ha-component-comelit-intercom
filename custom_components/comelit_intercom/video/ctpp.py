"""CTPP channel helpers: shared init/handshake sequence.

All CTPP consumers (VIP listener, video session, standalone door open) use
ctpp_init_sequence() so the registration handshake is implemented exactly once.
"""

from __future__ import annotations

import asyncio
import logging
import struct

from .channels import Channel
from .client import IconaBridgeClient
from .protocol import encode_call_response_ack, encode_ctpp_init

_LOGGER = logging.getLogger(__name__)

# Both sub-counters increment by 1 (bytes[4] and bytes[5] of the CTPP body).
# Used to compute the ACK timestamp offset from the init timestamp.
# Value matches PCAP-verified video session analysis (_CTR_INCR_BOTH in video_call.py).
_CTR_INCR_BOTH = 0x01010000

# Minimum response length: prefix(2) + timestamp(4) + action(2) = 8 bytes.
_CTPP_RESPONSE_MIN_LEN = 8


async def ctpp_init_sequence(
    client: IconaBridgeClient,
    channel: Channel,
    apt_addr: str,
    apt_sub: int,
    our_addr: str,
    timestamp: int | None = None,
    response_timeout: float = 5.0,
    send_ack: bool = True,
) -> None:
    """CTPP handshake: init → drain 2 responses → optionally send ACK pair.

    The ACK pair (0x1800 + 0x1820) is required for VIP listener and video
    sessions but must NOT be sent for standalone door opens — the original
    door open flow never sent it.

    Args:
        client: the shared ICONA Bridge client.
        channel: the already-open CTPP channel.
        apt_addr: apartment address without subaddress (e.g. "SB000006").
        apt_sub: apartment subaddress integer (e.g. 1).
        our_addr: full address including subaddress (e.g. "SB0000061").
        timestamp: LE32 timestamp to embed in the init message.
        response_timeout: seconds to wait for each device response.
        send_ack: send the ACK pair after draining responses (default True).
    """
    init_payload = encode_ctpp_init(apt_addr, apt_sub, timestamp)
    await client.send_binary(channel, init_payload)

    await read_response_ctpp(client, channel, response_timeout)

    if send_ack:
        assert timestamp is not None, "timestamp required when send_ack=True"
        ack_ts = (timestamp + _CTR_INCR_BOTH) & 0xFFFFFFFF
        await client.send_binary(
            channel, encode_call_response_ack(our_addr, apt_addr, ack_ts)
        )
        await client.send_binary(
            channel, encode_call_response_ack(our_addr, apt_addr, ack_ts, prefix=0x1820)
        )
        _LOGGER.debug(
            "CTPP ACK pair sent (init_ts=0x%08X ack_ts=0x%08X)",
            timestamp,
            ack_ts,
        )


async def read_response_ctpp(
    client: IconaBridgeClient,
    channel: Channel,
    response_timeout: float = 5.0,
) -> None:
    # Drain device's two responses (0x1800 ACK + 0x1860/0x0010 renewal request).
    # We don't use the device's timestamp to compute our ACK — see docstring.
    for i in range(2):
        try:
            async with asyncio.timeout(response_timeout):
                resp = await client.read_response(channel)
        except TimeoutError:
            _LOGGER.debug("CTPP init response %d: no response (timeout)", i + 1)
            continue
        if len(resp) >= _CTPP_RESPONSE_MIN_LEN:
            prefix = struct.unpack_from("<H", resp, 0)[0]
            resp_ts = struct.unpack_from("<I", resp, 2)[0]
            action = struct.unpack_from(">H", resp, 6)[0]
            _LOGGER.debug(
                "CTPP init response %d: %d bytes, prefix=0x%04X ts=0x%08X action=0x%04X",
                i + 1,
                len(resp),
                prefix,
                resp_ts,
                action,
            )
