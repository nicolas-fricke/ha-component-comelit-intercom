"""Comelit ICONA Bridge client library."""

import asyncio
import json
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

# Protocol Constants
ICONA_BRIDGE_PORT = 64100  # TCP port for ICONA Bridge protocol
HEADER_MAGIC = b"\x00\x06"  # All messages start with these magic bytes
HEADER_SIZE = 8  # Fixed header size: magic(2) + length(2) + request_id(2) + padding(2)
NULL = b"\x00"


class MessageType(IntEnum):
    """Binary message types used in the protocol"""

    COMMAND = 0xABCD  # Opens a channel
    END = 0x01EF  # Closes a channel
    OPEN_DOOR_INIT = 0x18C0  # Initialize door opening sequence
    OPEN_DOOR = 0x1800  # Send open door command
    OPEN_DOOR_CONFIRM = 0x1820  # Confirm door opening


class ViperChannelType(IntEnum):
    """Channel type IDs for JSON messages"""

    SERVER_INFO = 20
    PUSH = 2
    UAUT = 2
    UCFG = 3
    CTPP = 7
    CSPB = 8


class Channel:
    """Channel names"""

    UAUT = "UAUT"
    UCFG = "UCFG"
    INFO = "INFO"
    CTPP = "CTPP"
    CSPB = "CSPB"
    PUSH = "PUSH"


@dataclass
class ChannelData:
    """Tracks open channel state"""

    channel: str
    id: int
    sequence: int


class IconaBridgeClient:
    """Python implementation of Comelit ICONA Bridge client"""

    def __init__(
        self,
        host: str,
        port: int = ICONA_BRIDGE_PORT,
        logger: logging.Logger | None = None,
    ):
        self.host = host
        self.port = port
        self.logger = logger or logging.getLogger(__name__)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.open_channels: dict[str, ChannelData] = {}
        # Start with a semi-random request ID to avoid conflicts
        # The device tracks requests by ID, so we need unique values
        self.request_id = 8000 + int(asyncio.get_event_loop().time() * 10) % 1000

    async def connect(self):
        """Connect to the ICONA Bridge"""
        self.logger.info(f"Connecting to {self.host}:{self.port}")

        try:
            # Try asyncio connection with timeout
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10.0
            )
            self.logger.info("Connected")

        except TimeoutError as e:
            self.logger.error(f"Connection timeout to {self.host}:{self.port}")
            raise ConnectionError(
                f"Connection timeout to {self.host}:{self.port}"
            ) from e
        except OSError as e:
            # Special handling for macOS "No route to host" error
            if hasattr(e, "errno") and e.errno == 65:
                self.logger.error(
                    f"Cannot reach {self.host}:{self.port} - macOS errno 65 (No route to host)"
                )
                self.logger.error(
                    "This is a known issue with Python 3.13 on macOS. The device may still be reachable."
                )
                # Try subprocess workaround if available
                if await self._test_nc_connection():
                    self.logger.warning(
                        "Device is reachable via nc but not Python socket. This is a Python bug."
                    )
                    raise ConnectionError(
                        "Python socket connection failed due to macOS bug. Device is reachable via other methods."
                    ) from e
                else:
                    raise ConnectionError(
                        f"Cannot reach {self.host}:{self.port}"
                    ) from e
            else:
                self.logger.error(
                    f"OS Error connecting to {self.host}:{self.port}: {e}"
                )
                raise
        except Exception as e:
            self.logger.error(f"Failed to connect to {self.host}:{self.port}: {e}")
            raise

    async def _test_nc_connection(self) -> bool:
        """Test if device is reachable via nc (netcat)"""
        try:
            import subprocess

            result = subprocess.run(
                ["nc", "-z", "-w", "2", self.host, str(self.port)], capture_output=True
            )
            return result.returncode == 0
        except Exception:
            return False

    async def shutdown(self):
        """Close the connection"""
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.logger.info("Connection closed")

    def _create_header(self, body_length: int, request_id: int = 0) -> bytes:
        """Create 8-byte message header

        Header structure:
        [0:2] Magic bytes (0x00 0x06)
        [2:4] Body length (little-endian uint16)
        [4:6] Request ID (little-endian uint16) - 0 for binary commands
        [6:8] Padding (always 0x00 0x00)
        """
        header = bytearray(8)
        header[0:2] = HEADER_MAGIC
        header[2:4] = struct.pack(
            "<H", body_length
        )  # '<H' = little-endian unsigned short
        header[4:6] = struct.pack("<H", request_id)
        header[6:8] = b"\x00\x00"  # Required padding
        return bytes(header)

    def _create_json_packet(self, request_id: int, data: dict) -> bytes:
        """Create a JSON message packet"""
        json_str = json.dumps(data, separators=(",", ":"))
        json_bytes = json_str.encode("utf-8")
        header = self._create_header(len(json_bytes), request_id)
        return header + json_bytes

    def _create_binary_packet_from_buffers(
        self, request_id: int, *buffers: bytes
    ) -> bytes:
        """Create a binary message packet from multiple buffers"""
        body = b"".join(buffers)
        header = self._create_header(len(body), request_id)
        return header + body

    def _create_command_packet(
        self,
        request_id: int,
        seq: int,
        msg_type: int,
        channel: str | None = None,
        additional_data: str | None = None,
    ) -> bytes:
        """Create a command packet (for opening/closing channels)"""
        # Start with message type and sequence
        body = struct.pack("<HH", msg_type, seq)

        # Add channel data if provided
        if channel:
            # Channel type mapping discovered through protocol analysis
            # These numeric IDs are what the device expects for each channel
            # Note: The ViperChannelType enum values don't match these!
            channel_type = {
                "UAUT": 7,  # Authentication channel
                "UCFG": 2,  # Configuration channel
                "INFO": 20,  # Server info channel
                "CTPP": 16,  # Control channel (door operations)
                "CSPB": 17,  # Unknown purpose
                "PUSH": 2,  # Push notifications
            }.get(channel, 0)

            body += struct.pack("<I", channel_type)  # Channel type as 4-byte int
            body += (
                channel.encode("ascii") + NULL
            )  # Channel name as ASCII + null terminator
            body += struct.pack(
                "<H", request_id
            )  # Request ID again (protocol requirement)
            body += NULL  # Final null terminator

        # Add additional data if provided
        if additional_data:
            add_bytes = additional_data.encode("ascii")
            body += struct.pack("<I", len(add_bytes) + 1)
            body += add_bytes + NULL

        header = self._create_header(
            len(body), 0
        )  # Request ID is 0 for command packets
        return header + body

    async def _write_packet(self, packet: bytes):
        """Write a packet to the socket"""
        self.logger.debug(f"Writing {len(packet)} bytes: {packet.hex(' ')}")
        self.writer.write(packet)
        await self.writer.drain()

    async def _read_response(self) -> dict | None:
        """Read and parse a response from the socket

        The device sends different response types based on the request ID:
        - request_id == 0: Binary protocol messages (channel operations)
        - request_id > 0: JSON or binary data responses
        """
        # Read the 8-byte header first
        header = await self.reader.readexactly(HEADER_SIZE)
        body_length = struct.unpack("<H", header[2:4])[0]
        request_id = struct.unpack("<H", header[4:6])[0]

        # Read body if present
        if body_length > 0:
            body = await self.reader.readexactly(body_length)
            self.logger.debug(f"Read {len(body)} bytes: {body.hex(' ')}")

            # Parse response based on request_id
            if request_id == 0:
                # Binary response - these are responses to channel operations
                msg_type = struct.unpack("<H", body[0:2])[0]
                sequence = struct.unpack("<H", body[2:4])[0]

                if msg_type == MessageType.COMMAND:
                    # Channel open response includes the channel ID assigned by the device
                    # This ID is crucial - we must use it for all subsequent operations on this channel
                    # Format: msg_type(2) + sequence(2) + value(4) + channel_id(2) + padding
                    channel_id = None
                    if len(body) >= 10:
                        channel_id = struct.unpack("<H", body[8:10])[0]

                    return {
                        "type": "binary",
                        "message_type": msg_type,
                        "sequence": sequence,
                        "channel_id": channel_id,  # This is the ID to use for this channel!
                        "request_id": request_id,
                    }
                elif msg_type == MessageType.END:
                    # Channel close acknowledgment
                    return {
                        "type": "binary",
                        "message_type": msg_type,
                        "sequence": sequence,
                        "request_id": request_id,
                    }
            else:
                # Non-zero request_id means this is a data response
                # Check first byte to determine if it's JSON
                if body[0] == 0x7B:  # '{' character indicates JSON
                    json_data = json.loads(body.decode("utf-8"))
                    return {"type": "json", "request_id": request_id, "data": json_data}
                else:
                    # Binary data response (e.g., from door operations)
                    return {
                        "type": "binary_data",
                        "request_id": request_id,
                        "data": body,
                    }

        return None

    async def _open_channel(
        self, channel: str, additional_data: str | None = None
    ) -> ChannelData:
        """Open a communication channel"""
        if channel in self.open_channels:
            return self.open_channels[channel]

        self.request_id += 1
        channel_data = ChannelData(channel=channel, id=self.request_id, sequence=1)

        # Send COMMAND to open channel
        packet = self._create_command_packet(
            self.request_id, 1, MessageType.COMMAND, channel, additional_data
        )
        await self._write_packet(packet)

        # Read response
        response = await self._read_response()
        if response and response["type"] == "binary" and response["sequence"] == 2:
            channel_data.sequence = response["sequence"]
            # IMPORTANT: Use the channel ID from the response, not our request ID!
            if "channel_id" in response and response["channel_id"]:
                channel_data.id = response["channel_id"]
                self.logger.debug(
                    f"Channel {channel} opened with server ID {response['channel_id']} (our request ID was {self.request_id})"
                )
            self.open_channels[channel] = channel_data
            return channel_data
        else:
            raise Exception(f"Failed to open channel {channel}")

    async def _close_channel(self, channel_data: ChannelData) -> bool:
        """Close a communication channel"""
        channel_data.sequence += 1
        packet = self._create_command_packet(
            channel_data.id, channel_data.sequence, MessageType.END
        )
        await self._write_packet(packet)

        response = await self._read_response()
        if response and response["type"] == "binary":
            del self.open_channels[channel_data.channel]
            self.logger.debug(f"Closed channel {channel_data.channel}")
            return True
        return False

    async def authenticate(self, token: str) -> int:
        """Authenticate with the ICONA Bridge

        Returns:
            200: Success
            403: Invalid token
            500: Server error or no response
        """
        # Open authentication channel
        channel = await self._open_channel(Channel.UAUT)

        # Build authentication message
        # The 'message-id' field must be numeric 2, not the string channel name
        # This is a quirk of the protocol - different contexts use different ID types
        auth_data = {
            "message": "access",
            "user-token": token,
            "message-type": "request",
            "message-id": 2,  # Must be 2, not ViperChannelType.UAUT
        }
        packet = self._create_json_packet(channel.id, auth_data)
        await self._write_packet(packet)

        # Read authentication response
        response = await self._read_response()
        if response and response["type"] == "json":
            code = response["data"].get("response-code", 500)
            await self._close_channel(channel)
            return code

        # No valid response received
        await self._close_channel(channel)
        return 500

    async def get_config(self, addressbooks: str = "all") -> dict | None:
        """Get configuration from the device"""
        # Open config channel
        channel = await self._open_channel(Channel.UCFG)

        # Send get-configuration message
        config_data = {
            "message": "get-configuration",
            "addressbooks": addressbooks,
            "message-type": "request",
            "message-id": ViperChannelType.UCFG,
        }
        packet = self._create_json_packet(channel.id, config_data)
        await self._write_packet(packet)

        # Read response
        response = await self._read_response()
        if response and response["type"] == "json":
            await self._close_channel(channel)
            return response["data"]

        await self._close_channel(channel)
        return None

    async def list_doors(self) -> list[dict]:
        """List all available doors"""
        config = await self.get_config("all")
        if config and "vip" in config:
            return (
                config["vip"]
                .get("user-parameters", {})
                .get("opendoor-address-book", [])
            )
        return []

    async def list_actuators(self) -> list[dict]:
        """List all available actuators"""
        config = await self.get_config("all")
        if config and "vip" in config:
            return (
                config["vip"]
                .get("user-parameters", {})
                .get("actuator-address-book", [])
            )
        return []

    def _string_to_buffer(self, s: str, null_terminated: bool = False) -> bytes:
        """Convert string to bytes buffer"""
        b = s.encode("ascii")
        if null_terminated:
            b += NULL
        return b

    async def _open_door_init(self, vip: dict):
        """Initialize door opening sequence

        This establishes a control channel (CTPP) for the apartment.
        The init message contains specific byte patterns that the device
        expects for proper door control authorization.
        """
        apt_address = f"{vip['apt-address']}{vip.get('apt-subaddress', '')}"
        channel = await self._open_channel(Channel.CTPP, apt_address)

        # Build initialization message with specific byte patterns
        # These bytes were discovered through protocol analysis and must be exact
        buffers = [
            bytes([0xC0, 0x18, 0x5C, 0x8B]),  # Message type and fixed pattern
            bytes([0x2B, 0x73, 0x00, 0x11]),  # Fixed pattern (possibly version/flags)
            bytes([0x00, 0x40, 0xAC, 0x23]),  # Fixed pattern
            self._string_to_buffer(apt_address, True),  # Full apartment address
            bytes([0x10, 0x0E]),  # Fixed values
            bytes([0x00, 0x00, 0x00, 0x00]),  # Padding
            bytes([0xFF, 0xFF, 0xFF, 0xFF]),  # All-ones pattern (broadcast/wildcard?)
            self._string_to_buffer(apt_address, True),  # Apartment address again
            self._string_to_buffer(
                vip["apt-address"], True
            ),  # Base address without subaddress
            NULL,
        ]
        packet = self._create_binary_packet_from_buffers(channel.id, *buffers)
        await self._write_packet(packet)

        # The device sends two responses to initialization
        # These often timeout on some firmware versions, but the channel
        # is still established successfully
        try:
            await asyncio.wait_for(self._read_response(), timeout=2.0)
            await asyncio.wait_for(self._read_response(), timeout=2.0)
        except TimeoutError:
            self.logger.warning(
                "Timeout waiting for CTPP init responses - continuing anyway"
            )

    async def open_door(self, vip: dict, door_item: dict):
        """Open a specific door

        The door opening sequence involves multiple steps:
        1. Initialize CTPP channel if not already open
        2. Send open door command (0x1800) and confirmation (0x1820)
        3. Send door-specific initialization
        4. Repeat open door command and confirmation

        This redundancy ensures reliability across different firmware versions.
        """
        # Initialize control channel if needed
        if Channel.CTPP not in self.open_channels:
            await self._open_door_init(vip)

        channel = self.open_channels[Channel.CTPP]

        # Helper function to create door command messages
        def create_door_message(confirm: bool = False) -> bytes:
            # Two message types: OPEN_DOOR (0x1800) and OPEN_DOOR_CONFIRM (0x1820)
            # Both are required for the door to actually open
            msg_type = (
                MessageType.OPEN_DOOR_CONFIRM if confirm else MessageType.OPEN_DOOR
            )
            buffers = [
                struct.pack("<H", msg_type),  # Message type
                bytes([0x5C, 0x8B]),  # Fixed pattern (always present in door messages)
                bytes([0x2C, 0x74, 0x00, 0x00]),  # Fixed pattern
                bytes([0xFF, 0xFF, 0xFF, 0xFF]),  # Broadcast/wildcard pattern
                # Apartment address + output index identifies the specific door/actuator
                self._string_to_buffer(
                    f"{vip['apt-address']}{door_item['output-index']}", True
                ),
                self._string_to_buffer(
                    door_item["apt-address"], True
                ),  # Door's apartment address
                NULL,
            ]
            return self._create_binary_packet_from_buffers(channel.id, *buffers)

        # First door command sequence
        await self._write_packet(create_door_message(False))  # OPEN_DOOR
        await self._write_packet(create_door_message(True))  # OPEN_DOOR_CONFIRM

        # Send door-specific initialization
        # This message has slightly different byte patterns than the channel init
        buffers = [
            bytes(
                [0xC0, 0x18, 0x70, 0xAB]
            ),  # Different from channel init (0x70 vs 0x5c)
            bytes([0x29, 0x9F, 0x00, 0x0D]),  # Different pattern
            bytes([0x00, 0x2D]),  # Different value
            self._string_to_buffer(door_item["apt-address"], True),
            NULL,
            # Output index as 4-byte little-endian integer
            # This identifies which relay/actuator to trigger
            bytes([int(door_item["output-index"]), 0x00, 0x00, 0x00]),
            bytes([0xFF, 0xFF, 0xFF, 0xFF]),  # Broadcast pattern
            self._string_to_buffer(
                f"{vip['apt-address']}{door_item['output-index']}", True
            ),
            self._string_to_buffer(door_item["apt-address"], True),
            NULL,
        ]
        packet = self._create_binary_packet_from_buffers(channel.id, *buffers)
        await self._write_packet(packet)

        # Wait for initialization responses
        # Like channel init, these may timeout but that's normal
        try:
            await asyncio.wait_for(self._read_response(), timeout=2.0)
            await asyncio.wait_for(self._read_response(), timeout=2.0)
        except TimeoutError:
            self.logger.warning(
                "Timeout waiting for door init responses - continuing anyway"
            )

        # Send final door command sequence
        # This redundancy improves reliability - some devices need both sequences
        await self._write_packet(create_door_message(False))  # OPEN_DOOR
        await self._write_packet(create_door_message(True))  # OPEN_DOOR_CONFIRM

        # Don't wait for final responses - the door actuator triggers immediately
        # and the device may not send acknowledgments
        self.logger.info(f"Door '{door_item.get('name', 'Unknown')}' open command sent")


    def _get_open_actuator_message(self, vip: dict, item: dict, channel_id: int, confirm: bool = False) -> bytes:
        """Create open actuator message"""
        buffers = [
            bytes([0x20 if confirm else 0x00, 0x18, 0x45, 0xBE]),
            bytes([0x8F, 0x5C, 0x00, 0x04]),
            bytes([0xFF, 0xFF, 0xFF, 0xFF]),
            self._string_to_buffer(f"{vip['apt-address']}{item['output-index']}", True),
            self._string_to_buffer(item["apt-address"], True),
            NULL,
        ]
        return self._create_binary_packet_from_buffers(channel_id, *buffers)

    def _get_init_open_actuator_message(self, vip: dict, item: dict, channel_id: int) -> bytes:
        """Create init open actuator message"""
        buffers = [
            bytes([0xC0, 0x18, 0x45, 0xBE]),
            bytes([0x8F, 0x5C, 0x00, 0x04]),
            bytes([0x00, 0x20, 0xFF, 0x01]),
            bytes([0xFF, 0xFF, 0xFF, 0xFF]),
            self._string_to_buffer(f"{vip['apt-address']}{item['output-index']}", True),
            self._string_to_buffer(item["apt-address"], True),
            NULL,
        ]
        return self._create_binary_packet_from_buffers(channel_id, *buffers)

    async def open_actuator(self, vip: dict, item: dict):
        """Open a specific actuator"""
        if Channel.CTPP not in self.open_channels:
            await self._open_door_init(vip)

        channel = self.open_channels[Channel.CTPP]

        message1 = self._get_init_open_actuator_message(vip, item, channel.id)
        await self._write_packet(message1)
        resp = await self._read_response()
        self.logger.debug(f"{resp}")
        resp2 = await self._read_response()
        self.logger.debug(f"{resp2}")

        packet_message1 = self._get_open_actuator_message(vip, item, False, channel.id)
        await self._write_packet(packet_message1)
        confirm_message1 = self._get_open_actuator_message(vip, item, True, channel.id)
        await self._write_packet(confirm_message1)


# High-level convenience functions
async def list_doors(host: str, token: str) -> list[dict[str, Any]]:
    """List all available doors from a Comelit device"""
    client = IconaBridgeClient(host)
    try:
        await client.connect()
        auth_code = await client.authenticate(token)
        if auth_code == 200:
            return await client.list_doors()
        else:
            raise Exception(f"Authentication failed with code {auth_code}")
    finally:
        await client.shutdown()

# High-level convenience functions
async def list_actuators(host: str, token: str) -> list[dict[str, Any]]:
    """List all available actuators from a Comelit device"""
    client = IconaBridgeClient(host)
    try:
        await client.connect()
        auth_code = await client.authenticate(token)
        if auth_code == 200:
            return await client.list_actuators()
        else:
            raise Exception(f"Authentication failed with code {auth_code}")
    finally:
        await client.shutdown()


async def open_door(host: str, token: str, door_name: str) -> bool:
    """Open a specific door by name"""
    client = IconaBridgeClient(host)
    try:
        await client.connect()
        auth_code = await client.authenticate(token)
        if auth_code != 200:
            raise Exception(f"Authentication failed with code {auth_code}")

        # Get configuration
        config = await client.get_config("all")
        if not config or "vip" not in config:
            raise Exception("Failed to get configuration")

        vip = config["vip"]
        doors = vip.get("user-parameters", {}).get("opendoor-address-book", [])

        # Find the door by name
        door = next((d for d in doors if d.get("name") == door_name), None)
        if not door:
            available = [d.get("name", "Unknown") for d in doors]
            raise Exception(
                f"Door '{door_name}' not found. Available: {', '.join(available)}"
            )

        # Open the door
        await client.open_door(vip, door)
        return True

    finally:
        await client.shutdown()

async def open_actuator(host: str, token: str, actuator_name: str) -> bool:
    """Open a specific actuator by name"""
    client = IconaBridgeClient(host)
    try:
        await client.connect()
        auth_code = await client.authenticate(token)
        if auth_code != 200:
            raise Exception(f"Authentication failed with code {auth_code}")

        # Get configuration
        config = await client.get_config("all")
        if not config or "vip" not in config:
            raise Exception("Failed to get configuration")

        vip = config["vip"]
        actuators = vip.get("user-parameters", {}).get("actuator-address-book", [])

        # Find the actuator by name
        actuator = next((a for a in actuators if a.get("name") == actuator_name), None)
        if not actuator:
            available = [a.get("name", "Unknown") for a in actuators]
            raise Exception(
                f"Actuator '{actuator_name}' not found. Available: {', '.join(available)}"
            )

        # Open the actuator
        await client.open_actuator(vip, actuator)
        return True

    finally:
        await client.shutdown()
