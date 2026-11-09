"""DataUpdateCoordinator for the Comelit Local integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .comelit_client import IconaBridgeClient as LegacyDoorClient
from .const import CONF_ENABLE_NOTIFICATIONS, DEFAULT_PORT, DOMAIN
from .control_discovery import (
    CONTROL_TYPE_ACTUATOR,
    CONTROL_TYPE_OPENDOOR,
    control_identity,
)
from .video.auth import authenticate
from .video.channels import ChannelType
from .video.client import IconaBridgeClient
from .video.config_reader import get_device_config
from .video.ctpp import ctpp_init_sequence
from .video.exceptions import AuthenticationError
from .video.models import DeviceConfig, Door, PushEvent
from .video.push import register_push, send_push_keepalive
from .video.rtsp_server import LocalRtspServer
from .video.video_call import VideoCallSession
from .video.vip_listener import VipEventListener

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)
VIDEO_PURPOSE_LIVE = "live"
VIDEO_PURPOSE_SNAPSHOT = "snapshot"


class ComelitDataUpdateCoordinator(DataUpdateCoordinator[DeviceConfig]):
    """Coordinator that manages the persistent connection and push notifications."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry
        self.host = entry.data[CONF_HOST]
        self.port = entry.data.get("port", DEFAULT_PORT)
        self.token = entry.data[CONF_TOKEN]
        self.device_name = entry.title
        self._client: IconaBridgeClient | None = None
        self._config: DeviceConfig | None = None
        self._video_session: VideoCallSession | None = None
        self._video_session_purpose: str | None = None
        self._video_stream_revision = 0
        self._video_stopped_by_user: bool = False
        # Prevents concurrent async_start_video calls from racing each other.
        # The device can only handle one CTPP negotiation at a time; a second
        # concurrent call would conflict and fail ~35s later with a UDPM timeout.
        self._video_start_lock: asyncio.Lock = asyncio.Lock()
        # Serialize connection replacement and the dedicated legacy door path.
        # ICONA panels accept only one client, so overlapping reconnects (or a
        # reconnect while a door command owns the panel) evict each other.
        self._transport_lock: asyncio.Lock = asyncio.Lock()
        # Set before unload waits for the locks so an in-flight door/reconnect
        # cannot recreate transport resources behind the shutdown path.
        self._shutting_down: bool = False
        # Fires when a video session becomes ready — allows stream_source()
        # to wait briefly instead of returning None while CTPP is in flight.
        self._video_ready_event: asyncio.Event = asyncio.Event()
        self._rtsp_server: LocalRtspServer | None = None
        self._rtsp_url: str | None = None
        self._vip_listener: VipEventListener | None = None
        # LE32 counter sent in the last CTPP init on the shared connection;
        # VIP listener needs it to derive outgoing ACK timestamps
        # (ack_ts = init_ts + 0x01010000, PCAP-verified).
        self._ctpp_init_ts: int = 0
        self._keepalive_task: asyncio.Task[None] | None = None
        # Tracks whether we were connected on the last health-check so
        # disconnect / reconnect are logged exactly once per transition.
        self._connection_lost: bool = False
        # Use an insertion-ordered dict to track callbacks (value is always None).
        # This avoids ValueError on removal and preserves iteration order.
        self._push_callbacks: dict[Callable[[PushEvent], None], None] = {}
        # Async callbacks invoked at the top of async_stop_video, before any
        # RTSP client disconnect.  Lets the camera entity tear down HA's
        # Stream worker gracefully so its container_packets iterator ends
        # cleanly instead of raising "Stream ended; no additional packets"
        # on an EOF from our forced socket close.
        self._on_stop_video: dict[Callable[[], Awaitable[None]], None] = {}
        # Async callbacks invoked after video session becomes ready or is
        # fully torn down.  The camera entity uses this to write a fresh
        # HA state (is_streaming True/False) so the frontend card reacts
        # — without it, picture-entity locks to the transport it picked
        # at first stream_source() call and never upgrades from MJPEG.
        self._on_video_state_change: dict[Callable[[], Awaitable[None]], None] = {}
        self._last_video_end_reason: str | None = None
        self._last_video_end_at: str | None = None
        # WebRTC viewers own a live session independently. A dashboard card
        # closing must never tear down media still used by another viewer.
        self._live_viewers: set[str] = set()

    @property
    def device_config(self) -> DeviceConfig | None:
        """Return the current device configuration."""
        return self._config

    @property
    def rtsp_url(self) -> str | None:
        """Return the persistent RTSP URL (available after setup)."""
        return self._rtsp_url

    @property
    def rtsp_server(self) -> LocalRtspServer | None:
        """Return the persistent RTSP server instance."""
        return self._rtsp_server

    @property
    def video_available(self) -> bool:
        """Return whether the local relay and Comelit connection are ready."""
        return bool(
            self._rtsp_server is not None
            and self._client is not None
            and self._client.connected
        )

    @property
    def video_ready_event(self) -> asyncio.Event:
        """Return the media-ready event used by the camera entity."""
        return self._video_ready_event

    @property
    def last_video_end_reason(self) -> str | None:
        """Return the most recent unexpected video termination reason."""
        return self._last_video_end_reason

    @property
    def last_video_end_at(self) -> str | None:
        """Return when the most recent unexpected termination occurred."""
        return self._last_video_end_at

    async def _open_ctpp_channels(
        self, client: IconaBridgeClient, config: DeviceConfig
    ) -> int:
        """Open CTPP + CSPB channels and run the full init handshake.

        Called at setup and reconnect when notifications are enabled. When
        notifications are disabled, CTPP is opened lazily by door/video.

        Returns the init_ts used in the handshake so the VIP listener can
        derive its outgoing ACK timestamps from the same value.
        """
        our_addr = f"{config.apt_address}{config.apt_subaddress}"
        ctpp = await client.open_channel("CTPP", ChannelType.UAUT, extra_data=our_addr)
        await client.open_channel("CSPB", ChannelType.UAUT)
        ts = int(time.time()) & 0xFFFFFFFF
        await ctpp_init_sequence(
            client,
            ctpp,
            config.apt_address,
            config.apt_subaddress,
            our_addr,
            ts,
        )
        self._ctpp_init_ts = ts
        _LOGGER.info(
            "CTPP channels opened for VIP events (address=%s, ts=0x%08X)",
            our_addr,
            ts,
        )
        return ts

    async def async_setup(self) -> None:
        """Connect, authenticate, fetch config, and register for push."""
        client = IconaBridgeClient(self.host, self.port)
        await client.connect()
        try:
            await authenticate(client, self.token)
            self._config = await get_device_config(client)
            await register_push(client, self._config, self._on_push_event)
        except Exception:
            await client.disconnect()
            raise

        self._client = client
        client.set_disconnect_callback(lambda: self._on_client_disconnect(client))

        # Start VIP event listener for doorbell ring detection, unless disabled.
        # The PUSH channel is one-shot FCM registration; actual call events
        # arrive as binary VIP messages on the CTPP channel.
        if self.config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True):  # type: ignore[union-attr]
            try:
                init_ts = await self._open_ctpp_channels(client, self._config)
                vip = VipEventListener(
                    client,
                    self._config,
                    self._on_push_event,
                    init_ts=init_ts,
                )
                await vip.start()
                self._vip_listener = vip
            except Exception:
                _LOGGER.warning("Failed to start VIP event listener", exc_info=True)
        else:
            _LOGGER.info("VIP event listener disabled via options")

        self._start_keepalive()

        # Start persistent RTSP server so go2rtc can connect immediately
        if not self._rtsp_server:
            rtsp = LocalRtspServer()
            self._rtsp_url = await rtsp.start()
            self._rtsp_server = rtsp
            _LOGGER.info("Persistent RTSP server started: %s", self._rtsp_url)

        self.async_set_updated_data(self._config)
        _LOGGER.info(
            "Comelit setup complete: %d doors, %d cameras",
            len(self._config.doors),
            len(self._config.cameras),
        )

    async def _reconnect(self, *, force: bool = False) -> None:
        """Serialize reconnects and avoid replacing a connection already restored."""
        client_to_replace = self._client
        async with self._transport_lock:
            if self._shutting_down:
                return
            # Several callers can observe the same disconnect. The first one
            # restores the transport; later waiters must use that connection
            # instead of immediately evicting it with another ICONA login. A
            # forced reset is still skipped if another waiter already replaced
            # the exact client this caller observed.
            if (
                self._client is not None
                and self._client.connected
                and self._config is not None
                and (not force or self._client is not client_to_replace)
            ):
                _LOGGER.debug("Reconnect skipped — connection already restored")
                return
            await self._reconnect_locked()

    async def _reconnect_locked(self) -> None:
        """Replace the persistent transport while ``_transport_lock`` is held."""
        if self._shutting_down:
            return
        self._cancel_keepalive()
        # Stop any active video session before disconnecting — a concurrent
        # session.start() holds a reference to the old client and will hang
        # for READ_TIMEOUT (30s) waiting for channel opens that will never
        # arrive once the TCP socket is closed.
        if self._video_session:
            with contextlib.suppress(Exception):
                await self._video_session.stop(reason="reconnect")
            self._video_session = None
            self._video_session_purpose = None
            self._video_ready_event.clear()
            if self._rtsp_server:
                self._rtsp_server.mark_not_ready()
                self._rtsp_server.disconnect_clients()

        if self._vip_listener:
            with contextlib.suppress(Exception):
                await self._vip_listener.stop()
            self._vip_listener = None

        old_client = self._client
        self._client = None
        if old_client:
            # A planned teardown must not schedule a competing refresh. The
            # client-specific callback also protects against a late callback
            # that was already queued before it was detached.
            old_client.set_disconnect_callback(None)
            try:
                await old_client.disconnect()
            except Exception:
                _LOGGER.debug("Error disconnecting old client", exc_info=True)

        client = IconaBridgeClient(self.host, self.port)
        try:
            await client.connect()
            await authenticate(client, self.token)
            self._config = await get_device_config(client)
            await register_push(client, self._config, self._on_push_event)
        except Exception:
            # Clean up the new client if setup fails partway through
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise

        self._client = client
        client.set_disconnect_callback(lambda: self._on_client_disconnect(client))

        if self.config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True):  # type: ignore[union-attr]
            try:
                init_ts = await self._open_ctpp_channels(client, self._config)
                vip = VipEventListener(
                    client,
                    self._config,
                    self._on_push_event,
                    init_ts=init_ts,
                )
                await vip.start()
                self._vip_listener = vip
            except Exception:
                _LOGGER.warning(
                    "Failed to start VIP listener on reconnect", exc_info=True
                )

        self._start_keepalive()
        self._connection_lost = False
        ir.async_delete_issue(self.hass, DOMAIN, "auth_failed")
        await self._notify_video_state_change()
        _LOGGER.info("Comelit reconnected successfully")

    async def async_shutdown(self) -> None:
        """Disconnect from the device."""
        self._shutting_down = True
        async with self._video_start_lock:
            async with self._transport_lock:
                self._cancel_keepalive()
                await self.async_stop_video(reason="shutdown", reset_transport=False)
                if self._vip_listener:
                    with contextlib.suppress(Exception):
                        await self._vip_listener.stop()
                    self._vip_listener = None
                if self._rtsp_server:
                    with contextlib.suppress(Exception):
                        await self._rtsp_server.stop()
                    self._rtsp_server = None
                    self._rtsp_url = None
                client = self._client
                self._client = None
                if client:
                    client.set_disconnect_callback(None)
                    await client.disconnect()

    async def async_setup_video(self) -> None:
        """Set up the persistent ICONA, event, media, and RTSP paths."""
        await self.async_setup()

    async def async_shutdown_video(self) -> None:
        """Shut down resources owned by the config entry."""
        await self.async_shutdown()

    def add_push_callback(
        self, callback: Callable[[PushEvent], None]
    ) -> Callable[[], None]:
        """Register a push event callback. Returns a callable that removes it."""
        self._push_callbacks[callback] = None

        def _remove() -> None:
            self._push_callbacks.pop(callback, None)

        return _remove

    def add_stop_video_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]:
        """Register an async callback invoked when video is stopped."""
        self._on_stop_video[callback] = None

        def _remove() -> None:
            self._on_stop_video.pop(callback, None)

        return _remove

    def add_video_state_change_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]:
        """Register an async callback invoked after video becomes ready or stops."""
        self._on_video_state_change[callback] = None

        def _remove() -> None:
            self._on_video_state_change.pop(callback, None)

        return _remove

    async def _notify_video_state_change(self) -> None:
        """Fire all registered video state-change callbacks."""
        for cb in list(self._on_video_state_change):
            try:
                await cb()
            except Exception:
                _LOGGER.exception("Error in video state change callback")

    def _on_push_event(self, event: PushEvent) -> None:
        """Dispatch a push event to all registered callbacks."""
        for cb in list(self._push_callbacks):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("Error in push callback")

    async def async_open_door(self, door: Door) -> None:
        """Open a door using the dedicated path proven by the original fork."""
        if self._shutting_down:
            raise HomeAssistantError("Comelit integration is shutting down")
        if not self._config or not self._config.raw:
            raise RuntimeError("Not connected")
        vip = self._config.raw.get("vip")
        if not isinstance(vip, dict):
            raise HomeAssistantError("Comelit VIP configuration is unavailable")

        control: dict[str, object] = {
            "name": door.name,
            "control-type": (
                CONTROL_TYPE_ACTUATOR if door.is_actuator else CONTROL_TYPE_OPENDOOR
            ),
            "apt-address": door.apt_address,
            "output-index": door.output_index,
            "secure-mode": door.secure_mode,
        }
        if door.module_index is not None:
            control["module-index"] = door.module_index
        door_error: HomeAssistantError | None = None
        door_cause: Exception | None = None
        restore_error: Exception | None = None
        door_succeeded = False
        restart_live_video = False
        live_video_was_viewer_owned = False

        # Prevent a video negotiation from racing this exclusive takeover.
        # The transport lock also keeps a background refresh from stopping the
        # same video session concurrently. Door control always rebuilds the
        # transport below, so video cleanup does not need a separate reset.
        async with self._video_start_lock:
            active_session = self._video_session
            restart_live_video = bool(
                active_session
                and active_session.active
                and self._video_session_purpose == VIDEO_PURPOSE_LIVE
                and not self._video_stopped_by_user
            )
            live_video_was_viewer_owned = bool(self._live_viewers)
            async with self._transport_lock:
                if self._shutting_down:
                    raise HomeAssistantError("Comelit integration is shutting down")
                await self.async_stop_video(
                    reason="door control", reset_transport=False
                )
                self._cancel_keepalive()
                if self._vip_listener:
                    with contextlib.suppress(Exception):
                        await self._vip_listener.stop()
                    self._vip_listener = None

                # The panel supports one ICONA client. Deliberately release the
                # persistent client before starting the proven legacy sequence,
                # then rebuild it regardless of the door-command outcome.
                persistent_client = self._client
                self._client = None
                if persistent_client:
                    persistent_client.set_disconnect_callback(None)
                    with contextlib.suppress(Exception):
                        await persistent_client.disconnect()

                door_client = LegacyDoorClient(self.host, self.port)
                try:
                    await door_client.connect()
                    auth_code = await door_client.authenticate(self.token)
                    if auth_code != 200:
                        raise HomeAssistantError(
                            f"Comelit authentication failed with code {auth_code}"
                        )
                    if door.is_actuator:
                        await door_client.open_actuator(vip, control)
                    else:
                        await door_client.open_door(vip, control)
                    door_succeeded = True
                    _LOGGER.info(
                        "Door '%s' opened with dedicated legacy sequence "
                        "(target=%s, output=%d)",
                        door.name,
                        door.apt_address,
                        door.output_index,
                    )
                except HomeAssistantError as err:
                    door_error = err
                except Exception as err:
                    door_cause = err
                    door_error = HomeAssistantError(
                        f"Failed to open {door.name or 'Comelit control'}: {err}"
                    )
                finally:
                    with contextlib.suppress(Exception):
                        await door_client.shutdown()
                    if not self._shutting_down:
                        try:
                            await self._reconnect_locked()
                        except Exception as err:
                            self._connection_lost = True
                            restore_error = err
                            if door_error is not None:
                                _LOGGER.error(
                                    "Persistent Comelit connection could not be "
                                    "restored after a failed door command",
                                    exc_info=True,
                                )

        if door_succeeded:
            self._on_push_event(
                PushEvent(
                    event_type="door_opened",
                    apt_address=door.apt_address,
                    timestamp=time.time(),
                    raw={"door": door.name, "output_index": door.output_index},
                )
            )
        if (
            restart_live_video
            and restore_error is None
            and not self._shutting_down
            and not self._video_stopped_by_user
            and (not live_video_was_viewer_owned or self._live_viewers)
        ):
            try:
                await self.async_start_video(
                    auto_timeout=False, purpose=VIDEO_PURPOSE_LIVE
                )
            except Exception:
                # Door control already completed (or failed independently), so
                # a best-effort preview recovery must not change its result.
                _LOGGER.warning(
                    "Unable to resume live video after door control", exc_info=True
                )
        if door_error is not None:
            if door_cause is not None:
                raise door_error from door_cause
            raise door_error
        if restore_error is not None:
            raise HomeAssistantError(
                "Door command was sent, but the persistent Comelit connection "
                f"could not be restored: {restore_error}"
            ) from restore_error

    async def async_open_control(self, control: dict[str, object]) -> None:
        """Open a discovered legacy control using the richer door model."""
        identity = control_identity(control)
        if self._config:
            for door in self._config.doors:
                door_control: dict[str, object] = {
                    "control-type": (
                        CONTROL_TYPE_ACTUATOR
                        if door.is_actuator
                        else CONTROL_TYPE_OPENDOOR
                    ),
                    "apt-address": door.apt_address,
                    "output-index": door.output_index,
                }
                if door.module_index is not None:
                    door_control["module-index"] = door.module_index
                if control_identity(door_control) == identity:
                    await self.async_open_door(door)
                    return
        raise RuntimeError("Door control is no longer available")

    async def async_start_video(
        self,
        auto_timeout: bool = True,
        by_user: bool = False,
        purpose: str = VIDEO_PURPOSE_LIVE,
    ) -> VideoCallSession:
        """Start a video call session.

        Starts are serialized because the device can negotiate only one CTPP
        media session at a time. A live request can promote an in-progress
        snapshot session instead of tearing it down and negotiating again.

        Args:
            auto_timeout: stop the session after VIDEO_SESSION_TIMEOUT seconds.
            by_user: True when called from an explicit user action (button press).
                     False for auto-restarts from CALL_END / timeout callbacks.
                     Auto-restarts are silently dropped if the user has
                     since stopped video (prevents a stale async_create_task
                     from overriding a user stop and causing an infinite
                     go2rtc reconnect loop).
            purpose: ``live`` for a viewer-owned stream or ``snapshot`` for a
                     short still-image capture.
        """
        if not self._config:
            raise RuntimeError("Not configured")

        async with self._video_start_lock:
            if not self._client:
                raise RuntimeError("Not connected")

            if self._video_session and self._video_session.active:
                if (
                    purpose == VIDEO_PURPOSE_LIVE
                    and self._video_session_purpose == VIDEO_PURPOSE_SNAPSHOT
                ):
                    self._video_session_purpose = VIDEO_PURPOSE_LIVE
                    _LOGGER.debug("Snapshot session promoted to live viewing")
                return self._video_session

            # Drop auto-restarts that arrive after the user has stopped video.
            # Race: _on_video_call_end schedules async_start_video() as a task;
            # the user may stop video before the task executes.  Without this
            # check the stale task would reset _video_stopped_by_user and call
            # mark_ready(), causing go2rtc to reconnect into a dead stream.
            if self._video_stopped_by_user and not by_user:
                _LOGGER.debug("Skipping auto-restart — video was stopped by user")
                raise RuntimeError("Video was stopped by user — not auto-restarting")

            # If the TCP connection died (120s receive-loop timeout) before the
            # health-check interval had a chance to reconnect, reconnect now so
            # we don't start a session on a dead socket and wait 30s for UDPM
            # to time out.
            if not self._client.connected:
                _LOGGER.info("Client disconnected — reconnecting before video start")
                try:
                    await self._reconnect()
                except Exception as err:
                    raise RuntimeError(f"Reconnect failed: {err}") from err

            self._video_stopped_by_user = False
            await self.async_stop_video()

            # Pause the VIP listener task so it doesn't consume CTPP messages
            # meant for the video session. The CTPP channel itself stays open
            # and will be reused by the video session directly (no rename needed).
            # The listener task restarts in async_stop_video via _ensure_vip_listener.
            if self._vip_listener:
                with contextlib.suppress(Exception):
                    await self._vip_listener.stop_task()
                self._vip_listener = None

            t0 = time.monotonic()
            _LOGGER.info("Video session starting (CTPP setup)")
            session = VideoCallSession(
                self._client,
                self._config,
                auto_timeout=auto_timeout,
                rtsp_server=self._rtsp_server,
                on_call_end=self._on_video_call_end,
                on_timeout=self._on_video_call_end,
            )
            # Publish the session ONLY after start() has completed its
            # readiness gate (first real NAL queued).  Publishing earlier
            # lets HA's stream worker open the RTSP URL while CTPP is
            # still negotiating — it probes a video-less stream, stalls,
            # and takes ~20 s extra to recover once real NALs finally
            # arrive.  The trade-off is a cosmetic "camera does not
            # support play stream service" error logged by Lovelace at
            # the ~2 s mark, because `stream_source()` returns None while
            # CTPP is in flight.  go2rtc's WebRTC path queries the URL
            # through a different code path and is not affected, so the
            # user-visible latency stays at ~3 s.
            try:
                await session.start()
                # A visible dashboard can disappear while ICONA negotiation is
                # still in flight. The session is not published until media is
                # ready, so the normal stop path cannot see it during that
                # window. Honour the stop request before exposing the stream.
                if self._video_stopped_by_user:
                    await session.stop(reason="cancelled while starting")
                    if self._rtsp_server:
                        self._rtsp_server.mark_not_ready()
                        self._rtsp_server.disconnect_clients()
                    raise RuntimeError("Video start was cancelled")
                _LOGGER.info("Video session ready in %.1fs", time.monotonic() - t0)
                if self._rtsp_server and session.rtp_receiver:
                    session.rtp_receiver.attach_backchannel_queue(
                        self._rtsp_server.backchannel_queue
                    )
                self._video_session = session
                self._video_session_purpose = purpose
                self._video_stream_revision += 1
                # Preserve an unexpected end reason across an automatic
                # recovery so the card can explain why the previous stream
                # stopped. A deliberate new view starts with a clean status.
                if by_user and purpose == VIDEO_PURPOSE_LIVE:
                    self._last_video_end_reason = None
                    self._last_video_end_at = None
                self._video_ready_event.set()
                # Unblock PLAY handlers that have been waiting inside the RTSP
                # server for video to actually flow.  Any stream_worker that
                # reconnected during the CTPP handshake is stalled on PLAY
                # (our server holds 200 OK until mark_ready); releasing it here
                # means it transitions straight to reading frames instead of
                # erroring on an empty stream and taking a 10 s HA backoff.
                if self._rtsp_server:
                    self._rtsp_server.mark_ready()
                await self._notify_video_state_change()
                return session
            except Exception:
                if session.cleanup_requires_reconnect:
                    _LOGGER.info(
                        "Resetting Comelit connection after incomplete video cleanup"
                    )
                    try:
                        await self._reconnect(force=True)
                    except Exception:
                        _LOGGER.warning(
                            "Failed to reset connection after video start failure",
                            exc_info=True,
                        )
                await self._ensure_vip_listener()
                raise

    def _on_video_call_end(self) -> None:
        """Called by VideoCallSession when the device sends CALL_END."""
        if (
            self._video_stopped_by_user
            or self._video_session_purpose != VIDEO_PURPOSE_LIVE
        ):
            return
        self._record_unexpected_video_end()
        _LOGGER.debug("CALL_END received — scheduling session restart")
        self.config_entry.async_create_background_task(  # type: ignore[union-attr]
            self.hass, self._auto_restart_video(), "comelit-auto-restart-video"
        )

    async def _auto_restart_video(self) -> None:
        """Auto-restart video after CALL_END or timeout.

        Calls async_start_video() without by_user=True so the call is
        silently dropped if the user has stopped video in the meantime.
        RuntimeError from that path is caught here to avoid HA logging an
        unhandled task exception for a normal, expected situation.
        """
        try:
            await self.async_start_video(
                auto_timeout=False,
                purpose=VIDEO_PURPOSE_LIVE,
            )
        except RuntimeError as err:
            _LOGGER.debug("Auto-restart skipped: %s", err)
        except Exception:
            _LOGGER.warning("Auto-restart failed", exc_info=True)

    async def async_capture_video_snapshot(self) -> bytes | None:
        """Capture and cache one frame without leaving the panel occupied.

        If a live session already exists, its newest frame is used
        and the session is left untouched. Otherwise a short receive-only
        session is opened and released after the JPEG has been decoded. A live
        request arriving during that negotiation promotes the same session and
        prevents this method from closing it.
        """
        session = await self.async_start_video(
            auto_timeout=False,
            by_user=True,
            purpose=VIDEO_PURPOSE_SNAPSHOT,
        )
        try:
            receiver = session.rtp_receiver
            if receiver is None:
                return None
            if receiver.latest_frame:
                return receiver.latest_frame
            async with asyncio.timeout(3.0):
                return await receiver.get_jpeg_frame()
        except TimeoutError:
            return receiver.latest_frame if receiver is not None else None
        finally:
            stopped_snapshot = False
            async with self._video_start_lock:
                if (
                    self._video_session is session
                    and self._video_session_purpose == VIDEO_PURPOSE_SNAPSHOT
                ):
                    await self.async_stop_video(reason="snapshot captured")
                    stopped_snapshot = True
            # async_stop_video intentionally avoids restarting the VIP listener
            # while the start lock is held. Snapshot capture is not followed by
            # another start, so restore the listener explicitly afterwards.
            if stopped_snapshot:
                await self._ensure_vip_listener()

    def add_live_viewer(self, viewer_id: str) -> None:
        """Register a WebRTC viewer that currently owns the live session."""
        self._live_viewers.add(viewer_id)

    def remove_live_viewer(self, viewer_id: str) -> None:
        """Release a WebRTC viewer's ownership of the live session."""
        self._live_viewers.discard(viewer_id)

    async def async_release_live_video(
        self, reason: str = "live viewer closed"
    ) -> None:
        """Release a viewer-owned live session without racing a new viewer."""
        stopped_live = False
        async with self._video_start_lock:
            if (
                not self._live_viewers
                and self._video_session_purpose == VIDEO_PURPOSE_LIVE
            ):
                self.request_video_stop()
                await self.async_stop_video(reason=reason)
                stopped_live = True
        if stopped_live:
            await self._ensure_vip_listener()

    @property
    def video_stopped_by_user(self) -> bool:
        """Return True if the user explicitly stopped video (not CALL_END)."""
        return self._video_stopped_by_user

    def request_video_stop(self) -> None:
        """Mark that the user explicitly requested video to stop."""
        self._video_stopped_by_user = True

    def _start_keepalive(self) -> None:
        """Start the background keepalive task (cancels any previous one)."""
        self._cancel_keepalive()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _cancel_keepalive(self) -> None:
        """Cancel the keepalive task if running."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Send a periodic push-info probe to keep the TCP connection alive.

        The Comelit device sleeps when idle and stops sending TCP traffic.
        Without this probe, the receive-loop 120s timeout fires and triggers
        a full reconnect cycle.  Re-sending push-info every 90s causes the
        device to respond with a JSON ACK, resetting the idle timer.

        If the device is genuinely unreachable (half-open socket), send_json
        raises within 10s (ProtocolError timeout) — the receive-loop will
        also detect the dead connection shortly after and trigger reconnect.
        """
        KEEPALIVE_INTERVAL = 90
        KEEPALIVE_TIMEOUT = 10.0

        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self._client or not self._client.connected:
                return
            if not self._config:
                return
            try:
                await asyncio.wait_for(
                    send_push_keepalive(self._client, self._config),
                    timeout=KEEPALIVE_TIMEOUT,
                )
                _LOGGER.debug("Keepalive OK")
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.debug(
                    "Keepalive failed — connection may be dead", exc_info=True
                )
                # Don't force-reconnect here; the receive-loop will detect the
                # dead socket and set client.connected = False within seconds,
                # which the coordinator's health-check will pick up.

    async def _ensure_vip_listener(self) -> None:
        """Start VIP listener if enabled and not already running.

        Reuses the init_ts stored from the most recent _open_ctpp_channels
        call so the restarted listener's outgoing ACKs match the counter
        state the device already has for this CTPP channel.
        """
        if self._vip_listener or not self._config or not self._client:
            return
        if not self.config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True):  # type: ignore[union-attr]
            return
        try:
            vip = VipEventListener(
                self._client,
                self._config,
                self._on_push_event,
                init_ts=self._ctpp_init_ts,
            )
            await vip.start()
            self._vip_listener = vip
            _LOGGER.debug("VIP event listener restarted")
        except Exception:
            _LOGGER.warning("Failed to restart VIP listener", exc_info=True)

    async def async_stop_video(
        self, reason: str = "user stopped", *, reset_transport: bool = True
    ) -> None:
        """Stop the active video call session.

        Snapshots _video_session and clears it immediately so a concurrent
        async_stop_video call can't also try to stop the same session
        (previous behaviour raced and crashed with AttributeError when the
        first stop cleared the attribute while the second was awaiting a
        stop-callback).
        """
        session = self._video_session
        if session is None:
            return
        self._video_session = None
        self._video_session_purpose = None
        self._video_ready_event.clear()

        # Tear HA's Stream worker down gracefully FIRST, before any
        # forced RTSP client disconnect.  Stream.stop() joins the
        # worker thread, so its container closes cleanly; without
        # this, disconnect_clients() triggers an EOF mid-read and
        # HA logs "Stream ended; no additional packets" plus a 10 s
        # backoff before the next Start can recover.
        for cb in list(self._on_stop_video):
            try:
                await cb()
            except Exception:
                _LOGGER.exception("Error in stop-video callback")

        await session.stop(reason=reason)
        if (
            reset_transport
            and session.cleanup_requires_reconnect
            and reason not in ("reconnect", "shutdown")
        ):
            _LOGGER.info(
                "Resetting Comelit connection to finish releasing video channels"
            )
            await self._reconnect(force=True)
        # Block future PLAYs until the next session is ready, and
        # kick any remaining RTSP clients (e.g. go2rtc) so they
        # reconnect fresh against a stream that already has video.
        if self._rtsp_server:
            self._rtsp_server.mark_not_ready()
            self._rtsp_server.disconnect_clients()
        # Restart VIP listener now that video released the CTPP slot.
        # Skip if we're inside async_start_video (lock already held) —
        # start_video will stop VIP again immediately anyway.
        if not self._video_start_lock.locked():
            await self._ensure_vip_listener()
        await self._notify_video_state_change()

    @property
    def video_session(self) -> VideoCallSession | None:
        """Return the active video call session, if any."""
        return self._video_session

    @property
    def video_session_purpose(self) -> str | None:
        """Return whether the current media session serves live video or a still."""
        return self._video_session_purpose

    @property
    def video_stream_revision(self) -> int:
        """Return a monotonic identifier for the current media session."""
        return self._video_stream_revision

    def _on_client_disconnect(self, client: IconaBridgeClient) -> None:
        """Called by the TCP client when the connection drops unexpectedly.

        Schedules an immediate coordinator refresh so _async_update_data runs
        within milliseconds and triggers reconnect, instead of waiting for the
        next 30-second polling interval.
        """
        if self._shutting_down or client is not self._client:
            _LOGGER.debug("Ignoring disconnect from stale Comelit client")
            return
        if self._video_session and self._video_session.active:
            self._record_unexpected_video_end()
        if not self._connection_lost:
            _LOGGER.warning("Comelit device disconnected — attempting reconnect")
            self._connection_lost = True
        self.config_entry.async_create_background_task(  # type: ignore[union-attr]
            self.hass, self.async_request_refresh(), "comelit-reconnect-refresh"
        )

    def _record_unexpected_video_end(self) -> None:
        """Expose a device-ended call to entities, automations, and the UI."""
        self._last_video_end_reason = (
            "The active intercom session ended unexpectedly. The panel may have "
            "timed out, the network connection may have been interrupted, or "
            "another client may have taken control."
        )
        self._last_video_end_at = datetime.now(UTC).isoformat()
        self._on_push_event(
            PushEvent(
                event_type="call_ended",
                timestamp=time.time(),
                raw={"reason": self._last_video_end_reason},
            )
        )
        persistent_notification.async_create(
            self.hass,
            self._last_video_end_reason,
            title="Comelit call ended",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_call_ended",
        )

    async def _async_update_data(self) -> DeviceConfig:
        """Health-check the connection; reconnect if needed."""
        if self._client and self._client.connected and self._config:
            return self._config

        # Connection lost or no config — attempt reconnect.
        # One-shot warning: _on_client_disconnect may have already fired it;
        # set the flag here too for cases where the socket died silently.
        if not self._connection_lost:
            _LOGGER.warning("Comelit device disconnected, attempting reconnect")
            self._connection_lost = True

        try:
            await self._reconnect()
        except AuthenticationError as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "auth_failed",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="auth_failed",
                translation_placeholders={"name": self.device_name},
            )
            raise ConfigEntryAuthFailed(
                "Authentication failed — update the token"
            ) from err
        except Exception as err:
            raise UpdateFailed(f"Reconnect failed: {err}") from err

        return self._config  # type: ignore[return-value]
