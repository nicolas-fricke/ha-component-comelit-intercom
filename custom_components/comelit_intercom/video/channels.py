"""Channel definitions for the ICONA Bridge protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum


class ChannelType(IntEnum):
    """Channel type IDs used in binary COMMAND packets when opening a channel."""

    UAUT = 7
    UCFG = 2
    INFO = 20
    CTPP = 16
    CSPB = 17
    # PUSH uses the same wire type ID as UCFG (2). The device distinguishes
    # channel purpose by the channel name string, not the type ID.
    PUSH = 2


class ViperMessageId(IntEnum):
    """Message IDs used in JSON message-id fields. Different from ChannelType!"""

    UAUT = 2
    UCFG = 3
    SERVER_INFO = 20
    # PUSH uses the same wire message-id as UAUT (2) per the reverse-engineered
    # protocol; the message type is differentiated by the "message" field.
    PUSH = 2


@dataclass
class Channel:
    """Tracks state of an open channel."""

    name: str
    channel_type: ChannelType
    request_id: int
    server_channel_id: int = 0
    sequence: int = 1
    is_open: bool = False
    open_event: asyncio.Event = field(default_factory=asyncio.Event)
    open_response_body: bytes = b""
    response_queue: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_sequence(self) -> int:
        """Return the next sequence number and advance by 2 (client uses even, device odd)."""
        seq = self.sequence
        self.sequence += 2
        return seq
