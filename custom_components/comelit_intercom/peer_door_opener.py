"""Peer-aware door opener for Comelit intercoms (ICONA Bridge / ViPER).

This implements the proven behavior documented in `vip_peer_door_open.py`:

- Door open depends on the currently active peer/stream (cam1 vs cam2).
- To reliably select the cam2 peer, we start a call, wait for video to flow,
  then restart the call (RELEASE + START40) and wait for video again.
- The cam2 peer association persists for a short time (~20s), then falls back.
- For repeated door2 requests we reuse the cached cam2 peer only for a shorter
  configurable window (default: 15s) as a safety margin.

The actual door-open command is sent using the same "door-open only" tap
sequence as `vip_open_door_only.py` (REGISTER_REQ + OPENDOOR frames).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Observed on MSVF 2.1.3: cam2 peer association lasts ~20s then falls back.
PEER_FALLBACK_WINDOW_S = 20.0

# Call restart tuning (copied from vip_restart_camera_old.py)
RESTART_DELAY_S = 0.30
CAMERA_SWITCH_WARMUP_S = 2.0

# Keepalive pattern (copied from vip_restart_camera_old.py)
UDP_KEEPALIVE_INTERVAL_S = 0.33
UDP_KEEPALIVE_INTERVALS = 3
UDP_KEEPALIVE_BURST = 4
UDP_FORCE_FLAG1_AFTER_S = 2.5

VIPER_MAGIC = 0x0600
CMD_OPEN = 0xABCD
CHANNEL_TYPE = 7

# Tap framing
_RTCP_PACKET_TYPES = {200, 201, 202, 203, 204, 205, 206}
VIDEO_BITRATE_KBPS = 1000


@dataclass(frozen=True)
class PeerDoorConfig:
    """Configuration for peer-aware door opening."""

    call_target: str
    door_count: int
    cam2_reuse_window: float
    door_delay: float


class PeerDoorOpener:
    """Open Door 1/2 by selecting the active peer/stream."""

    _STORE_VERSION = 1
    _STORE_KEY = f"{DOMAIN}_peer_door_state"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, self._STORE_VERSION, self._STORE_KEY
        )
        self._state_lock = asyncio.Lock()
        self._state_cache: dict[str, Any] | None = None
        self._op_locks: dict[str, asyncio.Lock] = {}

    async def async_open_door_index(
        self,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        token: str,
        config: PeerDoorConfig,
        door_index: int,
        output_index: int = 1,
    ) -> None:
        """Open `door_index` (1/2) using peer-aware logic."""
        if not config.call_target:
            raise ValueError("call_target is empty")
        if int(config.door_count) < 1:
            raise ValueError("door_count must be >= 1")
        if int(door_index) < 1 or int(door_index) > int(config.door_count):
            raise ValueError("door_index must be in 1..door_count")
        if int(config.door_count) > 2:
            raise ValueError(
                "peer-aware door opening currently supports door_count <= 2"
            )

        key = _state_key(host, int(port), config.call_target)
        lock = self._op_locks.setdefault(key, asyncio.Lock())

        async with lock:
            entry = await self._async_get_state_entry(key)
            now = float(time.time())

            remaining_peer = _cam2_peer_remaining(
                entry, now=now, window_s=PEER_FALLBACK_WINDOW_S
            )
            cam2_peer_active = remaining_peer > 0.0

            remaining_reuse = _cam2_peer_remaining(
                entry, now=now, window_s=float(config.cam2_reuse_window)
            )
            cam2_reuse_active = remaining_reuse > 0.0

            last_req_door, last_req_at = _last_door_request(entry)
            door2_recent_req = (
                last_req_door == 2
                and last_req_at is not None
                and (now - float(last_req_at)) <= float(PEER_FALLBACK_WINDOW_S)
            )

            _LOGGER.info(
                "Peer-door open: target=%s door=%s cam2_peer_active=%s cam2_reuse_active=%s remaining_peer=%.1fs reuse_window=%.1fs",
                config.call_target,
                int(door_index),
                cam2_peer_active,
                cam2_reuse_active,
                max(0.0, float(remaining_peer)),
                float(config.cam2_reuse_window),
            )

            if int(door_index) == 1:
                if cam2_peer_active or door2_recent_req:
                    wait_s = max(0.0, float(remaining_peer))
                    if wait_s <= 0.0 and last_req_at is not None:
                        wait_s = max(
                            0.0,
                            float(PEER_FALLBACK_WINDOW_S) - (now - float(last_req_at)),
                        )
                    if wait_s > 0.0:
                        _LOGGER.info(
                            "Door1 requested shortly after door2 -> waiting %.1fs for fallback",
                            wait_s,
                        )
                        await asyncio.sleep(float(wait_s))

                await self._hass.async_add_executor_job(
                    _open_door_only_sync,
                    host,
                    int(port),
                    token,
                    config.call_target,
                    int(output_index),
                )
                await self._async_update_state(
                    key,
                    {
                        "door_last_request": 1,
                        "door_last_request_at": float(time.time()),
                    },
                )
                return

            if int(door_index) == 2:
                if not cam2_reuse_active:
                    _LOGGER.info(
                        "Door2 requested and cam2 peer not active -> setting cam2 peer"
                    )
                    cam2_at = await self._hass.async_add_executor_job(
                        _set_cam2_peer_sync, host, int(port), token, config.call_target
                    )
                    await self._async_update_state(
                        key,
                        {
                            "door_peer_cam2_at": float(cam2_at),
                        },
                    )
                    if float(config.door_delay) > 0.0:
                        _LOGGER.info(
                            "Door2: waiting configured door_delay %.1fs",
                            float(config.door_delay),
                        )
                        await asyncio.sleep(float(config.door_delay))

                await self._hass.async_add_executor_job(
                    _open_door_only_sync,
                    host,
                    int(port),
                    token,
                    config.call_target,
                    int(output_index),
                )
                await self._async_update_state(
                    key,
                    {
                        "door_last_request": 2,
                        "door_last_request_at": float(time.time()),
                    },
                )
                return

            raise ValueError("unsupported door_index")

    async def _async_get_state_entry(self, key: str) -> dict[str, Any]:
        async with self._state_lock:
            if self._state_cache is None:
                self._state_cache = await self._store.async_load() or {
                    "version": 1,
                    "targets": {},
                }
            targets = self._state_cache.get("targets")
            if not isinstance(targets, dict):
                targets = {}
                self._state_cache["targets"] = targets
            entry = targets.get(key)
            return entry if isinstance(entry, dict) else {}

    async def _async_update_state(self, key: str, patch: dict[str, Any]) -> None:
        async with self._state_lock:
            if self._state_cache is None:
                self._state_cache = await self._store.async_load() or {
                    "version": 1,
                    "targets": {},
                }
            targets = self._state_cache.get("targets")
            if not isinstance(targets, dict):
                targets = {}
                self._state_cache["targets"] = targets
            entry = targets.get(key)
            if not isinstance(entry, dict):
                entry = {}
            entry.update(patch)
            targets[key] = entry
            await self._store.async_save(self._state_cache)


def _state_key(host: str, port: int, call_target: str) -> str:
    return f"{host}:{int(port)}|{call_target}"


def _get_float(entry: dict[str, Any], key: str) -> float | None:
    val = entry.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None


def _cam2_peer_remaining(
    entry: dict[str, Any], *, now: float, window_s: float
) -> float:
    ts = _get_float(entry, "door_peer_cam2_at")
    if ts is None:
        return 0.0
    return float(window_s) - (float(now) - float(ts))


def _last_door_request(entry: dict[str, Any]) -> tuple[int | None, float | None]:
    try:
        door = int(entry.get("door_last_request"))
    except Exception:
        door = None
    return door, _get_float(entry, "door_last_request_at")


# ---- Door-open only (tap framing) ------------------------------------------


def _hdr(length: int, ctrl: int) -> bytes:
    return (
        struct.pack("<HHH", VIPER_MAGIC, int(length) & 0xFFFF, int(ctrl) & 0xFFFF)
        + b"\x00\x00"
    )


def _addr10(addr: str) -> bytes:
    raw = addr.encode("ascii", errors="strict")
    if len(raw) > 10:
        raise ValueError(f"addr too long: {addr!r}")
    return raw.ljust(10, b"\x00")[:10]


def _tap(
    *, ctpp_ctrl: int, opcode: int, token: bytes, payload: bytes, dst: str, src: str
) -> bytes:
    if len(token) != 4:
        raise ValueError("token must be 4 bytes")
    payload_len = len(payload)
    pad = (4 - (payload_len % 4)) % 4
    body = bytearray()
    body += struct.pack("<H", int(opcode) & 0xFFFF)
    body += token
    body += (payload_len & 0x7FF).to_bytes(2, "big")
    body += payload
    if pad:
        body += b"\x00" * pad
    body += b"\xff\xff\xff\xff" + _addr10(dst) + _addr10(src)
    return _hdr(len(body), int(ctpp_ctrl) & 0xFFFF) + bytes(body)


def _build_open(channel: str, ctrl: int, extra: str | None = None) -> bytes:
    if len(channel) != 4 or not channel.isascii():
        raise ValueError("channel must be 4 ASCII chars (e.g. UAUT/UCFG/CTPP)")
    body = bytearray()
    body += struct.pack("<HHI", CMD_OPEN, 1, CHANNEL_TYPE)
    body += channel.encode("ascii", errors="strict")
    body += struct.pack("<H", int(ctrl) & 0xFFFF)
    if extra is None:
        body += b"\x00"
    else:
        extra_b = extra.encode("ascii", errors="strict") + b"\x00"
        body += b"\x00\x00"
        body += struct.pack("<I", len(extra_b))
        body += extra_b
    return _hdr(len(body), 0) + bytes(body)


def _build_json(ctrl: int, obj: dict[str, Any]) -> bytes:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return _hdr(len(raw), int(ctrl) & 0xFFFF) + raw


@dataclass
class _Frame:
    ctrl: int
    body: bytes


class _ViperDoorSession:
    def __init__(self, host: str, port: int, *, token: str) -> None:
        self.host = host
        self.port = int(port)
        self.token = str(token or "")
        self.sock: socket.socket | None = None
        self._rx = bytearray()
        self._ctpp_tx_token: int | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self.sock.settimeout(2.0)

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("not connected")
        self.sock.sendall(data)

    def _recv_into_buf(self) -> bool:
        if self.sock is None:
            raise RuntimeError("not connected")
        try:
            chunk = self.sock.recv(65536)
        except TimeoutError:
            return False
        if not chunk:
            return False
        self._rx += chunk
        return True

    def recv_frame(self, timeout_s: float = 2.0) -> _Frame | None:
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if len(self._rx) >= 8:
                if self._rx[0:2] != b"\x00\x06":
                    try:
                        i = self._rx.index(0x00)
                        del self._rx[:i]
                    except Exception:
                        self._rx.clear()
                        continue
                if len(self._rx) < 8:
                    continue
                body_len = struct.unpack("<H", self._rx[2:4])[0]
                ctrl = struct.unpack("<H", self._rx[4:6])[0]
                need = 8 + int(body_len)
                if len(self._rx) >= need:
                    body = bytes(self._rx[8:need])
                    del self._rx[:need]
                    return _Frame(ctrl=int(ctrl) & 0xFFFF, body=body)
            self._recv_into_buf()
        return None

    def _next_token(self) -> bytes:
        if self._ctpp_tx_token is None:
            self._ctpp_tx_token = random.getrandbits(32) & 0xFFFFFFFF
        else:
            self._ctpp_tx_token = (int(self._ctpp_tx_token) + 1) & 0xFFFFFFFF
        return struct.pack("<I", int(self._ctpp_tx_token) & 0xFFFFFFFF)

    def _open_channel(self, name: str, ctrl: int, extra: str | None = None) -> None:
        self._send(_build_open(name, ctrl, extra))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            frm = self.recv_frame(timeout_s=0.5)
            if not frm or frm.ctrl != 0:
                continue
            if frm.body[:2] == struct.pack("<H", CMD_OPEN):
                return

    def _send_and_wait_json(
        self, ctrl: int, req: dict[str, Any], timeout_s: float = 4.0
    ) -> dict[str, Any]:
        self._send(_build_json(ctrl, req))
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            frm = self.recv_frame(timeout_s=0.5)
            if not frm:
                continue
            if frm.ctrl != (int(ctrl) & 0xFFFF):
                continue
            if frm.body[:1] != b"{":
                continue
            try:
                data = json.loads(frm.body.decode("utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        raise TimeoutError(f"no JSON response on ctrl=0x{ctrl:04x}")

    def _authenticate(self, ctrl: int) -> None:
        self._open_channel("UAUT", ctrl)
        resp = self._send_and_wait_json(
            ctrl,
            {
                "message": "access",
                "user-token": self.token,
                "message-type": "request",
                "message-id": 2,
            },
            timeout_s=4.0,
        )
        if int(resp.get("response-code", 0) or 0) != 200:
            raise RuntimeError(f"auth failed: {resp!r}")

    def _get_vip_identity(self, ctrl: int) -> tuple[str, int]:
        self._open_channel("UCFG", ctrl)
        resp = self._send_and_wait_json(
            ctrl,
            {
                "message": "get-configuration",
                "addressbooks": "none",
                "message-type": "request",
                "message-id": 3,
            },
            timeout_s=4.0,
        )
        vip = resp.get("vip") if isinstance(resp, dict) else None
        if not isinstance(vip, dict):
            raise RuntimeError(f"missing vip config in response: {resp!r}")
        apt = str(vip.get("apt-address") or "").strip()
        sub = vip.get("apt-subaddress")
        if not apt or not isinstance(sub, int):
            raise RuntimeError(f"invalid vip identity: {vip!r}")
        return apt, int(sub)

    def open_door_only(self, *, door_addr: str, output_index: int) -> None:
        door_addr = str(door_addr or "").strip()
        if not door_addr:
            raise ValueError("door_addr is empty")
        out_idx = int(output_index)
        if not (1 <= out_idx <= 255):
            raise ValueError("output_index must be 1..255")

        ctrl_base = random.randint(0x4B00, 0x7FF0)
        uaut_ctrl = ctrl_base
        ucfg_ctrl = (ctrl_base + 1) & 0xFFFF
        ctpp_ctrl = (ctrl_base + 2) & 0xFFFF
        cspb_ctrl = (ctrl_base + 3) & 0xFFFF
        info_ctrl = (ctrl_base + 4) & 0xFFFF

        self._authenticate(uaut_ctrl)
        vip_base, apt_sub = self._get_vip_identity(ucfg_ctrl)
        apt_full = f"{vip_base}{apt_sub}"

        self._open_channel("CTPP", ctpp_ctrl, extra=apt_full)
        self._open_channel("CSPB", cspb_ctrl)
        self._open_channel("INFO", info_ctrl)

        reg_cid = random.getrandbits(16) & 0xFFFF
        reg_payload = (
            b"\x00\x40"
            + struct.pack("<H", int(reg_cid) & 0xFFFF)
            + _addr10(apt_full)
            + struct.pack("<H", 0x0E10)
            + b"\x00"
        )
        pkt_reg = _tap(
            ctpp_ctrl=ctpp_ctrl,
            opcode=0x18C0,
            token=self._next_token(),
            payload=reg_payload,
            dst=apt_full,
            src=vip_base,
        )
        self._send(pkt_reg)
        time.sleep(0.20)

        dst_addr = f"{vip_base}{out_idx}"
        op_payload = b"\x00\x2d" + _addr10(door_addr) + bytes([out_idx & 0xFF])

        token_door = self._next_token()
        token_init = self._next_token()
        pkt_1800_a = _tap(
            ctpp_ctrl=ctpp_ctrl,
            opcode=0x1800,
            token=token_door,
            payload=b"",
            dst=dst_addr,
            src=door_addr,
        )
        pkt_1820_a = _tap(
            ctpp_ctrl=ctpp_ctrl,
            opcode=0x1820,
            token=token_door,
            payload=b"",
            dst=dst_addr,
            src=door_addr,
        )
        pkt_init = _tap(
            ctpp_ctrl=ctpp_ctrl,
            opcode=0x18C0,
            token=token_init,
            payload=op_payload,
            dst=dst_addr,
            src=door_addr,
        )
        pkt_1800_b = _tap(
            ctpp_ctrl=ctpp_ctrl,
            opcode=0x1800,
            token=token_door,
            payload=b"",
            dst=dst_addr,
            src=door_addr,
        )
        pkt_1820_b = _tap(
            ctpp_ctrl=ctpp_ctrl,
            opcode=0x1820,
            token=token_door,
            payload=b"",
            dst=dst_addr,
            src=door_addr,
        )
        self._send(pkt_1800_a + pkt_1820_a + pkt_init + pkt_1800_b + pkt_1820_b)


def _open_door_only_sync(
    host: str, port: int, token: str, door_addr: str, output_index: int
) -> None:
    sess = _ViperDoorSession(host, int(port), token=str(token or ""))
    try:
        sess.connect()
        sess.open_door_only(door_addr=str(door_addr), output_index=int(output_index))
        time.sleep(1.0)
    finally:
        sess.close()


# ---- Minimal call-restart cam2 peer setter ---------------------------------


@dataclass
class _RtpInfo:
    ssrc: int


def _looks_like_rtcp(pkt: bytes) -> bool:
    if not pkt or len(pkt) < 8:
        return False
    if (pkt[0] >> 6) != 2:
        return False
    pt = pkt[1] & 0xFF
    if pt not in _RTCP_PACKET_TYPES:
        return False
    try:
        words_minus1 = struct.unpack(">H", pkt[2:4])[0]
    except struct.error:
        return False
    rtcp_len = (int(words_minus1) + 1) * 4
    return 8 <= rtcp_len <= len(pkt)


def _parse_rtp(pkt: bytes) -> _RtpInfo | None:
    if not pkt or len(pkt) < 12:
        return None
    if _looks_like_rtcp(pkt):
        return None
    if (pkt[0] >> 6) != 2:
        return None
    try:
        ssrc = struct.unpack(">I", pkt[8:12])[0]
    except struct.error:
        return None
    return _RtpInfo(ssrc=int(ssrc) & 0xFFFFFFFF)


class _ViperMini:
    def __init__(self, host: str, port: int, *, user_token: str) -> None:
        self.host = str(host)
        self.port = int(port)
        self.user_token = str(user_token or "")

        ctrl_base = random.SystemRandom().randrange(0x1000, 0x7F00) & 0xFFFE
        self._req = (int(ctrl_base) - 1) & 0xFFFF

        self.tcp: socket.socket | None = None
        self.udp: socket.socket | None = None
        self._rx = bytearray()

        self.channels: dict[str, int] = {}
        self._open_resp: dict[int, bytes] = {}

        self.config: dict[str, Any] | None = None
        self._vip_base: str | None = None
        self._apt_full: str | None = None

        self.udp_lane_id: int | None = None
        self._udp_flag = 0x00
        self._udp_seq = 0xF7
        self._udp_last_send = 0.0
        self._udp_first_send: float | None = None
        self._udp_intervals_sent = 0
        self._udp_keepalive_done = False
        self._udp_saw_ack = False

        self._udp_keep_ctrl: int | None = None
        self._rtp_audio_ctrl: int | None = None
        self._rtp_video_ctrl: int | None = None

        # Call state
        self._target: str | None = None
        self._call_stage = "idle"
        self._call_id: int | None = None
        self._call_prefix: bytes | None = None
        self._call_seq: int | None = None
        self._call_token: bytes | None = None
        self._last_peer_token: bytes | None = None
        self._last_peer_token_at: float | None = None
        self._wait_empty_at: float | None = None
        self._pending_video_token: bytes | None = None
        self._video_mediareq_sent_at: float | None = None

        self._saw_video_rtp = False
        self._video_ssrc: int | None = None
        self._video_transport: str | None = None
        self._rtcp_sender_ssrc: int = random.getrandbits(32) & 0xFFFFFFFF
        self._pli_remaining = 0
        self._pli_next_at = 0.0

    def _next_ctrl(self) -> int:
        self._req = (self._req + 1) & 0xFFFF
        return int(self._req) & 0xFFFF

    @staticmethod
    def _hdr(body_len: int, ctrl: int) -> bytes:
        return struct.pack(
            "<HHHH", VIPER_MAGIC, int(body_len) & 0xFFFF, int(ctrl) & 0xFFFF, 0
        )

    def connect(self) -> None:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp.settimeout(10.0)
        tcp.connect((self.host, self.port))
        self.tcp = tcp

    def close(self) -> None:
        if self.tcp is not None:
            try:
                self.tcp.close()
            except Exception:
                pass
        self.tcp = None
        if self.udp is not None:
            try:
                self.udp.close()
            except Exception:
                pass
        self.udp = None
        self._rx = bytearray()

    def _send_tcp(self, data: bytes) -> None:
        if not self.tcp:
            raise RuntimeError("tcp is not connected")
        self.tcp.sendall(data)

    def _recv_frame(self, timeout: float) -> tuple[int, bytes] | None:
        if not self.tcp:
            return None
        self.tcp.settimeout(max(float(timeout), 0.0))
        deadline = time.monotonic() + max(float(timeout), 0.0)
        buf = self._rx

        while True:
            if len(buf) >= 8:
                magic, body_len, ctrl, _ = struct.unpack("<HHHH", buf[:8])
                if magic != VIPER_MAGIC:
                    idx = buf.find(b"\x00\x06", 1)
                    if idx == -1:
                        del buf[:-1]
                    else:
                        del buf[:idx]
                    continue
                total = 8 + int(body_len)
                if len(buf) >= total:
                    frame = bytes(buf[:total])
                    del buf[:total]
                    return int(ctrl) & 0xFFFF, frame[8:]
            if float(timeout) > 0 and time.monotonic() >= deadline:
                return None
            try:
                chunk = self.tcp.recv(4096)
            except TimeoutError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)

    def _build_open(
        self, name: str, ctrl: int, *, extra: bytes | None = None, tail: int = 0x00
    ) -> bytes:
        body = struct.pack("<HH", CMD_OPEN, 1)
        body += struct.pack("<I", CHANNEL_TYPE)
        body += name.encode("ascii")
        body += struct.pack("<H", int(ctrl) & 0xFFFF)
        if extra:
            body += b"\x00\x00" + struct.pack("<I", len(extra)) + extra
        else:
            body += bytes([int(tail) & 0xFF])
        return self._hdr(len(body), 0) + body

    def _handle_cmd(self, body: bytes) -> None:
        if len(body) < 12:
            return
        cmd, ver = struct.unpack_from("<HH", body, 0)
        if cmd != CMD_OPEN:
            return
        if ver == 1 and len(body) >= 14:
            req_ctrl = struct.unpack_from("<H", body, 12)[0]
            resp = struct.pack("<HHIHH", CMD_OPEN, 2, 4, req_ctrl & 0xFFFF, 0)
            self._send_tcp(self._hdr(len(resp), 0) + resp)
            return
        if ver == 2:
            resp_ctrl = struct.unpack_from("<H", body, 8)[0]
            extra = b""
            if len(body) >= 16:
                extra_len = struct.unpack_from("<I", body, 12)[0]
                if 16 + int(extra_len) <= len(body):
                    extra = body[16 : 16 + int(extra_len)]
            self._open_resp[int(resp_ctrl) & 0xFFFF] = bytes(extra)

    def _open_wait(
        self,
        name: str,
        *,
        extra: bytes | None = None,
        tail: int = 0x00,
        timeout_s: float = 2.0,
    ) -> int:
        ctrl = self._next_ctrl()
        self._send_tcp(self._build_open(name, ctrl, extra=extra, tail=tail))
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            frm = self._recv_frame(timeout=0.4)
            if not frm:
                continue
            c, b = frm
            if c == 0:
                self._handle_cmd(b)
                if (int(ctrl) & 0xFFFF) in self._open_resp:
                    break
        self.channels[name] = int(ctrl) & 0xFFFF
        return int(ctrl) & 0xFFFF

    def _send_json_raw(self, ctrl: int, json_bytes: bytes) -> None:
        self._send_tcp(self._hdr(len(json_bytes), ctrl) + json_bytes)

    def _read_json_any(self, timeout_s: float = 2.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            frm = self._recv_frame(timeout=0.4)
            if not frm:
                continue
            ctrl, body = frm
            if ctrl == 0:
                self._handle_cmd(body)
                continue
            if body[:1] != b"{":
                continue
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                continue
            return data if isinstance(data, dict) else None
        return None

    @staticmethod
    def _addr10(addr: str) -> bytes:
        raw = addr.encode("ascii", errors="strict")
        if len(raw) > 10:
            raise ValueError(f"addr too long: {addr!r}")
        return raw.ljust(10, b"\x00")[:10]

    def _tap(
        self,
        ctpp_ctrl: int,
        *,
        opcode: int,
        token: bytes,
        payload: bytes,
        dst: str,
        src: str,
    ) -> bytes:
        if len(token) != 4:
            raise ValueError("token must be 4 bytes")
        payload_len = len(payload)
        pad = (4 - (payload_len % 4)) % 4
        body = bytearray()
        body += struct.pack("<H", int(opcode) & 0xFFFF)
        body += token
        body += (payload_len & 0x7FF).to_bytes(2, "big")
        body += payload
        if pad:
            body += b"\x00" * pad
        body += b"\xff\xff\xff\xff" + self._addr10(dst) + self._addr10(src)
        return self._hdr(len(body), ctpp_ctrl) + bytes(body)

    @staticmethod
    def _parse_tap(body: bytes) -> dict[str, Any] | None:
        if not body or len(body) < 32:
            return None
        if body[1] != 0x18:
            return None
        payload_len = int.from_bytes(body[6:8], "big") & 0x7FF
        if len(body) < 8 + payload_len + 24:
            return None
        addr_off = len(body) - 24
        dst_raw = body[addr_off + 4 : addr_off + 14]
        src_raw = body[addr_off + 14 : addr_off + 24]
        dst = dst_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        src = src_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        return {
            "opcode": struct.unpack("<H", body[:2])[0],
            "token": body[2:6],
            "payload": body[8 : 8 + payload_len],
            "dst": dst,
            "src": src,
        }

    def setup_and_get_apt(self) -> str:
        info_ctrl = self._open_wait("INFO")
        self._send_json_raw(
            info_ctrl,
            b'{ "message": "server-info", "message-type": "request", "message-id": 160 }',
        )
        self._read_json_any(timeout_s=2.0)

        uaut_ctrl = self._open_wait("UAUT")
        auth = (
            f'{{ "message": "access", "message-type": "request", "message-id": 150, "user-token" : "{self.user_token}" }}'
        ).encode()
        self._send_json_raw(uaut_ctrl, auth)
        resp = self._read_json_any(timeout_s=4.0) or {}
        if int(resp.get("response-code", 0) or 0) != 200:
            raise RuntimeError(f"auth failed: {resp!r}")

        ucfg_ctrl = self._next_ctrl()
        open_ucfg = self._build_open("UCFG", ucfg_ctrl, tail=0x00)
        get_cfg = b'{"message":"get-configuration","addressbooks":"all","message-type":"request","message-id":100}'
        self._send_tcp(open_ucfg + self._hdr(len(get_cfg), ucfg_ctrl) + get_cfg)
        self.channels["UCFG"] = int(ucfg_ctrl) & 0xFFFF

        deadline = time.monotonic() + 10.0
        cfg: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            frm = self._recv_frame(timeout=0.6)
            if not frm:
                continue
            ctrl, body = frm
            if ctrl == 0:
                self._handle_cmd(body)
                continue
            if body[:1] != b"{":
                continue
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                continue
            if isinstance(parsed, dict):
                cfg = parsed
                break
        if not isinstance(cfg, dict):
            raise RuntimeError("get-configuration failed (no JSON)")

        self.config = dict(cfg)
        vip = cfg.get("vip") if isinstance(cfg.get("vip"), dict) else {}
        base = vip.get("apt-address")
        sub = vip.get("apt-subaddress")
        if not isinstance(base, str) or not base:
            raise RuntimeError(f"missing apt-address in config: {vip!r}")
        self._vip_base = str(base)
        apt = f"{base}{int(sub)}" if sub is not None else base
        self._apt_full = apt
        return apt

    def open_ctpp_echo_push_and_register(self, apt_full: str) -> None:
        ctpp_ctrl = self._next_ctrl()
        echo_ctrl = self._next_ctrl()
        push_ctrl = self._next_ctrl()

        apt_extra = apt_full.encode("ascii") + b"\x00"
        pkts = [
            self._build_open("CTPP", ctpp_ctrl, extra=apt_extra),
            self._build_open("ECHO", echo_ctrl),
            self._build_open("PUSH", push_ctrl),
        ]

        nonce = random.getrandbits(32) & 0xFFFFFFFF
        cid = random.getrandbits(16) & 0xFFFF
        parent = apt_full[:-1] if apt_full[-1:].isdigit() else apt_full
        reg_payload = (
            b"\x00\x40"
            + struct.pack("<H", cid)
            + self._addr10(apt_full)
            + struct.pack("<H", 0x0E10)
            + b"\x00"
        )
        reg_token = struct.pack("<I", nonce)
        reg = self._tap(
            ctpp_ctrl,
            opcode=0x18C0,
            token=reg_token,
            payload=reg_payload,
            dst=apt_full,
            src=parent,
        )

        self._send_tcp(b"".join(pkts) + reg)
        self.channels["CTPP"] = int(ctpp_ctrl) & 0xFFFF
        self.channels["ECHO"] = int(echo_ctrl) & 0xFFFF
        self.channels["PUSH"] = int(push_ctrl) & 0xFFFF

        self._udp_keep_ctrl = (int(push_ctrl) + 1) & 0xFFFF
        self._rtp_audio_ctrl = (int(push_ctrl) + 2) & 0xFFFF
        self._rtp_video_ctrl = (int(push_ctrl) + 3) & 0xFFFF

    def _rebind_udp(self) -> None:
        local_ip = "0.0.0.0"
        if self.tcp is not None:
            try:
                local_ip = self.tcp.getsockname()[0]
            except Exception:
                local_ip = "0.0.0.0"
        if self.udp is not None:
            try:
                self.udp.close()
            except Exception:
                pass
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind((local_ip, 0))
        udp.settimeout(0.0)
        self.udp = udp
        self._udp_flag = 0x00
        self._udp_last_send = 0.0
        self._udp_first_send = None
        self._udp_intervals_sent = 0
        self._udp_keepalive_done = False
        self._udp_saw_ack = False

    def open_udpm_and_udp_socket(self) -> None:
        if self._udp_keep_ctrl is None:
            raise RuntimeError("PUSH not opened")
        udpm_ctrl = int(self._udp_keep_ctrl) & 0xFFFF
        self._send_tcp(self._build_open("UDPM", udpm_ctrl, tail=0x01))
        self.channels["UDPM"] = udpm_ctrl

        deadline = time.monotonic() + 2.5
        lane_id = None
        while time.monotonic() < deadline:
            frm = self._recv_frame(timeout=0.4)
            if not frm:
                continue
            ctrl, body = frm
            if ctrl != 0:
                continue
            self._handle_cmd(body)
            extra = self._open_resp.get(udpm_ctrl)
            if isinstance(extra, (bytes, bytearray)) and len(extra) >= 1:
                lane_id = extra[0] & 0xFF
                break
        if lane_id is None:
            if self.udp_lane_id is not None:
                lane_id = int(self.udp_lane_id) & 0xFF
            else:
                info_ctrl = int(self.channels.get("INFO") or 0) & 0xFFFF
                lane_id = ((info_ctrl & 0xFF) * 2) & 0xFF
        self.udp_lane_id = int(lane_id) & 0xFF

        self._rebind_udp()

    def _udp_send_keepalive(self, now: float) -> None:
        if self.udp is None or self._udp_keep_ctrl is None or self.udp_lane_id is None:
            return
        if self._udp_keepalive_done:
            return
        if float(now) - float(self._udp_last_send) < float(UDP_KEEPALIVE_INTERVAL_S):
            return
        if self._udp_first_send is None:
            self._udp_first_send = float(now)
        if int(self._udp_intervals_sent) >= int(UDP_KEEPALIVE_INTERVALS):
            self._udp_keepalive_done = True
            return

        lane = int(self.udp_lane_id) & 0xFF
        seq = int(self._udp_seq) & 0xFF
        ctrl = int(self._udp_keep_ctrl) & 0xFFFF
        base_flag = 0x01 if int(self._udp_intervals_sent) >= 2 else 0x00
        flag = 0x01 if int(self._udp_flag) == 0x01 else base_flag
        body = bytes([lane, 0x11, 0x00, seq, int(flag) & 0xFF, 0x80])
        pkt = self._hdr(len(body), ctrl) + body
        for _ in range(int(UDP_KEEPALIVE_BURST)):
            self.udp.sendto(pkt, (self.host, self.port))
        self._udp_seq = (seq + 1) & 0xFF
        self._udp_last_send = float(now)
        self._udp_intervals_sent = int(self._udp_intervals_sent) + 1

        if (
            self._udp_flag != 0x01
            and self._udp_first_send is not None
            and float(now) - float(self._udp_first_send)
            >= float(UDP_FORCE_FLAG1_AFTER_S)
        ):
            self._udp_flag = 0x01
        if int(self._udp_intervals_sent) >= int(UDP_KEEPALIVE_INTERVALS):
            self._udp_keepalive_done = True

    def _drain_udp(self, now: float) -> None:
        if self.udp is None:
            return
        for _ in range(8):
            try:
                dat, _addr = self.udp.recvfrom(2048)
            except (BlockingIOError, TimeoutError):
                return
            except OSError:
                return
            if not dat or len(dat) < 8:
                continue
            try:
                _magic, _blen, ctrl, _z = struct.unpack("<HHHH", dat[:8])
            except struct.error:
                continue
            body = dat[8:]
            ctrl = int(ctrl) & 0xFFFF
            if (
                self._udp_keep_ctrl is not None
                and ctrl == int(self._udp_keep_ctrl)
                and len(body) == 6
            ):
                if body[1] == 0x11 and body[2] == 0x01:
                    self._udp_flag = 0x01
                    self._udp_saw_ack = True
                continue
            if self._rtp_video_ctrl is not None and ctrl == int(self._rtp_video_ctrl):
                rtp = _parse_rtp(body)
                if rtp is not None:
                    self._saw_video_rtp = True
                    self._video_ssrc = int(rtp.ssrc) & 0xFFFFFFFF
                    if self._video_transport is None:
                        self._video_transport = "udp"

    def _reset_call(self, *, apt_full: str, target: str) -> None:
        self._apt_full = apt_full
        self._target = target
        self._call_stage = "wait_start_ack_empty"
        self._call_id = random.getrandbits(32) & 0xFFFFFFFF

        cid = random.SystemRandom().randrange(1, 0x7FFF) & 0x7FFF
        lane = int(self.udp_lane_id if self.udp_lane_id is not None else 0) & 0xFF
        seq = random.getrandbits(8) & 0xFF
        self._call_prefix = bytes([(cid >> 8) & 0x7F, cid & 0xFF, lane])
        self._call_seq = int(seq)

        self._call_token = None
        self._last_peer_token = None
        self._last_peer_token_at = None
        self._wait_empty_at = None
        self._pending_video_token = None
        self._video_mediareq_sent_at = None
        self._saw_video_rtp = False
        self._video_transport = None

    def _start_token(self) -> bytes:
        if self._call_prefix is None or self._call_seq is None:
            raise RuntimeError("call tokens not initialized")
        tok = self._call_prefix + bytes([int(self._call_seq) & 0xFF])
        self._call_seq = (int(self._call_seq) + 1) & 0xFF
        return tok

    def _token_from_peer(self, peer_token: bytes, *, add_one: bool) -> bytes:
        if len(peer_token) != 4:
            raise ValueError("peer token must be 4 bytes")
        cid_hi = peer_token[0] & 0x7F
        cid_lo = peer_token[1] & 0xFF
        a = peer_token[3] & 0xFF
        b = (peer_token[2] + (1 if add_one else 0)) & 0xFF
        tok = bytes([cid_hi, cid_lo, a, b])
        self._call_token = tok
        self._last_peer_token = bytes(peer_token)
        self._last_peer_token_at = time.monotonic()
        return tok

    def _token_bump(self) -> bytes:
        tok = self._call_token
        if not isinstance(tok, (bytes, bytearray)) or len(tok) != 4:
            tok = self._start_token()
            self._call_token = tok
            return tok
        return bytes([tok[0] & 0x7F, tok[1], tok[2], (tok[3] + 1) & 0xFF])

    def _next_call_token_for_tx(self) -> bytes:
        tok = self._call_token
        if not isinstance(tok, (bytes, bytearray)) or len(tok) != 4:
            tok = self._start_token()
        else:
            tok = bytes([tok[0] & 0x7F, tok[1], tok[2], (tok[3] + 1) & 0xFF])
        self._call_token = bytes(tok)
        return bytes(tok)

    def start_call(self, *, apt_full: str, target: str) -> None:
        ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
        if not ctpp_ctrl:
            raise RuntimeError("CTPP not opened")
        self._reset_call(apt_full=apt_full, target=target)
        ours10 = self._addr10(apt_full)
        tgt10 = self._addr10(target)
        call_id = int(self._call_id or 0) & 0xFFFFFFFF

        payload = bytearray(40)
        payload[0:2] = b"\x00\x01"
        payload[2:12] = ours10
        payload[12:22] = tgt10
        payload[22] = 0x01
        payload[23] = 0x40
        payload[24:28] = struct.pack("<I", call_id)
        payload[28:38] = ours10
        payload[38] = 0x49
        payload[39] = 0x49

        tok = self._start_token()
        pkt = self._tap(
            ctpp_ctrl,
            opcode=0x18C0,
            token=tok,
            payload=bytes(payload),
            dst=apt_full,
            src=target,
        )
        self._send_tcp(pkt)

    def send_release(self) -> None:
        ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
        if not ctpp_ctrl or not self._apt_full or not self._target:
            return
        tok = self._next_call_token_for_tx()
        payload = b"\x00\x0e\x00"
        pkt = self._tap(
            ctpp_ctrl,
            opcode=0x1840,
            token=tok,
            payload=payload,
            dst=self._apt_full,
            src=self._target,
        )
        self._send_tcp(pkt)
        self._call_stage = "idle"

    def _open_rtpc(self) -> None:
        if self._rtp_audio_ctrl is None or self._rtp_video_ctrl is None:
            return
        pkts = [
            self._build_open("RTPC", int(self._rtp_audio_ctrl) & 0xFFFF, tail=0x01),
            self._build_open("RTPC", int(self._rtp_video_ctrl) & 0xFFFF, tail=0x01),
        ]
        self._send_tcp(b"".join(pkts))

    def _maybe_force_video_mediareq(self) -> None:
        if self._call_stage != "wait_peer_empty_before_video":
            return
        if self._video_mediareq_sent_at is not None:
            return
        if self._wait_empty_at is None or self._pending_video_token is None:
            return
        if (time.monotonic() - float(self._wait_empty_at)) < 0.45:
            return
        if self._rtp_video_ctrl is None or not self._apt_full or not self._target:
            return
        ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
        tok = bytes(self._pending_video_token)
        payload = (
            b"\x00\x11"
            + bytes([0x14, 0x02])
            + struct.pack("<I", 0)
            + struct.pack("<H", int(self._rtp_video_ctrl) & 0xFFFF)
            + struct.pack("<H", 0xFFFF)
            + struct.pack("<I", int(VIDEO_BITRATE_KBPS) & 0xFFFFFFFF)
        )
        pkt = self._tap(
            ctpp_ctrl,
            opcode=0x1840,
            token=tok,
            payload=payload,
            dst=self._apt_full,
            src=self._target,
        )
        self._send_tcp(pkt)
        self._video_mediareq_sent_at = time.monotonic()
        self._call_stage = "sent_video_mediareq"
        self._pending_video_token = None
        self._pli_remaining = 4
        self._pli_next_at = time.monotonic() + 0.02

    def _handle_tap(self, tap: dict[str, Any]) -> None:
        if not self._apt_full or not self._target:
            return
        opcode = int(tap.get("opcode") or 0) & 0xFFFF
        token = bytes(tap.get("token") or b"")
        payload = bytes(tap.get("payload") or b"")
        dst = str(tap.get("dst") or "")
        src = str(tap.get("src") or "")

        incoming = (dst == self._apt_full and src == self._target) or (
            dst == self._target and src == self._apt_full
        )
        peer = bool(incoming and len(token) == 4 and (token[0] & 0x80))
        if peer:
            self._last_peer_token = token
            self._last_peer_token_at = time.monotonic()

        msg_type = payload[1] if len(payload) >= 2 and payload[0] == 0x00 else None

        if msg_type == 0x40 and len(payload) >= 17 and peer:
            cid = struct.unpack("<H", payload[2:4])[0]
            logaddr10 = payload[4:14]
            res = b"\x00\x41" + struct.pack("<H", cid) + logaddr10 + b"\x00\x00"
            tok = self._token_bump()
            ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
            pkt = self._tap(
                ctpp_ctrl,
                opcode=0x18C0,
                token=tok,
                payload=res,
                dst=self._apt_full,
                src=self._target,
            )
            self._send_tcp(pkt)
            return

        if not peer:
            return

        ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
        if not ctpp_ctrl:
            return

        stage = str(self._call_stage)
        if stage == "wait_start_ack_empty":
            if opcode == 0x1800 and not payload:
                tok = self._token_from_peer(token, add_one=False)
                capab = b"\x00\x03" + bytes([0x49, 0x00]) + struct.pack("<I", 0x27)
                pkt = self._tap(
                    ctpp_ctrl,
                    opcode=0x1840,
                    token=tok,
                    payload=capab,
                    dst=self._apt_full,
                    src=self._target,
                )
                self._send_tcp(pkt)
                self._call_stage = "wait_peer_capab"
            return
        if stage == "wait_peer_capab":
            if msg_type == 0x03:
                tok = self._token_from_peer(token, add_one=True)
                pkt = self._tap(
                    ctpp_ctrl,
                    opcode=0x1800,
                    token=tok,
                    payload=b"",
                    dst=self._apt_full,
                    src=self._target,
                )
                self._send_tcp(pkt)
                self._call_stage = "wait_peer_connect"
            return
        if stage == "wait_peer_connect":
            if msg_type == 0x0C:
                tok = self._token_from_peer(token, add_one=True)
                pkt = self._tap(
                    ctpp_ctrl,
                    opcode=0x1800,
                    token=tok,
                    payload=b"",
                    dst=self._apt_full,
                    src=self._target,
                )
                self._send_tcp(pkt)
                self._open_rtpc()
                audio_ctrl = int(self._rtp_audio_ctrl or 0) & 0xFFFF
                media = (
                    b"\x00\x11"
                    + bytes([0x18, 0x02])
                    + struct.pack("<I", 0)
                    + struct.pack("<H", audio_ctrl)
                )
                pkt2 = self._tap(
                    ctpp_ctrl,
                    opcode=0x1840,
                    token=tok,
                    payload=media,
                    dst=self._apt_full,
                    src=self._target,
                )
                self._send_tcp(pkt2)
                self._call_stage = "wait_peer_mediareq"
            return
        if stage == "wait_peer_mediareq":
            if msg_type == 0x11:
                tok = self._token_from_peer(token, add_one=True)
                pkt = self._tap(
                    ctpp_ctrl,
                    opcode=0x1800,
                    token=tok,
                    payload=b"",
                    dst=self._apt_full,
                    src=self._target,
                )
                self._send_tcp(pkt)
                self._wait_empty_at = time.monotonic()
                self._pending_video_token = bytes(tok)
                self._call_stage = "wait_peer_empty_before_video"
            return
        if stage == "wait_peer_empty_before_video":
            if opcode == 0x1800 and not payload:
                tok = self._token_from_peer(token, add_one=True)
                vctrl = int(self._rtp_video_ctrl or 0) & 0xFFFF
                media = (
                    b"\x00\x11"
                    + bytes([0x14, 0x02])
                    + struct.pack("<I", 0)
                    + struct.pack("<H", vctrl)
                    + struct.pack("<H", 0xFFFF)
                    + struct.pack("<I", int(VIDEO_BITRATE_KBPS) & 0xFFFFFFFF)
                )
                pkt = self._tap(
                    ctpp_ctrl,
                    opcode=0x1840,
                    token=tok,
                    payload=media,
                    dst=self._apt_full,
                    src=self._target,
                )
                self._send_tcp(pkt)
                self._video_mediareq_sent_at = time.monotonic()
                self._call_stage = "sent_video_mediareq"
                self._pli_remaining = 4
                self._pli_next_at = time.monotonic() + 0.02
            return

    def _handle_tcp_frame(self, ctrl: int, body: bytes, *, now: float) -> None:
        if ctrl == 0:
            self._handle_cmd(body)
            return
        ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
        if ctpp_ctrl and ctrl == ctpp_ctrl:
            tap = self._parse_tap(body)
            if tap:
                self._handle_tap(tap)
            return
        if self._rtp_video_ctrl is not None and ctrl == int(self._rtp_video_ctrl):
            rtp = _parse_rtp(body)
            if rtp is not None:
                self._saw_video_rtp = True
                self._video_ssrc = int(rtp.ssrc) & 0xFFFFFFFF
                if self._video_transport is None:
                    self._video_transport = "tcp"

    def _maybe_send_pli(self, now: float) -> None:
        if self._pli_remaining <= 0:
            return
        if self._rtp_video_ctrl is None or self._video_ssrc is None:
            return
        if float(now) < float(self._pli_next_at):
            return
        rtcp = struct.pack(
            ">BBHII",
            0x81,
            0xCE,
            0x0002,
            int(self._rtcp_sender_ssrc) & 0xFFFFFFFF,
            int(self._video_ssrc) & 0xFFFFFFFF,
        )
        ctrl = int(self._rtp_video_ctrl) & 0xFFFF
        try:
            self._send_tcp(self._hdr(len(rtcp), ctrl) + rtcp)
        except Exception:
            return
        self._pli_remaining = max(int(self._pli_remaining) - 1, 0)
        self._pli_next_at = float(now) + 0.25

    def run_until_video_flowing(self, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            now = time.monotonic()
            self._udp_send_keepalive(now)
            self._maybe_force_video_mediareq()
            self._maybe_send_pli(now)
            frm = self._recv_frame(timeout=0.05)
            if frm:
                ctrl, body = frm
                self._handle_tcp_frame(ctrl, body, now=now)
            self._drain_udp(now=now)
            if self._call_stage == "sent_video_mediareq" and self._saw_video_rtp:
                return True
        return False

    def _pump(self, *, seconds: float) -> None:
        deadline = time.monotonic() + max(float(seconds), 0.0)
        while time.monotonic() < deadline:
            now = time.monotonic()
            self._udp_send_keepalive(now)
            frm = self._recv_frame(timeout=0.05)
            if frm:
                ctrl, body = frm
                self._handle_tcp_frame(ctrl, body, now=now)
            self._drain_udp(now=now)

    def end_call(self) -> None:
        ctpp_ctrl = int(self.channels.get("CTPP") or 0) & 0xFFFF
        if not ctpp_ctrl or not self._apt_full or not self._target:
            return
        self.send_release()
        self._pump(seconds=0.10)
        audio_ctrl = int(self._rtp_audio_ctrl or 0) & 0xFFFF
        if audio_ctrl:
            tok = self._next_call_token_for_tx()
            payload = (
                b"\x00\x11"
                + bytes([0x98, 0x02])
                + struct.pack("<I", 0)
                + struct.pack("<H", audio_ctrl)
            )
            pkt = self._tap(
                ctpp_ctrl,
                opcode=0x1840,
                token=tok,
                payload=payload,
                dst=self._apt_full,
                src=self._target,
            )
            self._send_tcp(pkt)
            self._pump(seconds=0.05)
        video_ctrl = int(self._rtp_video_ctrl or 0) & 0xFFFF
        if video_ctrl:
            tok = self._next_call_token_for_tx()
            payload = (
                b"\x00\x11"
                + bytes([0x94, 0x02])
                + struct.pack("<I", 0)
                + struct.pack("<H", video_ctrl)
            )
            pkt = self._tap(
                ctpp_ctrl,
                opcode=0x1860,
                token=tok,
                payload=payload,
                dst=self._apt_full,
                src=self._target,
            )
            self._send_tcp(pkt)
            self._pump(seconds=0.05)
        self._call_stage = "idle"


def _set_cam2_peer_sync(host: str, port: int, token: str, call_target: str) -> float:
    sess = _ViperMini(host, int(port), user_token=str(token or ""))
    try:
        sess.connect()
        apt_full = sess.setup_and_get_apt()
        sess.open_ctpp_echo_push_and_register(apt_full)
        sess.open_udpm_and_udp_socket()

        sess.start_call(apt_full=apt_full, target=str(call_target))
        if not sess.run_until_video_flowing(timeout_s=12.0):
            raise RuntimeError("call: no video flow")
        if float(CAMERA_SWITCH_WARMUP_S) > 0.0:
            sess._pump(seconds=float(CAMERA_SWITCH_WARMUP_S))

        sess.end_call()
        time.sleep(float(RESTART_DELAY_S))
        sess.open_udpm_and_udp_socket()

        sess.start_call(apt_full=apt_full, target=str(call_target))
        if not sess.run_until_video_flowing(timeout_s=20.0):
            raise RuntimeError("call: cam2 switch timeout (no video)")

        # IMPORTANT: mimic the working workflow: abort after "video flowing".
        return float(time.time())
    finally:
        try:
            sess.close()
        except Exception:
            pass
