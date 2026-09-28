"""Minimal local RTSP/RTP server for go2rtc.

Serves H.264 + G.711 PCMA over RTSP so that go2rtc (bundled in Home
Assistant) can relay the stream to the browser via WebRTC.

Supports multiple simultaneous clients (go2rtc + HA stream worker both
connect to stream_source() at the same time).  Feed tasks run from start()
to stop() and broadcast RTP to every registered client independently —
no client can steal another's data.

Transport modes:
  - TCP interleaving (RTP/AVP/TCP) — default for go2rtc and FFmpeg
  - UDP unicast (RTP/AVP) — fallback (single client)

Protocol flow (RFC 2326):
    client → OPTIONS  → 200 OK
    client → DESCRIBE → 200 OK + SDP (video H.264 PT96 + audio PCMA PT8)
    client → SETUP video → 200 OK
    client → SETUP audio → 200 OK
    client → PLAY       → 200 OK  [client registered, starts receiving RTP]
    [server broadcasts RTP until TEARDOWN or disconnect]

TCP interleaved RTP (RFC 2326 §10.12):
    $ | channel (1 byte) | length (2 bytes BE) | RTP packet

Audio keepalive:
    Between calls, silent PCMA (0xD5) is sent at the correct 20 ms cadence
    so go2rtc's WebRTC session stays alive in the browser.  Silence is
    suppressed during live calls (_ready_event set) to avoid glitches — if
    no real audio arrives within 20 ms, the loop iterates without sending.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import logging
import socket
import struct
import time

_LOGGER = logging.getLogger(__name__)

_MAX_RTP_PAYLOAD = 1400  # bytes — safe MTU headroom

# RTCP — seconds between 1900-01-01 (NTP epoch) and 1970-01-01 (Unix epoch).
# Used to encode wall-clock time into the 64-bit NTP field of Sender Reports.
_NTP_EPOCH_OFFSET = 2208988800
# Sender Report period.  RFC 3550 suggests adapting based on bandwidth, but
# 5 s is the conventional default and is enough for clients to lock their
# reference clock within the first SR.
_RTCP_SR_INTERVAL_S = 5.0

# H.264 parameter sets captured from the Comelit 6701W (baseline profile,
# level 3.1, 800x480 yuv420p — identical across every pcap we have).  Used as
# a fallback sprop-parameter-sets in the SDP so that clients which DESCRIBE
# before the first live SPS/PPS have arrived (notably HA's `stream` worker,
# which connects to the persistent RTSP server at HA startup — long before
# the first video call) still get a codec context with a known pix_fmt.
# Without this, PyAV's `add_stream_from_template` fails with
# `libx264 Invalid video pixel format: -1` on the first keyframe.
_DEFAULT_SPS = bytes.fromhex("6742001fe90283f402c4084a")
_DEFAULT_PPS = bytes.fromhex("68ce3880")


@dataclasses.dataclass
class _TcpClient:
    """Per-connection state for one RTSP/TCP client."""

    writer: asyncio.StreamWriter
    video_ch: int | None = None  # interleaved channel for video RTP
    audio_ch: int | None = None  # interleaved channel for audio RTP
    backchannel_ch: int | None = None  # browser microphone RTP
    backchannel_started: bool = False


class LocalRtspServer:
    """Minimal RTSP server that streams H.264 + G.711 PCMA.

    Supports multiple simultaneous TCP clients — each client registered on
    PLAY receives all RTP independently.  Feed tasks run permanently from
    start() and broadcast to the current client list.

    Usage:
        server = LocalRtspServer()
        url = await server.start()
        receiver.attach_rtsp_queues(server.nal_queue, server.audio_queue)
        # …later…
        await server.stop()
    """

    def __init__(self, bind_host: str = "127.0.0.1", port: int = 0) -> None:
        self._bind_host = bind_host
        self._bind_port = port
        self._rtsp_port: int = 0
        self._server: asyncio.Server | None = None

        # Incoming media queues — attached to rtp_receiver via attach_rtsp_queues.
        # Video queue carries (device_rtp_timestamp, nal_bytes) tuples so the
        # device's own 90 kHz PTS flows end-to-end instead of being fabricated.
        self.nal_queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(maxsize=300)
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        # RTP pass-through queue: raw video RTP packets forwarded with
        # header rewrite only (no NAL reassembly / FU-A re-fragmentation).
        self.rtp_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        # Backchannel queue: PCMA audio payloads received from go2rtc mic
        # input (via ANNOUNCE/RECORD).  rtp_receiver reads from this queue
        # to replace silence with real audio in _audio_send_loop.
        self.backchannel_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        # Keep normal viewing identical to the proven first camera beta:
        # video-only SDP. Audio tracks are advertised only while HA has
        # explicitly enabled a two-way call.
        self._audio_enabled = False

        # UDP sockets — fallback for clients that request UDP transport
        self._video_sock: socket.socket | None = None
        self._audio_sock: socket.socket | None = None
        self._video_server_port: int = 0
        self._audio_server_port: int = 0
        # UDP is single-client (last SETUP wins)
        self._udp_host: str | None = None
        self._udp_video_port: int = 0
        self._udp_audio_port: int = 0

        # Active TCP clients — appended on PLAY, removed on disconnect
        self._active_clients: list[_TcpClient] = []

        # Latest SPS/PPS bytes seen in the NAL stream (without start code).
        # Used to populate sprop-parameter-sets in SDP so clients know the
        # pixel format before the first keyframe arrives.  Preserved across
        # reset() so later DESCRIBEs benefit from past sessions.
        self._latest_sps: bytes = _DEFAULT_SPS
        self._latest_pps: bytes = _DEFAULT_PPS

        # RTP sequence numbers (shared across all clients).  MUST advance
        # monotonically for the lifetime of the server.
        self._video_seq: int = 0
        self._audio_seq: int = 0
        self._audio_ts: int = 0

        # Video timestamp translation: the device resets its RTP timestamp
        # per call, but the persistent HA stream worker stays connected
        # across calls and rejects backwards jumps as "Timestamp
        # discontinuity".  We translate device_ts → output_ts by adding a
        # running offset that is recalculated on each new call to keep the
        # output stream monotonic and aligned with wall clock.
        self._video_ts_out: int = 0  # last output timestamp sent
        self._video_ts_offset: int = 0  # device_ts + offset = output_ts
        self._last_device_ts: int | None = None  # last device_ts seen
        # Explicit "rebase on next frame" flag.  Set by reset() to force
        # the feed loop to recompute the offset on the next device NAL
        # regardless of what device_ts looks like — we cannot rely on the
        # backward-jump heuristic because the device may restart from a
        # higher number than it ended at.
        self._video_ts_rebase_pending: bool = False
        self._video_ssrc: int = 0xC0DE1234
        self._audio_ssrc: int = 0xA0D10001
        self._session_id: str = "87654321"

        # RTCP Sender Report state — running totals since the SSRC was
        # created.  Clients use these together with the NTP/RTP timestamp
        # pair to recover a reference clock; without them VLC, go2rtc and
        # most browsers wait many seconds before showing the first frame
        # ("no reference clock" / "PCR is called too late").  Counters are
        # zeroed in reset() so a new call presents fresh statistics.
        self._video_pkt_count: int = 0
        self._video_octet_count: int = 0
        self._audio_pkt_count: int = 0
        self._audio_octet_count: int = 0
        self._last_video_rtp_ts: int = 0
        self._last_audio_rtp_ts: int = 0

        self._running = False
        self._feed_tasks: list[asyncio.Task[None]] = []

        # Gated by the coordinator: set when a video session is producing
        # RTP, cleared during CTPP handshake and idle.  The PLAY handler
        # awaits this event (with a short timeout) before responding 200
        # OK, so HA's stream_worker stalls inside PLAY instead of erroring
        # with "Stream ended; no additional packets" and taking a 10 s
        # backoff when it reconnects into an in-flight handshake.
        self._ready_event: asyncio.Event = asyncio.Event()

        # Set by _broadcast_rtp on the first packet of each call; cleared
        # by reset() between calls.  The RTCP SR loop awaits this before
        # emitting its first Sender Report.
        self._first_media_event: asyncio.Event = asyncio.Event()

    @property
    def rtsp_url(self) -> str:
        """Return the RTSP URL that go2rtc should connect to."""
        return f"rtsp://{self._bind_host}:{self._rtsp_port}/intercom"

    async def start(self) -> str:
        """Start the RTSP server, bind UDP sockets, and start feed tasks.

        Returns the RTSP URL for the camera entity's stream_source.
        Feed tasks run until stop() — they broadcast to whoever is registered.
        """
        self._video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._video_sock.bind((self._bind_host, 0))
        self._video_server_port = self._video_sock.getsockname()[1]

        self._audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._audio_sock.bind((self._bind_host, 0))
        self._audio_server_port = self._audio_sock.getsockname()[1]

        self._server = await asyncio.start_server(
            self._handle_client,
            self._bind_host,
            self._bind_port,
        )
        self._rtsp_port = self._server.sockets[0].getsockname()[1]
        self._running = True

        # Persistent feed tasks — run for the lifetime of the server.
        # Passthrough loop is lower latency; falls back to NAL-based
        # path automatically if rtp_queue is not fed.
        self._feed_tasks = [
            asyncio.create_task(self._video_rtp_passthrough_loop()),
            asyncio.create_task(self._audio_feed_loop()),
            asyncio.create_task(self._rtcp_sr_loop()),
        ]

        _LOGGER.info("RTSP server started: %s", self.rtsp_url)
        return self.rtsp_url

    async def stop(self) -> None:
        """Stop the server, feed tasks, and all client connections."""
        self._running = False
        self._active_clients.clear()

        for task in self._feed_tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait([task], timeout=2.0)
        self._feed_tasks.clear()

        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

        for sock_attr in ("_video_sock", "_audio_sock"):
            sock = getattr(self, sock_attr)
            if sock:
                sock.close()
                setattr(self, sock_attr, None)

        _LOGGER.debug("RTSP server stopped")

    def mark_ready(self) -> None:
        """Signal that a video session is flowing — unblocks pending PLAYs."""
        self._ready_event.set()

    def mark_not_ready(self) -> None:
        """Signal that no session is flowing — future PLAYs will stall until ready."""
        self._ready_event.clear()

    def set_audio_enabled(self, enabled: bool) -> None:
        """Include exterior-audio and microphone tracks in subsequent DESCRIBEs."""
        self._audio_enabled = enabled

    def disconnect_clients(self) -> None:
        """Close all active RTSP client connections to force immediate reconnect.

        Called once a new video session is ready (first NAL received).
        go2rtc stays connected during idle but tracks only audio — when
        video frames start arriving on an existing audio-only connection,
        go2rtc can take 20+ seconds to detect the new track.  Kicking it
        forces a fresh DESCRIBE/PLAY against a stream that already has
        video flowing, so it starts presenting frames within a few seconds.
        """
        clients = list(self._active_clients)
        self._active_clients.clear()
        for c in clients:
            with contextlib.suppress(Exception):
                c.writer.close()
        if clients:
            _LOGGER.debug(
                "RTSP: disconnected %d client(s) — forcing reconnect on new video session",
                len(clients),
            )

    def reset(self, renewal: bool = False) -> None:
        """Reset for a new or renewed video call session.

        Drains stale media from existing queues (drain not replace, so
        RtpReceiver keeps pushing to the same queue objects the feed loops
        read).  NEVER resets RTP sequence/timestamp counters — the persistent
        HA stream worker stays connected across calls and any backwards jump
        causes a "Timestamp discontinuity" error.  `renewal` is kept for
        backwards compatibility with callers but no longer changes behaviour.
        """
        drained_nal = 0
        while not self.nal_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.nal_queue.get_nowait()
                drained_nal += 1
        while not self.rtp_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.rtp_queue.get_nowait()
                drained_nal += 1
        drained_audio = 0
        while not self.audio_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.audio_queue.get_nowait()
                drained_audio += 1
        while not self.backchannel_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.backchannel_queue.get_nowait()
        # Audio-video sync bootstrap: the audio feed loop has been advancing
        # `_audio_ts` by 160 per 20 ms of silence since server start, so by
        # the time the first video frame of a new call arrives, audio is N
        # seconds ahead on the muxer's output timeline.  Without correction
        # the first video output timestamp would be 0 (or a small device
        # value), leaving HA's stream worker to reconcile the gap by
        # stalling 20+ s until its buffers catch up.  Seed `_video_ts_out`
        # to the current audio clock converted to 90 kHz so the feed loop's
        # next frame lands right next to "now", and set an explicit rebase
        # flag so the loop knows to recompute the offset on the next NAL
        # regardless of the device's own timestamp value.
        audio_ts_90k = (self._audio_ts * 90000 // 8000) & 0xFFFFFFFF
        # Use whichever is higher: the audio clock (needed on first call
        # when _video_ts_out is 0) or the real last video output (needed
        # on renewal when video timestamps have advanced past the audio
        # clock).  Using the audio clock alone caused backward jumps on
        # renewal because the audio clock lags behind the video stream.
        self._video_ts_out = max(self._video_ts_out, audio_ts_90k)
        self._video_ts_rebase_pending = True

        # Reset RTCP counters — clients re-anchor their reference clock from
        # the next Sender Report regardless, but presenting fresh stats per
        # call avoids any spurious "loss" calculation on their side based on
        # cumulative deltas across an idle period.
        self._video_pkt_count = 0
        self._video_octet_count = 0
        self._audio_pkt_count = 0
        self._audio_octet_count = 0
        self._first_media_event.clear()

        # Re-prime all already-connected clients with current SPS+PPS.
        # New clients are primed in _prime_client_with_parameter_sets called
        # from PLAY, but clients that stayed connected across sessions (e.g.
        # the HA stream worker) don't reconnect and never see in-band parameter
        # sets for the new call, so libx264 gets pix_fmt=-1 on the first keyframe.
        for client in list(self._active_clients):
            self._prime_client_with_parameter_sets(client)

        _LOGGER.debug(
            "RTSP server reset (renewal=%s): drained %d NALs + %d audio, "
            "%d client(s) remain, video_ts_out seeded to 0x%08X",
            renewal,
            drained_nal,
            drained_audio,
            len(self._active_clients),
            self._video_ts_out,
        )

    # ------------------------------------------------------------------
    # RTSP request handling
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one RTSP client connection."""
        peer = writer.get_extra_info("peername")
        client_host = peer[0] if peer else "unknown"
        _LOGGER.debug("RTSP client connected from %s", client_host)

        # Disable Nagle's algorithm — send RTP packets immediately
        # instead of batching them (adds up to 40ms latency per packet)
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Per-client state — independent of every other connection
        client = _TcpClient(writer=writer)
        registered = False
        request_buffer = bytearray()

        try:
            while self._running:
                parsed = await self._read_rtsp_request(reader, request_buffer)
                if parsed is None:
                    break
                method, url, headers, cseq = parsed
                _LOGGER.debug("RTSP %s from %s", method, client_host)

                if method == "OPTIONS":
                    self._send(
                        writer,
                        cseq,
                        extra=(
                            "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, ANNOUNCE, RECORD\r\n"
                        ),
                    )
                    await writer.drain()

                elif method == "DESCRIBE":
                    sdp = self._build_sdp().encode()
                    writer.write(
                        f"RTSP/1.0 200 OK\r\n"
                        f"CSeq: {cseq}\r\n"
                        f"Content-Type: application/sdp\r\n"
                        f"Content-Length: {len(sdp)}\r\n"
                        f"\r\n".encode() + sdp
                    )
                    await writer.drain()

                elif method == "SETUP":
                    transport_hdr = headers.get("transport", "")
                    url_lower = url.lower()
                    if "/backchannel" in url_lower or "track3" in url_lower:
                        track_kind = "backchannel"
                    elif "/audio" in url_lower or "track2" in url_lower:
                        track_kind = "audio"
                    else:
                        track_kind = "video"
                    transport_resp = self._parse_setup(
                        transport_hdr, track_kind, client, client_host
                    )
                    self._send(
                        writer,
                        cseq,
                        extra=(f"Session: {self._session_id}\r\n{transport_resp}\r\n"),
                    )
                    await writer.drain()

                elif method == "PLAY":
                    # Do not let go2rtc bind a producer to an idle relay. The
                    # first camera beta waited here until real Comelit RTP was
                    # flowing; removing that gate caused clients to negotiate
                    # successfully but retain a black video track.
                    if not self._ready_event.is_set():
                        _LOGGER.debug(
                            "PLAY from %s waiting for video readiness", client_host
                        )
                        try:
                            await asyncio.wait_for(
                                self._ready_event.wait(), timeout=10.0
                            )
                        except TimeoutError:
                            writer.write(
                                f"RTSP/1.0 503 Service Unavailable\r\nCSeq: {cseq}\r\n\r\n".encode()
                            )
                            await writer.drain()
                            break
                    self._send(
                        writer,
                        cseq,
                        extra=(f"Session: {self._session_id}\r\nRange: npt=0.000-\r\n"),
                    )
                    await writer.drain()
                    self._active_clients.append(client)
                    registered = True
                    _LOGGER.info(
                        "RTSP streaming → %s (video_ch=%s audio_ch=%s) [%d client(s) total]",
                        client_host,
                        client.video_ch,
                        client.audio_ch,
                        len(self._active_clients),
                    )
                    # Immediately send in-band SPS + PPS to this client so
                    # FFmpeg's H.264 parser populates codecpar.format before
                    # the stream worker freezes its output template.  Without
                    # this, sprop-parameter-sets from SDP alone is not always
                    # enough — recent libavformat only reliably sets pix_fmt
                    # from in-band NAL units seen in the actual RTP stream.
                    self._prime_client_with_parameter_sets(client)
                    await self._wait_for_teardown(
                        reader, client, client_host, request_buffer
                    )
                    break

                elif method == "TEARDOWN":
                    self._send(writer, cseq, extra=f"Session: {self._session_id}\r\n")
                    await writer.drain()
                    break

                elif method == "ANNOUNCE":
                    # go2rtc backchannel: drain the SDP body then hand off
                    content_len = int(headers.get("content-length", 0))
                    if content_len > 0:
                        with contextlib.suppress(asyncio.IncompleteReadError):
                            await self._readexactly_buffered(
                                reader, request_buffer, content_len
                            )
                    self._send(writer, cseq)
                    await writer.drain()
                    _LOGGER.debug("Backchannel ANNOUNCE from %s", client_host)
                    await self._handle_backchannel(
                        reader, writer, client_host, request_buffer
                    )
                    break

                else:
                    writer.write(
                        f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n".encode()
                    )
                    await writer.drain()

        except (TimeoutError, ConnectionError):
            pass
        except Exception:
            _LOGGER.debug("RTSP client error", exc_info=True)
        finally:
            if registered:
                with contextlib.suppress(ValueError):
                    self._active_clients.remove(client)
                _LOGGER.debug(
                    "RTSP client disconnected from %s [%d client(s) remain]",
                    client_host,
                    len(self._active_clients),
                )
            with contextlib.suppress(Exception):
                writer.close()

    async def _read_rtsp_request(
        self, reader: asyncio.StreamReader, buffer: bytearray | None = None
    ) -> tuple[str, str, dict[str, str], str] | None:
        """Read one complete RTSP request. Returns (method, url, headers, cseq) or None on close."""
        if buffer is None:
            buffer = bytearray()
        delimiter = b"\r\n\r\n"
        while delimiter not in buffer:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
            if not chunk:
                return None
            buffer.extend(chunk)
        end = buffer.index(delimiter) + len(delimiter)
        raw = bytes(buffer[:end])
        del buffer[:end]
        request = raw.decode("utf-8", errors="replace")
        lines = [ln for ln in request.split("\r\n") if ln]
        if not lines:
            return None
        parts = lines[0].split()
        if len(parts) < 2:
            return None
        method, url = parts[0], parts[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        cseq = headers.get("cseq", "1")
        return method, url, headers, cseq

    @staticmethod
    async def _readexactly_buffered(
        reader: asyncio.StreamReader, buffer: bytearray, length: int
    ) -> bytes:
        """Read exact bytes without losing data already received with RTSP headers."""
        while len(buffer) < length:
            chunk = await reader.read(length - len(buffer))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(buffer), length)
            buffer.extend(chunk)
        data = bytes(buffer[:length])
        del buffer[:length]
        return data

    def _parse_setup(
        self,
        transport_hdr: str,
        track_kind: str,
        client: _TcpClient,
        client_host: str,
    ) -> str:
        """Parse SETUP Transport header, update client state, return response."""
        use_tcp = "RTP/AVP/TCP" in transport_hdr or "interleaved" in transport_hdr

        if use_tcp:
            channel = 0
            for part in transport_hdr.split(";"):
                if "interleaved" in part:
                    channel = int(part.split("=", 1)[1].split("-")[0])
            if track_kind == "backchannel":
                client.backchannel_ch = channel
            elif track_kind == "audio":
                client.audio_ch = channel
            else:
                client.video_ch = channel
            return f"Transport: RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"
        else:
            client_port = self._parse_client_port(transport_hdr)
            if track_kind == "audio":
                self._udp_audio_port = client_port
                server_port = self._audio_server_port
            else:
                self._udp_video_port = client_port
                server_port = self._video_server_port
            self._udp_host = client_host
            return (
                f"Transport: RTP/AVP;unicast;"
                f"client_port={client_port}-{client_port + 1};"
                f"server_port={server_port}-{server_port + 1}"
            )

    @staticmethod
    def _send(writer: asyncio.StreamWriter, cseq: str, extra: str = "") -> None:
        writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())

    @staticmethod
    def _parse_client_port(transport_hdr: str) -> int:
        for part in transport_hdr.split(";"):
            if "client_port" in part:
                ports = part.split("=", 1)[1].strip()
                return int(ports.split("-")[0])
        return 0

    def _build_sdp(self) -> str:
        sps_b64 = base64.b64encode(self._latest_sps).decode()
        pps_b64 = base64.b64encode(self._latest_pps).decode()
        # profile-level-id = first 3 bytes of SPS (profile_idc, constraints, level_idc)
        profile_level_id = self._latest_sps[1:4].hex()
        video_sdp = (
            "v=0\r\n"
            f"o=- 0 0 IN IP4 {self._bind_host}\r\n"
            "s=Comelit Intercom\r\n"
            "t=0 0\r\n"
            "m=video 0 RTP/AVP 96\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 packetization-mode=1;"
            f"profile-level-id={profile_level_id};"
            f"sprop-parameter-sets={sps_b64},{pps_b64}\r\n"
            "a=control:video\r\n"
        )
        if not self._audio_enabled:
            return video_sdp
        # go2rtc interprets RTSP SDP directions from its client perspective:
        # recvonly is media received from this relay, while sendonly is the
        # microphone backchannel it sends to the relay.  Reversing these makes
        # go2rtc SETUP both tracks successfully but routes each RTP direction
        # onto the other track, leaving both sides silent.
        return video_sdp + (
            "m=audio 0 RTP/AVP 8\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=recvonly\r\n"
            "a=control:audio\r\n"
            "m=audio 0 RTP/AVP 8\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=sendonly\r\n"
            "a=control:backchannel\r\n"
        )

    async def _wait_for_teardown(
        self,
        reader: asyncio.StreamReader,
        client: _TcpClient,
        client_host: str,
        request_buffer: bytearray | None = None,
    ) -> None:
        """Receive PLAY-session backchannel RTP until TEARDOWN or disconnect.

        go2rtc negotiates the microphone as a third SETUP track on the same
        RTSP connection used for PLAY. It then sends interleaved RTP on that
        track instead of opening a separate ANNOUNCE/RECORD connection.
        """
        if request_buffer is None:
            request_buffer = bytearray()
        while self._running:
            try:
                first = await asyncio.wait_for(
                    self._readexactly_buffered(reader, request_buffer, 1), timeout=10.0
                )
                if first == b"\x24":
                    header = await self._readexactly_buffered(reader, request_buffer, 3)
                    channel = header[0]
                    length = struct.unpack("!H", header[1:3])[0]
                    rtp = await self._readexactly_buffered(
                        reader, request_buffer, length
                    )
                    if (
                        channel == client.backchannel_ch
                        and self._queue_backchannel_rtp(rtp)
                    ):
                        if not client.backchannel_started:
                            client.backchannel_started = True
                            _LOGGER.info(
                                "Backchannel RTP from %s — mic audio flowing",
                                client_host,
                            )
                    continue

                request = first
                while b"\r\n\r\n" not in request:
                    if request_buffer:
                        chunk = bytes(request_buffer)
                        request_buffer.clear()
                    else:
                        chunk = await reader.read(256)
                    if not chunk:
                        return
                    request += chunk
                if b"TEARDOWN" in request:
                    break
            except TimeoutError:
                pass
            except (asyncio.IncompleteReadError, ConnectionError):
                break

    async def _handle_backchannel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_host: str,
        request_buffer: bytearray | None = None,
    ) -> None:
        """Handle SETUP + RECORD for a backchannel connection (after ANNOUNCE accepted)."""
        if request_buffer is None:
            request_buffer = bytearray()
        backchannel_ch = 0
        try:
            while self._running:
                parsed = await self._read_rtsp_request(reader, request_buffer)
                if parsed is None:
                    break
                method, _url, hdrs, cseq = parsed
                _LOGGER.debug("Backchannel %s from %s", method, client_host)

                if method == "SETUP":
                    transport_hdr = hdrs.get("transport", "")
                    for part in transport_hdr.split(";"):
                        if "interleaved" in part:
                            backchannel_ch = int(part.split("=", 1)[1].split("-")[0])
                    self._send(
                        writer,
                        cseq,
                        extra=f"Session: {self._session_id}\r\nTransport: RTP/AVP/TCP;unicast;interleaved={backchannel_ch}-{backchannel_ch + 1}\r\n",
                    )
                    await writer.drain()

                elif method == "RECORD":
                    self._send(writer, cseq, extra=f"Session: {self._session_id}\r\n")
                    await writer.drain()
                    _LOGGER.info(
                        "Backchannel RECORD from %s — mic audio flowing", client_host
                    )
                    await self._receive_backchannel_rtp(reader, request_buffer)
                    break

                elif method == "TEARDOWN":
                    self._send(writer, cseq, extra=f"Session: {self._session_id}\r\n")
                    await writer.drain()
                    break

                else:
                    writer.write(
                        f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n".encode()
                    )
                    await writer.drain()
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            _LOGGER.debug("Backchannel error", exc_info=True)
        _LOGGER.debug("Backchannel connection from %s closed", client_host)

    async def _receive_backchannel_rtp(
        self, reader: asyncio.StreamReader, request_buffer: bytearray | None = None
    ) -> None:
        """Read interleaved RTP from backchannel connection and queue audio payloads."""
        if request_buffer is None:
            request_buffer = bytearray()
        while self._running:
            try:
                header = await asyncio.wait_for(
                    self._readexactly_buffered(reader, request_buffer, 4), timeout=30.0
                )
            except asyncio.IncompleteReadError:
                break
            except ConnectionError:
                break
            except TimeoutError:
                continue

            if header[0] != 0x24:
                # Non-interleaved data (e.g. TEARDOWN) — drain and exit
                rest = header
                with contextlib.suppress(Exception):
                    while b"\r\n\r\n" not in rest:
                        chunk = await asyncio.wait_for(reader.read(256), timeout=5.0)
                        if not chunk:
                            break
                        rest += chunk
                break

            length = struct.unpack("!H", header[2:4])[0]
            try:
                rtp = await asyncio.wait_for(
                    self._readexactly_buffered(reader, request_buffer, length),
                    timeout=5.0,
                )
            except (asyncio.IncompleteReadError, TimeoutError):
                break

            self._queue_backchannel_rtp(rtp)

    def _queue_backchannel_rtp(self, rtp: bytes) -> bool:
        """Extract and enqueue one RTP audio payload."""
        if len(rtp) < 12:
            return False

        header_len = 12 + (rtp[0] & 0x0F) * 4
        if len(rtp) < header_len:
            return False
        if rtp[0] & 0x10:
            if len(rtp) < header_len + 4:
                return False
            extension_words = struct.unpack("!H", rtp[header_len + 2 : header_len + 4])[
                0
            ]
            header_len += 4 + extension_words * 4
        if len(rtp) <= header_len:
            return False

        payload = rtp[header_len:]
        if rtp[0] & 0x20:
            padding = payload[-1]
            if not padding or padding > len(payload):
                return False
            payload = payload[:-padding]
        if not payload:
            return False

        try:
            self.backchannel_queue.put_nowait(payload)
        except asyncio.QueueFull:
            return False
        return True

    # ------------------------------------------------------------------
    # RTP broadcast
    # ------------------------------------------------------------------

    def _prime_client_with_parameter_sets(self, client: _TcpClient) -> None:
        """Send SPS + PPS as in-band RTP packets to a freshly-registered client.

        FFmpeg's RTSP demuxer does not always populate codecpar.format from
        sprop-parameter-sets in the SDP — some versions only set pix_fmt
        after seeing an in-band SPS NAL in the RTP stream.  HA's stream
        worker captures codecpar.format at stream-open time and uses it to
        open libx264; if pix_fmt is unset at that moment, libx264 fails
        with "Invalid video pixel format: -1" on the first keyframe.
        Sending the cached SPS+PPS right after PLAY avoids that race.

        Also emits a first RTCP Sender Report *before* the SPS/PPS so the
        client gets an NTP↔RTP clock anchor before any RTP data arrives.
        Without this, clients that connect mid-stream see RTP timestamps
        far into the future and log "no reference clock" / "PCR is called
        too late" until the next 5 s SR tick catches up.
        """
        if client.video_ch is None:
            return
        self._send_initial_sr_to_client(client)
        for nal in (self._latest_sps, self._latest_pps):
            if not nal:
                continue
            pkt = _build_rtp(
                pt=96,
                seq=self._video_seq,
                ts=self._video_ts_out,
                ssrc=self._video_ssrc,
                payload=nal,
                marker=False,
            )
            self._video_seq = (self._video_seq + 1) & 0xFFFF
            try:
                client.writer.write(
                    struct.pack("!BBH", 0x24, client.video_ch, len(pkt)) + pkt
                )
            except Exception:
                _LOGGER.debug(
                    "Failed to prime client with parameter sets", exc_info=True
                )
                return
        _LOGGER.debug(
            "Primed RTSP client with SPS (%d B) + PPS (%d B)",
            len(self._latest_sps),
            len(self._latest_pps),
        )

    def _send_initial_sr_to_client(self, client: _TcpClient) -> None:
        """Send a one-shot video + audio SR to a single client before any RTP.

        Gives mid-stream clients the NTP↔RTP clock anchor up-front so their
        decoder does not stall while the 5 s periodic SR catches up.  Only
        emits a per-stream SR if that stream has actually sent at least one
        RTP packet — an SR advertising pkt_count=0 with rtp_ts=0 misleads
        ffmpeg's demuxer and can trigger "Stream ended" errors.
        """
        ntp_secs, ntp_frac = _ntp_now()
        if client.video_ch is not None and self._video_pkt_count > 0:
            sr = _build_rtcp_sr(
                ssrc=self._video_ssrc,
                ntp_secs=ntp_secs,
                ntp_frac=ntp_frac,
                rtp_ts=self._last_video_rtp_ts,
                pkt_count=self._video_pkt_count,
                octet_count=self._video_octet_count,
            )
            with contextlib.suppress(Exception):
                client.writer.write(
                    struct.pack("!BBH", 0x24, client.video_ch + 1, len(sr)) + sr
                )
        if client.audio_ch is not None and self._audio_pkt_count > 0:
            sr = _build_rtcp_sr(
                ssrc=self._audio_ssrc,
                ntp_secs=ntp_secs,
                ntp_frac=ntp_frac,
                rtp_ts=self._last_audio_rtp_ts,
                pkt_count=self._audio_pkt_count,
                octet_count=self._audio_octet_count,
            )
            with contextlib.suppress(Exception):
                client.writer.write(
                    struct.pack("!BBH", 0x24, client.audio_ch + 1, len(sr)) + sr
                )

    def _broadcast_rtp(self, pkt: bytes, is_video: bool) -> None:
        """Send one RTP packet to every registered TCP client + UDP client.

        Dead clients (writer closing, broken pipe, etc.) are removed from
        the active list so we don't keep writing into a closed socket and
        leaking error log spam every 20ms.
        """
        self._first_media_event.set()
        dead: list[_TcpClient] = []
        for c in list(self._active_clients):
            ch = c.video_ch if is_video else c.audio_ch
            if ch is None:
                continue
            if c.writer.is_closing():
                dead.append(c)
                continue
            try:
                c.writer.write(struct.pack("!BBH", 0x24, ch, len(pkt)) + pkt)
            except Exception:
                dead.append(c)

        for c in dead:
            with contextlib.suppress(ValueError):
                self._active_clients.remove(c)
            with contextlib.suppress(Exception):
                c.writer.close()
            _LOGGER.info(
                "Removed dead RTSP client [%d remain]",
                len(self._active_clients),
            )

        # UDP fallback — single client (last SETUP wins)
        if self._udp_host:
            port = self._udp_video_port if is_video else self._udp_audio_port
            sock = self._video_sock if is_video else self._audio_sock
            if port and sock:
                with contextlib.suppress(OSError):
                    sock.sendto(pkt, (self._udp_host, port))

    # ------------------------------------------------------------------
    # RTP feed loops — run from start() to stop()
    # ------------------------------------------------------------------

    async def _video_rtp_passthrough_loop(self) -> None:
        """Forward raw video RTP packets with header rewrite.

        Each RTP packet from the device is forwarded immediately with
        seq/ts/ssrc rewritten and PT forced to 96 (matching SDP).
        No NAL reassembly or re-fragmentation — the device's own FU-A
        packing is preserved, eliminating one frame of latency.

        Falls back to the NAL-based loop if no raw RTP packets arrive
        (standalone/test mode where only nal_queue is fed).
        """
        fallback_count = 0
        try:
            while self._running:
                try:
                    rtp = await asyncio.wait_for(self.rtp_queue.get(), timeout=2.0)
                except TimeoutError:
                    fallback_count += 1
                    if fallback_count >= 3 and not self.nal_queue.empty():
                        await self._drain_nal_queue_fallback()
                    continue

                fallback_count = 0
                if len(rtp) < 12:
                    continue

                device_ts = struct.unpack_from("!I", rtp, 4)[0]
                payload = rtp[12:]
                if not payload:
                    continue

                # Cache SPS/PPS from single-NAL packets (not FU-A fragments)
                nal_type_byte = payload[0] & 0x1F
                if nal_type_byte == 7 and len(payload) > 1:
                    self._latest_sps = payload
                elif nal_type_byte == 8 and len(payload) > 1:
                    self._latest_pps = payload

                # Translate device timestamp → monotonic output
                self._translate_video_ts(device_ts)

                # Rewrite RTP header: force PT=96 (matches SDP), keep
                # marker bit from device, replace seq/ts/ssrc with ours.
                marker_bit = rtp[1] & 0x80
                new_header = struct.pack(
                    "!BBHII",
                    rtp[0],  # version/padding/extension/CC
                    marker_bit | 96,  # marker from device + PT=96
                    self._video_seq,
                    self._video_ts_out,
                    self._video_ssrc,
                )
                self._video_seq = (self._video_seq + 1) & 0xFFFF
                self._broadcast_rtp(new_header + payload, is_video=True)
                self._video_pkt_count += 1
                self._video_octet_count += len(payload)
                self._last_video_rtp_ts = self._video_ts_out

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Video RTP pass-through loop error", exc_info=True)

    def _translate_video_ts(self, device_ts: int) -> None:
        """Translate device RTP timestamp to monotonic output timestamp."""
        if self._last_device_ts is None or self._video_ts_rebase_pending:
            self._video_ts_offset = (self._video_ts_out + 1 - device_ts) & 0xFFFFFFFF
            self._video_ts_rebase_pending = False
            _LOGGER.debug(
                "Video timestamp rebased: device=0x%08X seed=0x%08X new_out=0x%08X offset=0x%08X",
                device_ts,
                self._video_ts_out,
                (device_ts + self._video_ts_offset) & 0xFFFFFFFF,
                self._video_ts_offset,
            )
        else:
            forward = (device_ts - self._last_device_ts) & 0xFFFFFFFF
            if forward > 0x80000000:
                prev_out = self._video_ts_out
                self._video_ts_offset = (
                    self._video_ts_out + 1 - device_ts
                ) & 0xFFFFFFFF
                _LOGGER.debug(
                    "Video timestamp rebased (backward): device=0x%08X prev_out=0x%08X new_out=0x%08X offset=0x%08X",
                    device_ts,
                    prev_out,
                    (device_ts + self._video_ts_offset) & 0xFFFFFFFF,
                    self._video_ts_offset,
                )
        self._last_device_ts = device_ts
        self._video_ts_out = (device_ts + self._video_ts_offset) & 0xFFFFFFFF

    async def _drain_nal_queue_fallback(self) -> None:
        """Fallback: drain nal_queue using NAL-based packetization."""
        while not self.nal_queue.empty():
            try:
                device_ts, nal = self.nal_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nal[:4] == b"\x00\x00\x00\x01":
                nal_data = nal[4:]
            elif nal[:3] == b"\x00\x00\x01":
                nal_data = nal[3:]
            else:
                nal_data = nal
            if not nal_data:
                continue
            nal_type = nal_data[0] & 0x1F
            if nal_type == 7:
                self._latest_sps = nal_data
            elif nal_type == 8:
                self._latest_pps = nal_data
            self._translate_video_ts(device_ts)
            self._send_h264(nal_data)

    async def _video_feed_loop(self) -> None:
        """Broadcast H.264 NALs to all registered clients.

        Uses the device's own RTP timestamp directly — the rtp_receiver
        extracts it from the RTP header and passes it through the queue.
        On each new call the device restarts its timestamp from a low
        value, which would look like a backwards jump to the persistent
        HA stream worker; we detect that and adjust `_video_ts_offset` so
        `output_ts = device_ts + offset` stays strictly monotonic and
        preserves the device's exact inter-frame pacing.
        """
        try:
            while self._running:
                try:
                    device_ts, nal = await asyncio.wait_for(
                        self.nal_queue.get(), timeout=2.0
                    )
                except TimeoutError:
                    continue

                if nal[:4] == b"\x00\x00\x00\x01":
                    nal_data = nal[4:]
                elif nal[:3] == b"\x00\x00\x01":
                    nal_data = nal[3:]
                else:
                    nal_data = nal

                if not nal_data:
                    continue

                # Cache SPS/PPS for SDP sprop-parameter-sets.
                nal_type = nal_data[0] & 0x1F
                if nal_type == 7:
                    self._latest_sps = nal_data
                elif nal_type == 8:
                    self._latest_pps = nal_data

                # Translate device timestamp → output timestamp.
                #
                # Happy path (same call): output_ts = device_ts + offset.
                # The device's 90 kHz clock increments naturally between
                # frames so downstream sees the encoder's real pacing.
                #
                # Rebase triggers (recompute offset, then next output =
                # previous _video_ts_out + 1):
                #   1. First frame ever on this server instance.
                #   2. reset() set `_video_ts_rebase_pending` because a
                #      new call is starting — `_video_ts_out` has been
                #      seeded from the audio clock for A/V alignment.
                #   3. device_ts jumped backwards (device clock reset
                #      mid-stream without our reset() being called).
                if self._last_device_ts is None or self._video_ts_rebase_pending:
                    self._video_ts_offset = (
                        self._video_ts_out + 1 - device_ts
                    ) & 0xFFFFFFFF
                    self._video_ts_rebase_pending = False
                    _LOGGER.debug(
                        "Video timestamp rebased (bootstrap): device_ts=0x%08X out=0x%08X offset=0x%08X",
                        device_ts,
                        self._video_ts_out + 1,
                        self._video_ts_offset,
                    )
                else:
                    forward = (device_ts - self._last_device_ts) & 0xFFFFFFFF
                    if forward > 0x80000000:
                        self._video_ts_offset = (
                            self._video_ts_out + 1 - device_ts
                        ) & 0xFFFFFFFF
                        _LOGGER.debug(
                            "Video timestamp rebased (backward jump): device_ts=0x%08X out=0x%08X offset=0x%08X",
                            device_ts,
                            self._video_ts_out + 1,
                            self._video_ts_offset,
                        )

                self._last_device_ts = device_ts
                self._video_ts_out = (device_ts + self._video_ts_offset) & 0xFFFFFFFF

                self._send_h264(nal_data)

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Video feed loop error", exc_info=True)

    def _send_h264(self, nal_data: bytes) -> None:
        """Packetize one H.264 NAL unit and broadcast to all clients."""
        if len(nal_data) <= _MAX_RTP_PAYLOAD:
            pkt = _build_rtp(
                pt=96,
                seq=self._video_seq,
                ts=self._video_ts_out,
                ssrc=self._video_ssrc,
                payload=nal_data,
                marker=True,
            )
            self._video_seq = (self._video_seq + 1) & 0xFFFF
            self._broadcast_rtp(pkt, is_video=True)
            self._video_pkt_count += 1
            self._video_octet_count += len(nal_data)
            self._last_video_rtp_ts = self._video_ts_out
        else:
            # FU-A fragmentation (RFC 6184 §5.8)
            nal_header = nal_data[0]
            nal_type = nal_header & 0x1F
            nal_ref = nal_header & 0xE0
            fu_indicator = nal_ref | 28

            payload = nal_data[1:]
            offset = 0
            first = True
            while offset < len(payload):
                chunk = payload[offset : offset + _MAX_RTP_PAYLOAD - 2]
                offset += len(chunk)
                last = offset >= len(payload)

                fu_header = (
                    (0x80 if first else 0x00) | (0x40 if last else 0x00) | nal_type
                )
                fragment = struct.pack("BB", fu_indicator, fu_header) + chunk

                pkt = _build_rtp(
                    pt=96,
                    seq=self._video_seq,
                    ts=self._video_ts_out,
                    ssrc=self._video_ssrc,
                    payload=fragment,
                    marker=last,
                )
                self._video_seq = (self._video_seq + 1) & 0xFFFF
                self._broadcast_rtp(pkt, is_video=True)
                self._video_pkt_count += 1
                self._video_octet_count += len(fragment)
                self._last_video_rtp_ts = self._video_ts_out
                first = False

    async def _audio_feed_loop(self) -> None:
        """Broadcast G.711 PCMA to all registered clients.

        Between calls (_ready_event clear), sends silent PCMA (0xD5) at the
        correct 20 ms cadence so go2rtc's WebRTC session stays alive in the
        browser.  Silence is suppressed during live calls (_ready_event set)
        to avoid glitches; if no real audio arrives within 20 ms in that case,
        the loop iterates without sending.
        """
        _silence = b"\xd5" * 160
        try:
            while self._running:
                try:
                    payload = await asyncio.wait_for(
                        self.audio_queue.get(), timeout=0.020
                    )
                except TimeoutError:
                    if not self._ready_event.is_set() and self._active_clients:
                        payload = _silence
                    else:
                        continue

                pkt = _build_rtp(
                    pt=8,
                    seq=self._audio_seq,
                    ts=self._audio_ts,
                    ssrc=self._audio_ssrc,
                    payload=payload,
                    marker=False,
                )
                self._audio_seq = (self._audio_seq + 1) & 0xFFFF
                self._audio_ts = (self._audio_ts + len(payload)) & 0xFFFFFFFF
                self._broadcast_rtp(pkt, is_video=False)
                self._audio_pkt_count += 1
                self._audio_octet_count += len(payload)
                self._last_audio_rtp_ts = self._audio_ts

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Audio feed loop error", exc_info=True)

    # ------------------------------------------------------------------
    # RTCP — Sender Reports
    # ------------------------------------------------------------------

    async def _rtcp_sr_loop(self) -> None:
        """Emit one RTCP Sender Report per stream every _RTCP_SR_INTERVAL_S.

        RFC 3550 §6.4.1.  Without these, well-behaved clients (VLC, go2rtc,
        browsers via MSE/WebRTC) cannot map RTP timestamps to wall-clock
        time and stall their decoders for many seconds while they try to
        infer a reference clock from RTP alone.
        """
        try:
            # Wait until the first RTP packet has actually flowed, then emit
            # the first SR immediately — clients that haven't seen an SR yet
            # stall their decoders for seconds trying to infer a reference
            # clock from RTP alone.  After that, cadence per RFC 3550.
            await self._first_media_event.wait()

            while self._running:
                if self._active_clients or self._udp_host:
                    ntp_secs, ntp_frac = _ntp_now()

                    video_sr = _build_rtcp_sr(
                        ssrc=self._video_ssrc,
                        ntp_secs=ntp_secs,
                        ntp_frac=ntp_frac,
                        rtp_ts=self._last_video_rtp_ts,
                        pkt_count=self._video_pkt_count,
                        octet_count=self._video_octet_count,
                    )
                    self._broadcast_rtcp(video_sr, is_video=True)

                    audio_sr = _build_rtcp_sr(
                        ssrc=self._audio_ssrc,
                        ntp_secs=ntp_secs,
                        ntp_frac=ntp_frac,
                        rtp_ts=self._last_audio_rtp_ts,
                        pkt_count=self._audio_pkt_count,
                        octet_count=self._audio_octet_count,
                    )
                    self._broadcast_rtcp(audio_sr, is_video=False)

                await asyncio.sleep(_RTCP_SR_INTERVAL_S)

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("RTCP SR loop error", exc_info=True)

    def _broadcast_rtcp(self, pkt: bytes, is_video: bool) -> None:
        """Send one RTCP packet to every TCP and UDP client.

        TCP path: interleaved on (video_ch | audio_ch) + 1 — RFC 2326 §10.12
        reserves the odd interleaved channel of each pair for RTCP.
        UDP path: send to (client_port + 1) — RFC 3550 §11 convention.
        We reuse the existing RTP socket as the source; clients accept
        RTCP from the RTP source port in practice.
        """
        dead: list[_TcpClient] = []
        for c in self._active_clients:
            ch = c.video_ch if is_video else c.audio_ch
            if ch is None:
                continue
            if c.writer.is_closing():
                dead.append(c)
                continue
            try:
                c.writer.write(struct.pack("!BBH", 0x24, ch + 1, len(pkt)) + pkt)
            except Exception:
                dead.append(c)

        for c in dead:
            with contextlib.suppress(ValueError):
                self._active_clients.remove(c)
            with contextlib.suppress(Exception):
                c.writer.close()

        if self._udp_host:
            port = self._udp_video_port if is_video else self._udp_audio_port
            sock = self._video_sock if is_video else self._audio_sock
            if port and sock:
                with contextlib.suppress(OSError):
                    sock.sendto(pkt, (self._udp_host, port + 1))


def _ntp_now() -> tuple[int, int]:
    """Return current wall-clock time as (NTP seconds, NTP fractional)."""
    now = time.time()
    secs = int(now) + _NTP_EPOCH_OFFSET
    frac = int((now - int(now)) * (1 << 32)) & 0xFFFFFFFF
    return secs, frac


_CNAME = b"comelit@local"


def _build_rtcp_sr(
    ssrc: int,
    ntp_secs: int,
    ntp_frac: int,
    rtp_ts: int,
    pkt_count: int,
    octet_count: int,
) -> bytes:
    """Build a compound RTCP packet: Sender Report + SDES (CNAME).

    RFC 3550 §6.1 requires RTCP to be sent as a compound packet where the
    first must be SR (or RR) and SDES CNAME MUST be present.  Strict clients
    (VLC default build, gstreamer, some browsers) ignore a lone SR and keep
    logging "no reference clock" until a CNAME arrives.
    """
    # --- SR (28 bytes) ----------------------------------------------------
    # V=2, P=0, RC=0 -> 0x80; PT=200 (SR); length=6 (32-bit words minus 1).
    sr = struct.pack(
        "!BBHIIIIII",
        0x80,
        200,
        6,
        ssrc & 0xFFFFFFFF,
        ntp_secs & 0xFFFFFFFF,
        ntp_frac & 0xFFFFFFFF,
        rtp_ts & 0xFFFFFFFF,
        pkt_count & 0xFFFFFFFF,
        octet_count & 0xFFFFFFFF,
    )

    # --- SDES with one chunk: SSRC + CNAME item + END, 32-bit padded ------
    # SDES item: type=1 (CNAME), length=len(text), text bytes.
    cname_item = struct.pack("!BB", 1, len(_CNAME)) + _CNAME
    # Terminator item (type=0), then zero-pad to 32-bit boundary.
    chunk = struct.pack("!I", ssrc & 0xFFFFFFFF) + cname_item + b"\x00"
    pad = (-len(chunk)) & 3
    chunk += b"\x00" * pad
    sdes_len_words = len(chunk) // 4  # number of 32-bit words after header
    # V=2, P=0, SC=1 -> 0x81; PT=202 (SDES); length = words_after_header.
    sdes = struct.pack("!BBH", 0x81, 202, sdes_len_words) + chunk
    return sr + sdes


def _build_rtp(
    pt: int,
    seq: int,
    ts: int,
    ssrc: int,
    payload: bytes,
    marker: bool,
) -> bytes:
    """Build a minimal 12-byte RTP header + payload."""
    first_byte = 0x80  # version=2, no padding, no extension, CC=0
    second_byte = (0x80 if marker else 0x00) | (pt & 0x7F)
    return struct.pack("!BBHII", first_byte, second_byte, seq, ts, ssrc) + payload
