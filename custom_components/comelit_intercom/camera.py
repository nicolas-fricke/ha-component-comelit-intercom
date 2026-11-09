"""Camera platform for the Comelit intercom live video feed."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    CameraWebRTCProvider,
)
from homeassistant.components.camera.webrtc import (
    DATA_WEBRTC_PROVIDERS,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    VIDEO_PURPOSE_LIVE,
    VIDEO_PURPOSE_SNAPSHOT,
    ComelitDataUpdateCoordinator,
)
from .placeholder import PLACEHOLDER_JPEG

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_MAX_AGE = 15.0
SNAPSHOT_FIRST_LOAD_TIMEOUT = 9.0
LIVE_VIEW_MAX_SECONDS = 180.0
LIVE_VIEW_CLOSE_GRACE = 3.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the intercom camera entity."""
    coordinator: ComelitDataUpdateCoordinator = entry.runtime_data
    if coordinator.device_config and coordinator.device_config.apt_address:
        async_add_entities([ComelitIntercomCamera(coordinator)])


class ComelitIntercomCamera(Camera):
    """Live camera stream initiated through the ICONA Bridge protocol."""

    _attr_has_entity_name = True
    _attr_name = "Live feed"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: ComelitDataUpdateCoordinator) -> None:
        """Initialize the camera."""
        super().__init__()
        self._coordinator = coordinator
        entry_unique_id = coordinator.entry.unique_id or coordinator.host
        self._attr_unique_id = f"{entry_unique_id}_intercom_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_unique_id)},
            name=f"Comelit Intercom ({coordinator.host})",
            manufacturer="Comelit",
            model="ICONA Bridge",
        )
        self._remove_stop_callback: Callable[[], None] | None = None
        self._remove_state_callback: Callable[[], None] | None = None
        self._webrtc_sessions: dict[str, CameraWebRTCProvider] = {}
        self._owned_webrtc_sessions: set[str] = set()
        self._last_image: bytes | None = None
        self._last_image_monotonic = 0.0
        self._snapshot_task: asyncio.Task[bytes | None] | None = None
        self._live_stop_task: asyncio.Task[None] | None = None
        self._live_stop_committed = False

    @property
    def available(self) -> bool:
        """Return whether the local RTSP relay initialized successfully."""
        return self._coordinator.video_available

    @property
    def is_streaming(self) -> bool:
        """Return whether a user-visible live intercom stream is active."""
        session = self._coordinator.video_session
        return bool(
            session
            and session.active
            and self._coordinator.video_session_purpose != VIDEO_PURPOSE_SNAPSHOT
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the reason an unexpected camera session ended."""
        return {
            "last_end_reason": self._coordinator.last_video_end_reason,
            "last_end_at": self._coordinator.last_video_end_at,
        }

    async def stream_source(self) -> str | None:
        """Start the panel stream on demand and return its local RTSP relay."""
        self._cancel_live_stop()
        if not self.is_streaming:
            try:
                await self._coordinator.async_start_video(
                    auto_timeout=False,
                    by_user=True,
                    purpose=VIDEO_PURPOSE_LIVE,
                )
            except Exception:
                _LOGGER.warning(
                    "Unable to start the Comelit video stream", exc_info=True
                )
                return None
        self._schedule_live_stop(LIVE_VIEW_MAX_SECONDS)

        source = self._coordinator.rtsp_url
        if rtsp_server := self._coordinator.rtsp_server:
            rtsp_server.set_audio_enabled(False)
        if source is None:
            return None

        # Home Assistant's go2rtc provider keeps producers registered by URL.
        # A stable URL can therefore reconnect the RTSP socket after a short
        # snapshot session while leaving a new WebRTC viewer attached to the
        # old producer (the panel streams, but the browser stays on its poster).
        # Keep the URL stable within one media session and change it between
        # sessions so go2rtc replaces the producer for every new live view.
        separator = "&" if "?" in source else "?"
        revision = self._coordinator.video_stream_revision
        live_source = f"{source}{separator}session={revision}"
        _LOGGER.debug("Live RTSP source revision %d", revision)
        return live_source

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a cached still, refreshing it with a short panel session."""
        session = self._coordinator.video_session
        if session and session.active and session.rtp_receiver:
            receiver = session.rtp_receiver
            frame = receiver.latest_frame
            if frame is None:
                try:
                    async with asyncio.timeout(2.0):
                        frame = await receiver.get_jpeg_frame()
                except TimeoutError:
                    frame = receiver.latest_frame
            if frame:
                self._remember_image(frame)
                return frame

        if (
            self._last_image
            and time.monotonic() - self._last_image_monotonic < SNAPSHOT_MAX_AGE
        ):
            return self._last_image

        if self._snapshot_task is None or self._snapshot_task.done():
            self._snapshot_task = asyncio.create_task(self._async_refresh_snapshot())

        # Once a still exists, return it immediately and refresh in the
        # background. The first request waits briefly so a newly added camera
        # shows a real image rather than the placeholder whenever possible.
        if self._last_image:
            return self._last_image
        try:
            async with asyncio.timeout(SNAPSHOT_FIRST_LOAD_TIMEOUT):
                frame = await asyncio.shield(self._snapshot_task)
            return frame or PLACEHOLDER_JPEG
        except TimeoutError:
            return PLACEHOLDER_JPEG
        except Exception:
            _LOGGER.debug("Unable to obtain the first Comelit still", exc_info=True)
            return PLACEHOLDER_JPEG

    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Start media on demand and delegate WebRTC to HA's provider."""
        self._cancel_live_stop()
        self._owned_webrtc_sessions.add(session_id)
        self._coordinator.add_live_viewer(session_id)
        delegated = False
        try:
            source = await self.stream_source()
            for provider in self.hass.data.get(DATA_WEBRTC_PROVIDERS, set()):
                if source and provider.async_is_supported(source):
                    self._webrtc_sessions[session_id] = provider
                    await provider.async_handle_async_webrtc_offer(
                        self, offer_sdp, session_id, send_message
                    )
                    delegated = True
                    return
            send_message(
                WebRTCError(
                    code="webrtc_provider_unavailable",
                    message="Home Assistant's go2rtc WebRTC provider is unavailable",
                )
            )
        finally:
            if not delegated:
                self._webrtc_sessions.pop(session_id, None)
                self._release_webrtc_viewer(session_id)
                if not self._owned_webrtc_sessions:
                    self._schedule_live_stop(0)

    async def async_on_webrtc_candidate(self, session_id: str, candidate: Any) -> None:
        """Forward a trickled ICE candidate to Home Assistant's provider."""
        if provider := self._webrtc_sessions.get(session_id):
            await provider.async_on_webrtc_candidate(session_id, candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close the delegated WebRTC session."""
        if provider := self._webrtc_sessions.pop(session_id, None):
            provider.async_close_session(session_id)
        self._release_webrtc_viewer(session_id)
        if not self._owned_webrtc_sessions:
            self._schedule_live_stop(LIVE_VIEW_CLOSE_GRACE)

    async def async_added_to_hass(self) -> None:
        """Register callbacks for video lifecycle changes."""
        await super().async_added_to_hass()
        self._remove_stop_callback = self._coordinator.add_stop_video_callback(
            self._async_stop_ha_stream
        )
        self._remove_state_callback = self._coordinator.add_video_state_change_callback(
            self._async_video_state_changed
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callbacks when the entity is removed."""
        for task in (self._snapshot_task, self._live_stop_task):
            if task and not task.done():
                task.cancel()
        if self._remove_stop_callback:
            self._remove_stop_callback()
            self._remove_stop_callback = None
        if self._remove_state_callback:
            self._remove_state_callback()
            self._remove_state_callback = None
        for session_id in tuple(self._owned_webrtc_sessions):
            self._release_webrtc_viewer(session_id)
        await super().async_will_remove_from_hass()

    def _release_webrtc_viewer(self, session_id: str) -> None:
        """Release local and coordinator ownership for one WebRTC session."""
        self._owned_webrtc_sessions.discard(session_id)
        self._coordinator.remove_live_viewer(session_id)

    async def _async_video_state_changed(self) -> None:
        """Refresh HA's stream provider and camera state."""
        refresh_providers = getattr(self, "async_refresh_providers", None)
        if refresh_providers is not None:
            await refresh_providers()
        self.async_write_ha_state()

    async def _async_stop_ha_stream(self) -> None:
        """Stop HA's cached stream before the RTSP socket is disconnected."""
        stream: Any = getattr(self, "stream", None)
        if stream is None:
            return
        self.stream = None
        try:
            await stream.stop()
        except Exception:
            _LOGGER.debug(
                "Error stopping the Home Assistant camera stream", exc_info=True
            )

    async def _async_refresh_snapshot(self) -> bytes | None:
        """Capture one still through a short receive-only ICONA session."""
        try:
            frame = await self._coordinator.async_capture_video_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Unable to refresh the Comelit still", exc_info=True)
            return self._last_image
        if frame:
            self._remember_image(frame)
        return frame

    def _remember_image(self, frame: bytes) -> None:
        """Store a JPEG and its freshness timestamp."""
        self._last_image = frame
        self._last_image_monotonic = time.monotonic()

    def _cancel_live_stop(self) -> None:
        """Cancel the pending automatic live-view release."""
        if (
            self._live_stop_task
            and not self._live_stop_task.done()
            and not self._live_stop_committed
        ):
            self._live_stop_task.cancel()
            self._live_stop_task = None

    def _schedule_live_stop(self, delay: float) -> None:
        """Release live video after the viewer closes or a safety timeout."""
        self._cancel_live_stop()
        self._live_stop_committed = False
        self._live_stop_task = asyncio.create_task(self._async_stop_live_after(delay))

    async def _async_stop_live_after(self, delay: float) -> None:
        """Stop a viewer-owned stream after a grace period."""
        try:
            await asyncio.sleep(delay)
            if self._owned_webrtc_sessions:
                return
            if self._coordinator.video_session_purpose != VIDEO_PURPOSE_LIVE:
                return
            self._live_stop_committed = True
            await self._coordinator.async_release_live_video()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.debug("Unable to release the Comelit live view", exc_info=True)
        finally:
            self._live_stop_committed = False
