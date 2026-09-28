"""Wire protocol: header encoding/decoding, message types, serialization."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, cast

from .const import VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH

HEADER_SIZE = 8  # Fixed header size: magic(2) + length(2) + request_id(2) + padding(2)
HEADER_MAGIC = b"\x00\x06"  # All messages start with these magic bytes
ICONA_BRIDGE_PORT = 64100  # TCP port for ICONA Bridge protocol
NULL = b"\x00"

# CTPP init message magic byte sequences (from PCAP analysis)
_CTPP_INIT_FLAGS1 = bytes([0x00, 0x11])
_CTPP_INIT_FLAGS2 = bytes([0x00, 0x40])
_CTPP_INIT_CAPABILITY = bytes(
    [0x18, 0xC2]
)  # protocol constant (PCAP-verified; echoed by device in renewals)
_CTPP_INIT_SEPARATOR = bytes([0x10, 0x0E])
_CTPP_INIT_ZERO_PAD = bytes([0x00, 0x00, 0x00, 0x00])
_CTPP_ADDR_WILDCARD = bytes([0xFF, 0xFF, 0xFF, 0xFF])
_CTPP_LEGACY_TS = bytes(
    [0x5C, 0x8B, 0x2B, 0x73]
)  # hardcoded timestamp for door open flow


class MessageType(IntEnum):
    """Binary message type constants."""

    COMMAND = 0xABCD
    END = 0x01EF
    OPEN_DOOR_INIT = 0x18C0
    OPEN_DOOR = 0x1800
    OPEN_DOOR_CONFIRM = 0x1820


def encode_header(body_length: int, request_id: int = 0) -> bytes:
    """Encode an 8-byte ICONA Bridge packet header.

    Format: [0x00 0x06] [body_length LE uint16] [request_id LE uint16] [0x00 0x00]
    """
    header = bytearray(HEADER_SIZE)
    header[0:2] = HEADER_MAGIC
    header[2:4] = struct.pack("<H", body_length)  # '<H' = little-endian unsigned short
    header[4:6] = struct.pack("<H", request_id)
    header[6:8] = b"\x00\x00"  # Required padding
    return bytes(header)


def decode_header(data: bytes) -> tuple[int, int]:
    """Decode an 8-byte header. Returns (body_length, request_id)."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"Header too short: {len(data)} bytes")
    body_length = struct.unpack_from("<H", data, 2)[0]
    request_id = struct.unpack_from("<H", data, 4)[0]
    return body_length, request_id


def encode_json_message(msg: dict[str, Any], request_id: int) -> bytes:
    """Encode a JSON message with header. Uses compact JSON (no spaces)."""
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    return encode_header(len(body), request_id) + body


def decode_json_body(body: bytes) -> dict[str, Any]:
    """Decode a JSON body."""
    return cast("dict[str, Any]", json.loads(body.decode("utf-8")))


def _null_terminated(s: str) -> bytes:
    """Encode a string as null-terminated ASCII bytes."""
    return s.encode("ascii") + NULL


def encode_channel_open(
    channel_name: str,
    channel_type_id: int,
    sequence: int,
    request_id: int,
    extra_data: str | None = None,
    trailing_byte: int = 0,
) -> bytes:
    """Encode a COMMAND (channel open) binary packet.

    Body layout (PCAP-verified):
      [MessageType.COMMAND as LE uint16] [sequence as LE uint16]
      [channel_type_id as LE uint32]
      [channel_name as ASCII (no null)]
      [request_id as LE uint16] [trailing_byte]
      [optional: 0x00 pad byte, extra_data length+1 as LE uint32, extra_data null-terminated]

    The 0x00 pad byte before extra_data is required by the device. Without it,
    extra_data lands at the wrong offset and the device ignores CTPP messages.
    Both PCAP and old (accidentally correct) code placed extra_data at body[20].
    """
    body = bytearray()
    body += struct.pack("<HH", MessageType.COMMAND, sequence)
    body += struct.pack("<I", channel_type_id)
    body += channel_name.encode("ascii")
    body += struct.pack("<H", request_id)
    body += bytes([trailing_byte])
    if extra_data:
        body += b"\x00"  # pad byte (PCAP-verified: present before extra_len)
        extra_bytes = extra_data.encode("ascii")
        body += struct.pack("<I", len(extra_bytes) + 1)
        body += extra_bytes + NULL
    return encode_header(len(body), 0) + body


def encode_channel_open_response(request_id: int) -> bytes:
    """Encode a COMMAND response to a device-initiated channel open.

    From PCAP: when the device opens a channel (e.g., RTPC after video config),
    the phone responds with a COMMAND response mirroring the device's request_id.

    Body: [0xABCD LE16] [seq=2 LE16] [0x04000000] [request_id LE16] [0x0000]
    """
    body = bytearray()
    body += struct.pack("<HH", MessageType.COMMAND, 2)  # seq=2
    body += struct.pack("<I", 4)
    body += struct.pack("<H", request_id)
    body += b"\x00\x00"
    return encode_header(len(body), 0) + bytes(body)


def encode_channel_close(sequence: int, server_channel_id: int = 0) -> bytes:
    """Encode an END (channel close) binary packet.

    server_channel_id: the device-assigned ID for the channel to close.
    Included as request_id in the header so the device can identify which
    channel is being closed and release its associated session state.
    """
    body = struct.pack("<H", MessageType.END) + struct.pack("<H", sequence)
    return encode_header(len(body), server_channel_id) + bytes(body)


def parse_command_response(body: bytes) -> tuple[int, int, int]:
    """Parse a COMMAND response body.

    Returns (message_type, sequence, server_channel_id).
    """
    msg_type = struct.unpack_from("<H", body, 0)[0]
    seq = struct.unpack_from("<H", body, 2)[0]
    server_channel_id = 0
    if len(body) >= 10:
        server_channel_id = struct.unpack_from("<H", body, 8)[0]
    return msg_type, seq, server_channel_id


def is_json_body(body: bytes) -> bool:
    """Check if a response body is JSON (starts with '{')."""
    return len(body) > 0 and body[0:1] == b"{"


# --- Door open binary payloads ---


def encode_ctpp_init(
    apt_address: str, apt_subaddress: int, timestamp: int | None = None
) -> bytes:
    """Encode the CTPP channel init message (Phase A of door open).

    Sent after opening the CTPP channel. The timestamp fills bytes 2-5 and
    must be reused in subsequent ACKs so the device can match them to this
    init (bytes 2-3 act as a "session ID" that must stay consistent).
    """
    addr_with_sub = f"{apt_address}{apt_subaddress}"
    buf = bytearray()
    buf += struct.pack("<H", 0x18C0)
    if timestamp is not None:
        buf += struct.pack("<I", timestamp)
    else:
        buf += _CTPP_LEGACY_TS
    buf += _CTPP_INIT_FLAGS1
    buf += _CTPP_INIT_FLAGS2
    buf += _CTPP_INIT_CAPABILITY
    buf += _null_terminated(addr_with_sub)
    buf += _CTPP_INIT_SEPARATOR
    buf += _CTPP_INIT_ZERO_PAD
    buf += _CTPP_ADDR_WILDCARD
    buf += _null_terminated(addr_with_sub)
    buf += _null_terminated(apt_address)
    buf += b"\x00"
    return bytes(buf)


def encode_open_door(
    msg_type: MessageType,
    apt_address: str,
    output_index: int,
    door_apt_address: str,
) -> bytes:
    """Encode an OPEN_DOOR or OPEN_DOOR_CONFIRM message (Phase B/D)."""
    buf = bytearray()
    buf += struct.pack("<H", msg_type)
    buf += bytes([0x5C, 0x8B])
    buf += bytes([0x2C, 0x74, 0x00, 0x00])
    buf += bytes([0xFF, 0xFF, 0xFF, 0xFF])
    buf += _null_terminated(f"{apt_address}{output_index}")
    buf += _null_terminated(door_apt_address)
    buf += b"\x00"
    return bytes(buf)


def encode_door_init(
    apt_address: str,
    output_index: int,
    door_apt_address: str,
) -> bytes:
    """Encode the door-specific init message (Phase C of door open)."""
    buf = bytearray()
    buf += bytes([0xC0, 0x18, 0x70, 0xAB])
    buf += bytes([0x29, 0x9F, 0x00, 0x0D])
    buf += bytes([0x00, 0x2D])
    buf += _null_terminated(door_apt_address)
    buf += b"\x00"
    buf += struct.pack("<I", output_index)
    buf += bytes([0xFF, 0xFF, 0xFF, 0xFF])
    buf += _null_terminated(f"{apt_address}{output_index}")
    buf += _null_terminated(door_apt_address)
    buf += b"\x00"
    return bytes(buf)


def encode_actuator_init(
    apt_address: str,
    output_index: int,
    actuator_apt_address: str,
) -> bytes:
    """Encode actuator init message (alternative door type)."""
    buf = bytearray()
    buf += bytes([0xC0, 0x18, 0x45, 0xBE])
    buf += bytes([0x8F, 0x5C, 0x00, 0x04])
    buf += bytes([0x00, 0x20, 0xFF, 0x01])
    buf += bytes([0xFF, 0xFF, 0xFF, 0xFF])
    buf += _null_terminated(f"{apt_address}{output_index}")
    buf += _null_terminated(actuator_apt_address)
    buf += b"\x00"
    return bytes(buf)


def encode_actuator_open(
    apt_address: str,
    output_index: int,
    actuator_apt_address: str,
    confirm: bool = False,
) -> bytes:
    """Encode actuator open/confirm message."""
    buf = bytearray()
    buf += bytes([0x20 if confirm else 0x00, 0x18, 0x45, 0xBE])
    buf += bytes([0x8F, 0x5C, 0x00, 0x04])
    buf += bytes([0xFF, 0xFF, 0xFF, 0xFF])
    buf += _null_terminated(f"{apt_address}{output_index}")
    buf += _null_terminated(actuator_apt_address)
    buf += b"\x00"
    return bytes(buf)


# --- Video call binary payloads ---

# Action codes used in CTPP video signaling messages
ACTION_CALL_INIT = 0x0028
ACTION_CODEC_NEG = 0x0008
ACTION_RTPC_LINK = 0x000A
ACTION_VIDEO_CONFIG = 0x001A
ACTION_PEER = 0x0070  # "accept call" / peer (answer sequence msg 1)
ACTION_CONFIG_ACK = 0x000E  # supplemental config ACK (answer sequence msg 2)
ACTION_SELF_VIEW_AUDIO_CONFIG = 0x000C
ACTION_HANGUP = 0x002D  # '-' = hangup
ACTION_DOOR_OPEN = 0x000D  # door open on active video CTPP channel (PCAP-verified)
ACTION_CALL_ACCEPTED = 0x0002  # device→app on outbound; app→device on inbound answer
ACTION_RTPC2_READY = (
    0x0003  # sent by app after RTPC2 opens on inbound answer (PCAP-verified)
)


def _build_ctpp_video_msg(
    prefix: int,
    timestamp: int,
    action: int,
    flags: int,
    caller: str,
    callee: str,
    extra: bytes = b"",
) -> bytes:
    """Build a CTPP video signaling binary message.

    Common structure:
      [prefix LE uint16] [timestamp LE uint32] [action BE uint16] [flags BE uint16]
      [extra bytes] [0xFFFFFFFF] [caller\\0] [callee\\0\\0]
    """
    buf = bytearray()
    buf += struct.pack("<H", prefix)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", action)
    buf += struct.pack(">H", flags)
    buf += extra
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += callee.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_call_init(caller: str, callee: str, timestamp: int) -> bytes:
    """Encode call initiation message (c018 prefix, action 0x0028).

    From PCAP the call init has a larger format with addresses before and
    after the separator, plus an "II" codec marker.
    """
    buf = bytearray()
    buf += struct.pack("<H", 0x18C0)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", ACTION_CALL_INIT)
    buf += struct.pack(">H", 0x0001)
    buf += caller.encode("ascii") + b"\x00"
    buf += callee.encode("ascii") + b"\x00\x00"
    # 6-byte session block (flag + random session ID)
    buf += bytes([0x01, 0x20])
    buf += struct.pack("<I", timestamp ^ 0xC0D31185)
    buf += caller.encode("ascii") + b"\x00"
    buf += b"II"
    buf += b"\xff\xff\xff\xff"
    buf += caller.encode("ascii") + b"\x00"
    buf += callee.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_call_ack(
    caller: str, callee: str, timestamp: int, codec_param: int = 0x27
) -> bytes:
    """Encode codec negotiation / call ack (4018 prefix, action 0x0008).

    Sent after receiving initial call responses.
    Extra bytes: 0x49='I' codec marker, 0x00, codec_param, padding.
    codec_param: 0x27 for outbound (HA-initiated), 0x07 for inbound (PCAP-verified).
    """
    return _build_ctpp_video_msg(
        prefix=0x1840,
        timestamp=timestamp,
        action=ACTION_CODEC_NEG,
        flags=0x0003,
        caller=caller,
        callee=callee,
        extra=bytes([0x49, 0x00, codec_param, 0x00, 0x00, 0x00]),
    )


def encode_rtpc_link(
    caller: str, callee: str, rtpc_req_id: int, timestamp: int, refresh: bool = False
) -> bytes:
    """Encode RTPC link message (4018 prefix, action 0x000A).

    Links an RTPC channel to the active call.
    Extra: [first_byte 0x02] [4 zeros] [rtpc_req_id LE16] [2 zeros]

    When refresh=True (re-establishment after CALL_END), first_byte is 0x98
    instead of 0x18 (bit 7 set, PCAP-verified).
    """
    extra = bytearray()
    extra += bytes([0x98 if refresh else 0x18, 0x02, 0x00, 0x00, 0x00, 0x00])
    extra += struct.pack("<H", rtpc_req_id)
    extra += bytes([0x00, 0x00])
    return _build_ctpp_video_msg(
        prefix=0x1840,
        timestamp=timestamp,
        action=ACTION_RTPC_LINK,
        flags=0x0011,
        caller=caller,
        callee=callee,
        extra=bytes(extra),
    )


def encode_video_config_resp(
    caller: str,
    callee: str,
    rtpc2_req_id: int,
    timestamp: int,
) -> bytes:
    """Encode video config response (6018 prefix, action 0x001A).

    Sent after the CALL_END / RTPC_LINK re-establishment sequence to complete
    the video session lock-in. Uses prefix 0x1860 and a stripped-down extra
    block (zeros only, no resolution/fps fields).

    Extra: [0x94 0x02] [4 zeros] [rtpc2_req_id LE16] [18 zeros]
    (PCAP-verified from working Android session)
    """
    extra = bytearray()
    extra += bytes([0x94, 0x02, 0x00, 0x00, 0x00, 0x00])
    extra += struct.pack("<H", rtpc2_req_id)
    extra += bytes(18)  # 18 trailing zeros (vs resolution+fps in original)
    return _build_ctpp_video_msg(
        prefix=0x1860,
        timestamp=timestamp,
        action=ACTION_VIDEO_CONFIG,
        flags=0x0011,
        caller=caller,
        callee=callee,
        extra=bytes(extra),
    )


def encode_video_config(
    caller: str,
    callee: str,
    rtpc2_req_id: int,
    timestamp: int,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    fps: int = VIDEO_FPS,
) -> bytes:
    """Encode video config trigger (4018 prefix, action 0x001A).

    This is the final message that triggers the device to start UDP video.
    Extra: [0x14 0x32] [4 zeros] [rtpc2_req_id LE16] [0xFFFF] [4 zeros]
           [width LE16] [height LE16] [width/2 LE16] [height/2 LE16] [fps LE16] [2 zeros]
    """
    extra = bytearray()
    extra += bytes([0x14, 0x32, 0x00, 0x00, 0x00, 0x00])
    extra += struct.pack("<H", rtpc2_req_id)
    extra += bytes(
        [0xFF, 0xFF, 0xC0, 0x00, 0x00, 0x00]
    )  # PCAP2: extra[10:14]=c0 00 00 00
    extra += struct.pack("<H", width)
    extra += struct.pack("<H", height)
    # PCAP shows 320x240 as the secondary resolution (not width//2)
    extra += struct.pack("<H", 320)
    extra += struct.pack("<H", 240)
    extra += struct.pack("<H", fps)
    extra += bytes([0x00, 0x00])
    return _build_ctpp_video_msg(
        prefix=0x1840,
        timestamp=timestamp,
        action=ACTION_VIDEO_CONFIG,
        flags=0x0011,
        caller=caller,
        callee=callee,
        extra=bytes(extra),
    )


def encode_call_response_ack(
    caller: str, callee: str, timestamp: int, prefix: int = 0x1800
) -> bytes:
    """Encode an ACK for a device call response.

    From PCAP: ACK messages (0x1800/0x1820 prefix) use a shorter format
    WITHOUT the flags field:
      [prefix LE16] [timestamp LE32] [action=0x0000 BE16]
      [0xFFFFFFFF] [caller\\0] [callee\\0\\0]
    """
    buf = bytearray()
    buf += struct.pack("<H", prefix)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", 0x0000)  # action = 0
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += callee.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_answer_video_reconfig(
    caller: str,
    apt_addr: str,
    rtpc2_req_id: int,
    timestamp: int,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    fps: int = VIDEO_FPS,
) -> bytes:
    """Encode answer sequence message 1: video config re-negotiate.

    Identical to encode_video_config but callee is apt_addr (not entrance_addr).
    PCAP-verified: prefix=0x1840, callee="SB000006" (apt_address without subaddress).
    """
    extra = bytearray()
    extra += bytes([0x14, 0x32, 0x00, 0x00, 0x00, 0x00])
    extra += struct.pack("<H", rtpc2_req_id)
    extra += bytes([0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00])
    extra += struct.pack("<H", width)
    extra += struct.pack("<H", height)
    extra += struct.pack("<H", 320)
    extra += struct.pack("<H", 240)
    extra += struct.pack("<H", fps)
    extra += bytes([0x00, 0x00])
    return _build_ctpp_video_msg(
        prefix=0x1840,
        timestamp=timestamp,
        action=ACTION_VIDEO_CONFIG,
        flags=0x0011,
        caller=caller,
        callee=apt_addr,
        extra=bytes(extra),
    )


def encode_self_view_audio_peer(
    caller: str,
    apt_addr: str,
    apt_subaddress: str,
    timestamp: int,
) -> bytes:
    """Encode the peer message used to add audio to a self-view session.

    Unlike the regular outbound peer message, the captured audio activation
    sequence targets the apartment and carries its full subaddress inside the
    peer payload.
    """
    inner_payload = apt_subaddress.encode("ascii") + b"\x00" + b"\x01\x00\x00\x00"
    buf = bytearray()
    buf += struct.pack("<H", 0x1840)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", len(inner_payload))
    buf += struct.pack(">H", ACTION_PEER)
    buf += inner_payload
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += apt_addr.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_self_view_audio_config_ack(
    caller: str,
    apt_addr: str,
    timestamp: int,
) -> bytes:
    """Encode the final config message in self-view audio activation."""
    buf = bytearray()
    buf += struct.pack("<H", 0x1840)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", 0x0002)
    buf += struct.pack(">H", ACTION_SELF_VIEW_AUDIO_CONFIG)
    buf += b"\x00\x00"
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += apt_addr.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_answer_peer(
    caller: str,
    callee: str,
    timestamp: int,
    renewal: bool = False,
    inbound: bool = False,
) -> bytes:
    """Encode peer/accept message (action 0x70).

    Wire format (PCAP2-verified for both outbound and inbound):
      [prefix LE16] [timestamp LE32] [inner_len BE16] [0x0070 BE16]
      [caller\\0] [flag 0x01 0x00] [0x0000 if inbound] [0xFFFFFFFF]
      [caller\\0] [callee\\0\\0]

    Outbound: callee=entrance_addr, no extra padding before separator.
    Inbound:  callee=our_base_addr, extra 0x0000 padding before separator.
    Both formats include caller after the separator.

    Initial call: prefix=0x1840, flag=0x01
    Renewal:      prefix=0x1860, flag=0x00
    """
    prefix = 0x1860 if renewal else 0x1840
    flag = b"\x00\x00" if renewal else b"\x01\x00"
    inner_payload = caller.encode("ascii") + b"\x00" + flag
    buf = bytearray()
    buf += struct.pack("<H", prefix)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", 2 + len(inner_payload))  # inner_len = action(2) + payload
    buf += struct.pack(">H", ACTION_PEER)  # 0x0070
    buf += inner_payload
    if inbound:
        buf += b"\x00\x00"  # extra padding before separator (PCAP2-verified)
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += callee.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_answer_config_ack(
    caller: str,
    entrance_addr: str,
    timestamp: int,
) -> bytes:
    """Encode answer sequence message 2: supplemental config ACK (action 0x0e).

    PCAP-verified wire format:
      [0x1840 LE16] [timestamp LE32] [0x0002 BE16] [0x000E BE16]
      [0x0000] [0xFFFFFFFF] [caller\\0] [entrance_addr\\0\\0]
    """
    buf = bytearray()
    buf += struct.pack("<H", 0x1840)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", 0x0002)  # inner_len = 2 (just the padding)
    buf += struct.pack(">H", ACTION_CONFIG_ACK)  # 0x000E
    buf += b"\x00\x00"  # 2 bytes padding
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += entrance_addr.encode("ascii") + b"\x00\x00"
    return bytes(buf)


def encode_rtpc2_ready(caller: str, callee: str, timestamp: int) -> bytes:
    """Encode RTPC2-ready message (0x1840/0x0003, flags=0x000A).

    Sent immediately after RTPC2 opens on inbound answer calls. From PCAP:
    extra=b"\\x00\\x00". Purpose unknown; required by device to proceed.
    """
    return _build_ctpp_video_msg(
        prefix=0x1840,
        timestamp=timestamp,
        action=ACTION_RTPC2_READY,
        flags=0x000A,
        caller=caller,
        callee=callee,
        extra=b"\x00\x00",
    )


def encode_call_accepted(caller: str, callee: str, timestamp: int) -> bytes:
    """Encode call-accepted message sent TO the device on inbound answer.

    On inbound calls the roles are reversed: the app sends ACTION_CALL_ACCEPTED
    (0x1840/0x0002, flags=0x000C) to the device instead of waiting to receive it.
    PCAP2-verified extra=b"\\x00\\x00" (4 total FF bytes = separator only).
    Sent after the PEER message.
    """
    return _build_ctpp_video_msg(
        prefix=0x1840,
        timestamp=timestamp,
        action=ACTION_CALL_ACCEPTED,
        flags=0x000C,
        caller=caller,
        callee=callee,
        extra=b"\x00\x00",
    )


def encode_door_open_during_video(
    our_addr: str,
    entrance_addr: str,
    call_counter: int,
    relay_index: int,
) -> bytes:
    """Encode a door open command for use on an active video CTPP channel.

    PCAP-verified (camera_feed_with_open_door_local.pcap): during video the
    Android app sends a SINGLE 0x1840/0x000D message on the existing video
    CTPP channel — no separate channel open, no 6-step sequence.

    Body structure (48 bytes):
      [LE16 0x1840] [LE32 counter] [BE16 0x000D] [BE16 0x002D]
      [entrance_addr padded to 10] [LE32 relay_index] [4x 0xFF]
      [our_addr padded to 10] [entrance_addr padded to 10]

    relay_index: the door's output_index from the device config (PCAP shows 1
    for the only door on that device; use door.output_index for our device).
    """
    our_b = our_addr.encode("ascii").ljust(10, b"\x00")[:10]
    entr_b = entrance_addr.encode("ascii").ljust(10, b"\x00")[:10]
    buf = bytearray()
    buf += struct.pack("<H", 0x1840)
    buf += struct.pack("<I", call_counter)
    buf += struct.pack(">H", ACTION_DOOR_OPEN)
    buf += struct.pack(">H", 0x002D)
    buf += entr_b
    buf += struct.pack("<I", relay_index)
    buf += b"\xff\xff\xff\xff"
    buf += our_b
    buf += entr_b
    return bytes(buf)


def encode_hangup(
    caller: str,
    entrance_addr: str,
    timestamp: int,
) -> bytes:
    """Encode hangup message (action 0x2d).

    Sent to terminate the call. Body includes the entrance address
    (e.g. "SB100001").
    """
    buf = bytearray()
    buf += struct.pack("<H", 0x1830)
    buf += struct.pack("<I", timestamp)
    buf += struct.pack(">H", ACTION_HANGUP)
    buf += entrance_addr.encode("ascii") + b"\x00"
    buf += b"\xff\xff\xff\xff"
    buf += _null_terminated(caller)
    buf += entrance_addr.encode("ascii") + b"\x00\x00"
    return bytes(buf)


@dataclass
class RtpHeader:
    """Parsed RTP header fields."""

    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int


def decode_rtp_header(data: bytes) -> tuple[RtpHeader, bytes]:
    """Parse ICONA header + RTP header from a UDP video packet.

    Strips the 8-byte ICONA header, parses 12-byte RTP header,
    returns (RtpHeader, payload).
    """
    if len(data) < HEADER_SIZE + 12:
        raise ValueError(f"Packet too short for ICONA+RTP: {len(data)} bytes")

    # Skip ICONA 8-byte header
    rtp_data = data[HEADER_SIZE:]

    byte0 = rtp_data[0]
    byte1 = rtp_data[1]
    header = RtpHeader(
        version=(byte0 >> 6) & 0x03,
        padding=bool((byte0 >> 5) & 0x01),
        extension=bool((byte0 >> 4) & 0x01),
        csrc_count=byte0 & 0x0F,
        marker=bool((byte1 >> 7) & 0x01),
        payload_type=byte1 & 0x7F,
        sequence=struct.unpack(">H", rtp_data[2:4])[0],
        timestamp=struct.unpack(">I", rtp_data[4:8])[0],
        ssrc=struct.unpack(">I", rtp_data[8:12])[0],
    )

    payload = rtp_data[12:]
    return header, payload
