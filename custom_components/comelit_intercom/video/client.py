"""AsyncIO TCP client for the ICONA Bridge protocol."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from .channels import Channel, ChannelType
from .const import is_verbose_logging
from .exceptions import ConnectionComelitError, ProtocolError
from .protocol import (
    HEADER_SIZE,
    ICONA_BRIDGE_PORT,
    decode_header,
    decode_json_body,
    encode_channel_close,
    encode_channel_open,
    encode_channel_open_response,
    encode_header,
    encode_json_message,
    is_json_body,
    parse_command_response,
)

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
CLOSE_TIMEOUT = 1.5
_SENSITIVE_JSON_KEYS = frozenset({"user-token", "password"})


def _redact_json_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a log-safe copy of a JSON protocol message."""
    return {
        key: "<redacted>" if key in _SENSITIVE_JSON_KEYS else value
        for key, value in msg.items()
    }


def _format_packet_for_log(data: bytes) -> str:
    """Format a packet without exposing authentication credentials."""
    body = data[HEADER_SIZE:] if len(data) >= HEADER_SIZE else b""
    if is_json_body(body) and any(
        f'"{key}"'.encode() in body for key in _SENSITIVE_JSON_KEYS
    ):
        return f"{data[:HEADER_SIZE].hex(' ')} <redacted sensitive JSON payload>"
    return data.hex(" ")


class IconaBridgeClient:
    """Async TCP client for communicating with a Comelit ICONA Bridge device."""

    def __init__(self, host: str, port: int = ICONA_BRIDGE_PORT) -> None:
        """Initialize the client."""
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 8000 + int(asyncio.get_event_loop().time() * 10) % 1000
        self._sequence = 0
        self._channels: dict[str, Channel] = {}
        self._receive_task: asyncio.Task[None] | None = None
        self._callbacks: dict[int, asyncio.Future[bytes]] = {}
        self._close_waiters: dict[int, asyncio.Future[None]] = {}
        self._push_callback: Callable[[dict[str, Any]], None] | None = None
        self._connected = False
        self._disconnect_callback: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        """Return True if the TCP connection is active."""
        return self._connected

    async def connect(self) -> None:
        """Open TCP connection to the device."""

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
        except (OSError, TimeoutError) as e:
            raise ConnectionComelitError(
                f"Failed to connect to {self.host}:{self.port}: {e}"
            ) from e

        # Enable TCP keepalives so the OS detects when the device goes to sleep
        # without sending a FIN. Without this, the connection appears alive
        # indefinitely and we never reconnect — missing doorbell ring events.
        assert self._writer is not None
        sock = self._writer.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Start probing after 60s idle, retry every 10s, drop after 3 failed
            # probes — detects a dead connection in ~90s total.
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

        self._connected = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        _LOGGER.debug("Connected to %s:%s", self.host, self.port)

    async def disconnect(self) -> None:
        """Close the TCP connection.

        The receive task is cancelled with a 2s timeout to allow orderly
        shutdown without risking a 30-40s hang on a dead socket.
        """
        self._connected = False
        if self._receive_task:
            task, self._receive_task = self._receive_task, None
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait([task], timeout=2.0)
        if self._writer:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None
        self._channels.clear()
        # Cancel any pending response futures
        for response_future in self._callbacks.values():
            if not response_future.done():
                response_future.cancel()
        self._callbacks.clear()
        for close_future in self._close_waiters.values():
            if not close_future.done():
                close_future.cancel()
        self._close_waiters.clear()
        _LOGGER.debug("Disconnected from %s:%s", self.host, self.port)

    async def _send(self, data: bytes) -> None:
        """Send raw bytes to the device."""
        if not self._writer:
            raise ConnectionComelitError("Not connected")
        if is_verbose_logging():
            _LOGGER.debug(
                "Writing %d bytes: %s", len(data), _format_packet_for_log(data)
            )
        self._writer.write(data)
        try:
            await self._writer.drain()
        except (OSError, ConnectionError) as e:
            raise ConnectionComelitError(f"Send failed: {e}") from e

    async def _read_packet(self) -> tuple[int, bytes]:
        """Read one full packet. Returns (request_id, body)."""
        if not self._reader:
            raise ConnectionComelitError("Not connected")
        header = await self._reader.readexactly(HEADER_SIZE)
        body_length, request_id = decode_header(header)
        if is_verbose_logging():
            _LOGGER.debug(
                "Read header: %s (body_length=%d, request_id=%d)",
                header.hex(" "),
                body_length,
                request_id,
            )
        body = await self._reader.readexactly(body_length) if body_length > 0 else b""
        if is_verbose_logging():
            if is_json_body(body):
                _LOGGER.debug(
                    "Read JSON body (%d bytes): %s",
                    len(body),
                    body.decode("utf-8", errors="replace")[:500],
                )
            else:
                _LOGGER.debug(
                    "Read binary body (%d bytes): %s", len(body), body.hex(" ")[:200]
                )
        return request_id, body

    def set_disconnect_callback(self, callback: Callable[[], None] | None) -> None:
        """Register a callback invoked when the TCP connection drops unexpectedly.

        Called from the receive loop on EOF, 120s timeout, or unhandled error.
        Not called on deliberate shutdown (CancelledError / disconnect()).
        """
        self._disconnect_callback = callback

    async def _receive_loop(self) -> None:
        """Background task that reads packets and dispatches them.

        Uses a 120s read timeout as an application-level dead-connection
        detector. TCP keepalive socket options are unreliable inside Docker
        containers, so we can't rely on the OS to close the socket when the
        device goes to sleep without sending a FIN.
        """
        _LOGGER.debug("Receive loop started")
        unexpected = False
        try:
            while self._connected:
                if is_verbose_logging():
                    _LOGGER.debug("Waiting for next packet...")
                try:
                    request_id, body = await asyncio.wait_for(
                        self._read_packet(), timeout=120.0
                    )
                except TimeoutError:
                    _LOGGER.warning(
                        "No data received for 120s — marking connection dead"
                    )
                    self._connected = False
                    unexpected = True
                    break
                self._dispatch(request_id, body)
        except asyncio.IncompleteReadError:
            _LOGGER.info("Connection closed by device")
            self._connected = False
            unexpected = True
        except (OSError, ConnectionComelitError) as err:
            _LOGGER.info("Comelit transport connection lost: %s", err)
            self._connected = False
            unexpected = True
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error in receive loop")
            self._connected = False
            unexpected = True

        if unexpected and self._disconnect_callback:
            self._disconnect_callback()

    def _dispatch(self, request_id: int, body: bytes) -> None:
        """Dispatch a received packet to the appropriate handler."""
        if request_id == 0:
            self._dispatch_command_response(body)
            return

        # Some firmware acknowledges a locally initiated END on the channel's
        # request ID instead of request_id=0. Route that control response before
        # normal channel data so close_channel() can complete its release barrier.
        if len(body) >= 4 and struct.unpack_from("<H", body, 0)[0] == 0x01EF:
            self._dispatch_command_response(body, request_id=request_id)
            return

        # Data response — check if there's a waiting future (for send_json)
        if request_id in self._callbacks:
            _LOGGER.debug("Matched callback for request_id=%d", request_id)
            future = self._callbacks.pop(request_id)
            if not future.done():
                future.set_result(body)
            return

        # Queue response on the matching channel (for read_response)
        for ch in self._channels.values():
            if ch.server_channel_id == request_id and ch.is_open:
                ch.response_queue.put_nowait(body)
                if is_json_body(body):
                    _LOGGER.debug("Queued JSON on %s (%d bytes)", ch.name, len(body))
                elif is_verbose_logging():
                    _LOGGER.debug("Queued binary on %s (%d bytes)", ch.name, len(body))
                return

        # Check for push notification or unsolicited message
        if is_json_body(body):
            try:
                msg = decode_json_body(body)
                _LOGGER.debug("Unsolicited JSON on channel %d: %s", request_id, msg)
                if self._push_callback:
                    self._push_callback(msg)
            except Exception:
                _LOGGER.debug(
                    "Failed to decode unsolicited body on channel %d", request_id
                )
        else:
            _LOGGER.debug(
                "Unsolicited binary on channel %d, %d bytes", request_id, len(body)
            )

    def _dispatch_command_response(self, body: bytes, request_id: int = 0) -> None:
        """Handle a command/control packet from the device."""
        if len(body) >= 4:
            msg_type, seq, server_ch_id = parse_command_response(body)
            if msg_type == 0x01EF and server_ch_id == 0:
                server_ch_id = request_id
            _LOGGER.debug(
                "Command response: type=0x%04X seq=%d ch_id=%d",
                msg_type,
                seq,
                server_ch_id,
            )

            if msg_type == 0xABCD and seq == 1 and len(body) >= 10:
                # Device-initiated channel open (seq=1). Parse the
                # device's request_id from the body and respond with a
                # COMMAND response so the device knows we accepted it.
                # Body: [cdab] [seq=1 LE16] [type LE32] [name...] [req_id LE16] [trailing]
                # The request_id is near the end — find it after the channel name.
                name_start = 8
                try:
                    name_end = body.index(0, name_start)
                    dev_req_id = struct.unpack_from("<H", body, name_end + 1)[0]
                except (ValueError, struct.error):
                    # No null terminator — name runs to end minus 3 bytes
                    dev_req_id = struct.unpack_from("<H", body, len(body) - 3)[0]
                _LOGGER.debug(
                    "Device channel open: dev_req_id=0x%04X",
                    dev_req_id,
                )
                # Send COMMAND response back to device
                resp_pkt = encode_channel_open_response(dev_req_id)
                if self._writer:
                    self._writer.write(resp_pkt)
                    # drain happens asynchronously — fire and forget is OK here
                # Assign to placeholder channel if one exists
                for ch in self._channels.values():
                    if (
                        not ch.is_open
                        and ch.server_channel_id == 0
                        and ch.request_id == 0
                    ):
                        ch.server_channel_id = dev_req_id
                        ch.is_open = True
                        ch.sequence = 3
                        ch.open_response_body = body
                        ch.open_event.set()
                        _LOGGER.debug(
                            "Placeholder %s assigned dev_req_id=0x%04X",
                            ch.name,
                            dev_req_id,
                        )
                        break
                return

            # Regular command response (seq >= 2) — assign server_channel_id
            # to the first pending channel open. Only for COMMAND (0xABCD),
            # not END (0x01EF) or other message types.
            if msg_type == 0xABCD:
                for ch in self._channels.values():
                    if (
                        not ch.is_open
                        and ch.server_channel_id == 0
                        and ch.request_id != 0
                    ):
                        ch.server_channel_id = server_ch_id
                        ch.is_open = True
                        ch.sequence = seq + 1
                        ch.open_response_body = body
                        ch.open_event.set()
                        _LOGGER.debug(
                            "Channel %s assigned id=%d", ch.name, server_ch_id
                        )
                        break
            elif msg_type == 0x01EF:
                # Device-initiated channel close (END type, sub_type=2 in bytes 4-7).
                # Must ACK with type=4 response so device can re-open the channel.
                # PCAP-verified: app sends ef01 0400 04000000 [ch_id LE16] 0000.
                sub_type = struct.unpack_from("<I", body, 4)[0] if len(body) >= 8 else 0
                if sub_type == 2 and server_ch_id:
                    ack_body = (
                        struct.pack("<HH", 0x01EF, 4)  # END magic + seq=4
                        + struct.pack("<I", 4)  # sub_type=4 (close ACK)
                        + struct.pack("<H", server_ch_id)  # channel being closed
                        + b"\x00\x00"  # padding
                    )
                    ack_pkt = (
                        b"\x00\x06"
                        + struct.pack("<H", len(ack_body))
                        + b"\x00\x00\x00\x00"
                        + ack_body
                    )
                    if self._writer:
                        self._writer.write(ack_pkt)
                    _LOGGER.debug(
                        "Sent close ACK for ch=0x%04X (device-initiated END)",
                        server_ch_id,
                    )
                else:
                    _LOGGER.debug(
                        "Device ACKed our close: ch=0x%04X sub_type=%d",
                        server_ch_id,
                        sub_type,
                    )
                if server_ch_id:
                    self._mark_channel_closed(server_ch_id)
            else:
                _LOGGER.debug(
                    "Non-COMMAND message type=0x%04X (not assigning)", msg_type
                )

    def _next_request_id(self) -> int:
        """Return the next request ID for a channel open packet."""
        self._request_id += 1
        return self._request_id

    def _next_sequence(self) -> int:
        """Return the next sequence number, advancing by 2 (client uses even numbers)."""
        self._sequence += 1
        return self._sequence

    async def open_channel(
        self,
        name: str,
        channel_type: ChannelType,
        extra_data: str | None = None,
        trailing_byte: int = 0,
        wire_name: str | None = None,
    ) -> Channel:
        """Open a named channel. Returns Channel with server-assigned ID.

        Args:
            name: Internal key for tracking this channel.
            wire_name: Protocol name sent on the wire (defaults to name).
        """
        protocol_name = wire_name or name
        request_id = self._next_request_id()
        seq = 1  # Device expects sequence=1 for all channel opens
        _LOGGER.debug(
            "Opening channel %s (wire=%s): type=%d, request_id=%d, seq=%d, extra=%s",
            name,
            protocol_name,
            int(channel_type),
            request_id,
            seq,
            extra_data,
        )
        channel = Channel(
            name=name,
            channel_type=channel_type,
            request_id=request_id,
        )
        self._channels[name] = channel

        packet = encode_channel_open(
            protocol_name, channel_type, seq, request_id, extra_data, trailing_byte
        )
        await self._send(packet)

        # Wait for the channel to be opened by the received loop
        try:
            await asyncio.wait_for(channel.open_event.wait(), timeout=READ_TIMEOUT)
        except TimeoutError:
            raise ProtocolError(f"Timeout waiting for channel {name} to open") from None

        return channel

    def _mark_channel_closed(self, server_channel_id: int) -> None:
        """Resolve a close waiter and forget the matching local channel."""
        waiter = self._close_waiters.get(server_channel_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        for name, channel in list(self._channels.items()):
            if channel.server_channel_id == server_channel_id:
                channel.is_open = False
                self._channels.pop(name, None)

    async def close_channel(self, name: str) -> bool:
        """Close a named channel.

        Sends an END packet to the device using the channel's server_channel_id
        as request_id and waits briefly for the matching END response. Returning
        False tells the coordinator to reset the shared TCP connection before a
        new call is allowed to start.
        """
        channel = self._channels.pop(name, None)
        if channel is None:
            return True
        channel.is_open = False
        server_channel_id = channel.server_channel_id
        if server_channel_id == 0:
            return True

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._close_waiters[server_channel_id] = waiter
        try:
            # Channel opens use seq=1 and the device replies with seq=2, so the
            # app's END must use this channel's next sequence (normally 3).
            await self._send(
                encode_channel_close(channel.next_sequence(), server_channel_id)
            )
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await waiter
            except TimeoutError:
                if name in {"UDPM", "RTPC_DEVICE"}:
                    _LOGGER.debug(
                        "No %s channel close ACK; connection reset will finish "
                        "cleanup (ch=0x%04X)",
                        name,
                        server_channel_id,
                    )
                else:
                    _LOGGER.warning(
                        "Timed out waiting for %s channel close ACK (ch=0x%04X)",
                        name,
                        server_channel_id,
                    )
                return False
            _LOGGER.debug("Closed channel %s (ch=0x%04X)", name, server_channel_id)
            return True
        except Exception:
            _LOGGER.debug("Failed to close channel %s", name, exc_info=True)
            return False
        finally:
            self._close_waiters.pop(server_channel_id, None)

    async def send_json(self, channel: Channel, msg: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON message on a channel and wait for JSON response.

        Uses a per-channel lock so concurrent callers are serialized — the
        device always responds with server_channel_id, which can only map to
        one pending callback at a time.
        """
        if not channel.is_open or channel.server_channel_id == 0:
            raise ProtocolError(f"Channel {channel.name} not open")

        async with channel.send_lock:
            _LOGGER.debug(
                "send_json on %s (server_channel_id=%d): %s",
                channel.name,
                channel.server_channel_id,
                _redact_json_message(msg),
            )

            loop = asyncio.get_running_loop()
            future: asyncio.Future[bytes] = loop.create_future()
            self._callbacks[channel.server_channel_id] = future

            packet = encode_json_message(msg, channel.server_channel_id)
            await self._send(packet)

            try:
                body = await asyncio.wait_for(future, timeout=READ_TIMEOUT)
            except TimeoutError:
                _LOGGER.error(
                    "Timeout on %s (server_channel_id=%d), pending_callbacks=%s",
                    channel.name,
                    channel.server_channel_id,
                    list(self._callbacks.keys()),
                )
                self._callbacks.pop(channel.server_channel_id, None)
                raise ProtocolError(
                    f"Timeout waiting for response on {channel.name}"
                ) from None

        if is_json_body(body):
            return decode_json_body(body)
        raise ProtocolError(f"Expected JSON response on {channel.name}, got binary")

    async def send_binary(self, channel: Channel, data: bytes) -> None:
        """Send a binary payload on a channel (used for door open commands)."""
        if not channel.is_open or channel.server_channel_id == 0:
            raise ProtocolError(f"Channel {channel.name} not open")
        packet = encode_header(len(data), channel.server_channel_id) + data
        await self._send(packet)

    async def read_response(self, channel: Channel) -> bytes:
        """Wait for a response on a specific channel.

        Uses the channel's response queue. The receive loop queues incoming
        binary packets per channel, so there's no race condition — packets
        that arrive before this method is called are buffered in the queue.
        Callers manage their own timeout via asyncio.timeout().
        """
        return await channel.response_queue.get()

    def register_placeholder_channel(self, name: str) -> Channel:
        """Register a placeholder for a device-initiated channel open.

        Used when the device creates a channel (e.g., the media RTPC channel
        after the RTPC link message). The dispatch logic will assign the next
        COMMAND response to this placeholder.
        """
        channel = Channel(name=name, channel_type=ChannelType.UAUT, request_id=0)
        self._channels[name] = channel
        return channel

    def release_placeholder_channel(self, name: str) -> None:
        """Remove an unassigned placeholder without sending a close packet.

        Call this when a placeholder times out so it doesn't steal future
        device-initiated channel opens (e.g. re-establishment RTPC channels).
        """
        self._channels.pop(name, None)

    def remove_channel(self, name: str) -> None:
        """Remove a channel from the registry without sending a close packet.

        Used by VideoCallSession to clean up its channels when stopping,
        without disconnecting the shared client.
        """
        self._channels.pop(name, None)

    def rename_channel(self, old_name: str, new_name: str) -> None:
        """Rename a channel in the registry without sending any packets.

        Preserves the server_channel_id so the device keeps routing responses
        to the same channel (now under a new local key). Used by the coordinator
        to transfer CTPP_VIP ownership to the video session.
        """
        if old_name in self._channels:
            channel = self._channels.pop(old_name)
            channel.name = new_name
            self._channels[new_name] = channel

    def get_channel(self, name: str) -> Channel | None:
        """Return an open channel by name, or None if not open."""
        ch = self._channels.get(name)
        return ch if ch is not None and ch.is_open else None

    def set_push_callback(
        self, callback: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Set a callback for push notifications (unsolicited JSON messages)."""
        self._push_callback = callback
