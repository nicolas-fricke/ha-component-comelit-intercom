"""Tests for the split video and exterior-audio lifecycle."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import logging
import struct
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "comelit_intercom"
)

try:
    import homeassistant.components  # noqa: F401
except ImportError:
    HAS_HOMEASSISTANT = False
else:
    HAS_HOMEASSISTANT = True

requires_homeassistant = pytest.mark.skipif(
    not HAS_HOMEASSISTANT,
    reason="Home Assistant is required for coordinator and entity tests",
)


def _package(name: str, path: Path) -> None:
    """Load a package namespace without executing the HA integration setup."""
    if name in sys.modules:
        return
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package


try:
    from homeassistant.exceptions import HomeAssistantError as _HomeAssistantError
except ImportError:
    homeassistant = types.ModuleType("homeassistant")
    exceptions = types.ModuleType("homeassistant.exceptions")

    class _HomeAssistantError(Exception):
        """Minimal Home Assistant exception used by protocol tests."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args)

    exceptions.HomeAssistantError = _HomeAssistantError
    homeassistant.exceptions = exceptions
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.exceptions"] = exceptions


_package("custom_components", COMPONENT_DIR.parent)
_package("custom_components.comelit_intercom", COMPONENT_DIR)
_package("custom_components.comelit_intercom.video", COMPONENT_DIR / "video")

if HAS_HOMEASSISTANT:
    from custom_components.comelit_intercom import camera as camera_module
    from custom_components.comelit_intercom import coordinator as coordinator_module
    from custom_components.comelit_intercom.entity_migration import (
        beta_door_unique_id,
        door_unique_id,
    )

    ComelitIntercomCamera = camera_module.ComelitIntercomCamera
from custom_components.comelit_intercom.video.channels import (  # noqa: E402
    Channel,
    ChannelType,
)
from custom_components.comelit_intercom.video.client import (  # noqa: E402
    IconaBridgeClient,
)
from custom_components.comelit_intercom.video.exceptions import (  # noqa: E402
    VideoCallError,
)
from custom_components.comelit_intercom.video.models import (  # noqa: E402
    DeviceConfig,
    Door,
)
from custom_components.comelit_intercom.video.protocol import (  # noqa: E402
    encode_self_view_audio_config_ack,
    encode_self_view_audio_peer,
)
from custom_components.comelit_intercom.video.rtp_receiver import (  # noqa: E402
    RtpReceiver,
)
from custom_components.comelit_intercom.video.rtsp_server import (  # noqa: E402
    LocalRtspServer,
    _TcpClient,
)
from custom_components.comelit_intercom.video.video_call import (  # noqa: E402
    VideoCallSession,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_actuator", [False, True])
@requires_homeassistant
async def test_door_buttons_use_proven_dedicated_legacy_sequence(
    is_actuator: bool,
) -> None:
    """Video state must not replace the door sequence that works on the device."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator.host = "192.0.2.1"
    coordinator.port = 64100
    coordinator.token = "token"
    coordinator._config = DeviceConfig(
        raw={"vip": {"apt-address": "SB000001", "apt-subaddress": 1}}
    )
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._transport_lock = asyncio.Lock()
    coordinator._shutting_down = False
    coordinator._video_session = None
    coordinator._video_session_purpose = None
    coordinator._video_stopped_by_user = False
    coordinator._live_viewers = set()
    coordinator._vip_listener = None
    coordinator._client = MagicMock()
    coordinator._client.disconnect = AsyncMock()
    coordinator._client.set_disconnect_callback = MagicMock()
    coordinator._cancel_keepalive = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator._reconnect_locked = AsyncMock()
    coordinator._on_push_event = MagicMock()
    door = Door(
        id=1,
        index=1,
        name="Gate" if is_actuator else "Entrance",
        apt_address="SBIO0255" if is_actuator else "SB100001",
        output_index=1,
        is_actuator=is_actuator,
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.authenticate = AsyncMock(return_value=200)
    client.open_door = AsyncMock()
    client.open_actuator = AsyncMock()
    client.shutdown = AsyncMock()

    with patch.object(coordinator_module, "LegacyDoorClient", return_value=client):
        await coordinator.async_open_door(door)

    client.connect.assert_awaited_once()
    client.authenticate.assert_awaited_once_with("token")
    if is_actuator:
        client.open_actuator.assert_awaited_once()
        client.open_door.assert_not_awaited()
    else:
        client.open_door.assert_awaited_once()
        client.open_actuator.assert_not_awaited()
    client.shutdown.assert_awaited_once()
    coordinator.async_stop_video.assert_awaited_once_with(
        reason="door control", reset_transport=False
    )
    assert coordinator._client is None
    coordinator._reconnect_locked.assert_awaited_once()
    coordinator._on_push_event.assert_called_once()


@pytest.mark.asyncio
@requires_homeassistant
async def test_failed_door_command_still_restores_persistent_transport() -> None:
    """A rejected legacy command must not leave push and video disconnected."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator.host = "192.0.2.1"
    coordinator.port = 64100
    coordinator.token = "token"
    coordinator._config = DeviceConfig(raw={"vip": {}})
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._transport_lock = asyncio.Lock()
    coordinator._shutting_down = False
    coordinator._video_session = None
    coordinator._video_session_purpose = None
    coordinator._video_stopped_by_user = False
    coordinator._live_viewers = set()
    coordinator._vip_listener = None
    coordinator._client = None
    coordinator._cancel_keepalive = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator._reconnect_locked = AsyncMock()
    coordinator._on_push_event = MagicMock()
    door = Door(
        id=1,
        index=1,
        name="Entrance",
        apt_address="SB100001",
        output_index=1,
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.authenticate = AsyncMock(return_value=200)
    client.open_door = AsyncMock(side_effect=ConnectionResetError("panel busy"))
    client.shutdown = AsyncMock()

    with (
        patch.object(coordinator_module, "LegacyDoorClient", return_value=client),
        pytest.raises(_HomeAssistantError, match="panel busy"),
    ):
        await coordinator.async_open_door(door)

    client.shutdown.assert_awaited_once()
    coordinator._reconnect_locked.assert_awaited_once()
    coordinator._on_push_event.assert_not_called()


@pytest.mark.asyncio
@requires_homeassistant
async def test_successful_door_event_survives_transport_restore_failure() -> None:
    """Report physical door success even if rebuilding push/video fails."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator.host = "192.0.2.1"
    coordinator.port = 64100
    coordinator.token = "token"
    coordinator._config = DeviceConfig(raw={"vip": {}})
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._transport_lock = asyncio.Lock()
    coordinator._shutting_down = False
    coordinator._video_session = None
    coordinator._video_session_purpose = None
    coordinator._video_stopped_by_user = False
    coordinator._live_viewers = set()
    coordinator._vip_listener = None
    coordinator._client = None
    coordinator._connection_lost = False
    coordinator._cancel_keepalive = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator._reconnect_locked = AsyncMock(
        side_effect=ConnectionResetError("restore failed")
    )
    coordinator._on_push_event = MagicMock()
    door = Door(
        id=1,
        index=1,
        name="Entrance",
        apt_address="SB100001",
        output_index=1,
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.authenticate = AsyncMock(return_value=200)
    client.open_door = AsyncMock()
    client.shutdown = AsyncMock()

    with (
        patch.object(coordinator_module, "LegacyDoorClient", return_value=client),
        pytest.raises(_HomeAssistantError, match="could not be restored"),
    ):
        await coordinator.async_open_door(door)

    coordinator._on_push_event.assert_called_once()
    assert coordinator._on_push_event.call_args.args[0].event_type == "door_opened"
    assert coordinator._connection_lost is True


@pytest.mark.asyncio
@requires_homeassistant
async def test_door_control_resumes_live_video_started_while_waiting() -> None:
    """Snapshot live state only after door control owns the video-start lock."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator.host = "192.0.2.1"
    coordinator.port = 64100
    coordinator.token = "token"
    coordinator._config = DeviceConfig(raw={"vip": {}})
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._transport_lock = asyncio.Lock()
    coordinator._shutting_down = False
    coordinator._video_session = None
    coordinator._video_session_purpose = None
    coordinator._video_stopped_by_user = False
    coordinator._live_viewers = set()
    coordinator._vip_listener = None
    coordinator._client = None
    coordinator._cancel_keepalive = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator.async_start_video = AsyncMock()
    coordinator._reconnect_locked = AsyncMock()
    coordinator._on_push_event = MagicMock()
    door = Door(
        id=1,
        index=1,
        name="Entrance",
        apt_address="SB100001",
        output_index=1,
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.authenticate = AsyncMock(return_value=200)
    client.open_door = AsyncMock()
    client.shutdown = AsyncMock()

    await coordinator._video_start_lock.acquire()
    with patch.object(coordinator_module, "LegacyDoorClient", return_value=client):
        door_task = asyncio.create_task(coordinator.async_open_door(door))
        await asyncio.sleep(0)
        coordinator._video_session = MagicMock(active=True)
        coordinator._video_session_purpose = coordinator_module.VIDEO_PURPOSE_LIVE
        coordinator._live_viewers.add("viewer")
        coordinator._video_start_lock.release()
        await door_task

    coordinator.async_start_video.assert_awaited_once_with(
        auto_timeout=False, purpose=coordinator_module.VIDEO_PURPOSE_LIVE
    )


@pytest.mark.asyncio
@requires_homeassistant
async def test_shutdown_during_door_command_does_not_reconnect() -> None:
    """Unload waits for door ownership and prevents post-unload resource recreation."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator.host = "192.0.2.1"
    coordinator.port = 64100
    coordinator.token = "token"
    coordinator._config = DeviceConfig(raw={"vip": {}})
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._transport_lock = asyncio.Lock()
    coordinator._shutting_down = False
    coordinator._video_session = None
    coordinator._video_session_purpose = None
    coordinator._video_stopped_by_user = False
    coordinator._live_viewers = set()
    coordinator._vip_listener = None
    coordinator._rtsp_server = None
    coordinator._rtsp_url = None
    coordinator._client = MagicMock()
    coordinator._client.disconnect = AsyncMock()
    coordinator._client.set_disconnect_callback = MagicMock()
    coordinator._cancel_keepalive = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator._reconnect_locked = AsyncMock()
    coordinator._on_push_event = MagicMock()
    door = Door(
        id=1,
        index=1,
        name="Entrance",
        apt_address="SB100001",
        output_index=1,
    )
    command_started = asyncio.Event()
    release_command = asyncio.Event()

    async def _open_door(*_args: object) -> None:
        command_started.set()
        await release_command.wait()

    client = MagicMock()
    client.connect = AsyncMock()
    client.authenticate = AsyncMock(return_value=200)
    client.open_door = AsyncMock(side_effect=_open_door)
    client.shutdown = AsyncMock()

    with patch.object(coordinator_module, "LegacyDoorClient", return_value=client):
        door_task = asyncio.create_task(coordinator.async_open_door(door))
        await command_started.wait()
        shutdown_task = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        assert coordinator._shutting_down is True
        release_command.set()
        await asyncio.gather(door_task, shutdown_task)

    coordinator._reconnect_locked.assert_not_awaited()
    assert coordinator._client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
@requires_homeassistant
async def test_concurrent_reconnects_replace_transport_only_once(force: bool) -> None:
    """Refreshes observing one outage must not evict each other's new client."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator._transport_lock = asyncio.Lock()
    coordinator._shutting_down = False
    coordinator._client = None
    coordinator._config = MagicMock()

    async def _restore() -> None:
        await asyncio.sleep(0)
        coordinator._client = MagicMock(connected=True)

    coordinator._reconnect_locked = AsyncMock(side_effect=_restore)

    await asyncio.gather(
        coordinator._reconnect(force=force), coordinator._reconnect(force=force)
    )

    coordinator._reconnect_locked.assert_awaited_once()


@requires_homeassistant
def test_stale_client_disconnect_does_not_start_another_reconnect() -> None:
    """A late callback from a retired socket cannot replace the live socket."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator._client = MagicMock()
    coordinator._shutting_down = False
    coordinator._connection_lost = False
    coordinator._video_session = None
    coordinator.config_entry = MagicMock()

    coordinator._on_client_disconnect(MagicMock())

    assert coordinator._connection_lost is False
    coordinator.config_entry.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_receive_loop_treats_socket_loss_as_expected_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A network reset should notify the coordinator without an error traceback."""
    client = IconaBridgeClient("192.0.2.1")
    client._connected = True
    client._read_packet = AsyncMock(side_effect=ConnectionResetError("reset"))
    callback = MagicMock()
    client.set_disconnect_callback(callback)

    with caplog.at_level(logging.ERROR):
        await client._receive_loop()

    callback.assert_called_once()
    assert not caplog.records


def _camera_coordinator() -> MagicMock:
    """Return the minimum coordinator contract required by the camera entity."""
    coordinator = MagicMock()
    coordinator.entry.unique_id = "test-entry"
    coordinator.host = "192.0.2.1"
    coordinator.video_session = None
    coordinator.video_session_purpose = None
    coordinator.video_stream_revision = 7
    coordinator.rtsp_server = None
    coordinator.rtsp_url = "rtsp://127.0.0.1:12345/intercom"
    return coordinator


@pytest.mark.asyncio
@requires_homeassistant
async def test_camera_uses_a_cached_still_without_reopening_the_panel() -> None:
    """Dashboard image polling reuses a JPEG during the cache interval."""
    coordinator = _camera_coordinator()
    jpeg = b"\xff\xd8current-still\xff\xd9"
    coordinator.async_capture_video_snapshot = AsyncMock(return_value=jpeg)
    camera = ComelitIntercomCamera(coordinator)

    assert await camera.async_camera_image() == jpeg
    assert await camera.async_camera_image() == jpeg

    coordinator.async_capture_video_snapshot.assert_awaited_once()


@pytest.mark.asyncio
@requires_homeassistant
async def test_camera_is_created_without_any_door_controls() -> None:
    """Video availability is independent of optional door address books."""
    coordinator = _camera_coordinator()
    coordinator.device_config = DeviceConfig(apt_address="SB000001", doors=[])
    entry = MagicMock(runtime_data=coordinator)
    add_entities = MagicMock()

    await camera_module.async_setup_entry(MagicMock(), entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert len(entities) == 1
    assert isinstance(entities[0], ComelitIntercomCamera)


@pytest.mark.asyncio
@requires_homeassistant
async def test_camera_live_view_starts_without_video_buttons() -> None:
    """Opening the native camera dialog starts a viewer-owned live session."""
    coordinator = _camera_coordinator()
    coordinator.async_start_video = AsyncMock()
    camera = ComelitIntercomCamera(coordinator)

    try:
        assert await camera.stream_source() == f"{coordinator.rtsp_url}?session=7"
    finally:
        camera._cancel_live_stop()

    coordinator.async_start_video.assert_awaited_once_with(
        auto_timeout=False,
        by_user=True,
        purpose=coordinator_module.VIDEO_PURPOSE_LIVE,
    )


@pytest.mark.asyncio
@requires_homeassistant
async def test_camera_source_changes_between_panel_sessions() -> None:
    """A new panel session must replace go2rtc's previous producer."""
    coordinator = _camera_coordinator()
    coordinator.video_session = MagicMock(active=True)
    coordinator.video_session_purpose = coordinator_module.VIDEO_PURPOSE_LIVE
    camera = ComelitIntercomCamera(coordinator)

    try:
        first_source = await camera.stream_source()
        coordinator.video_stream_revision = 8
        second_source = await camera.stream_source()
    finally:
        camera._cancel_live_stop()

    assert first_source == f"{coordinator.rtsp_url}?session=7"
    assert second_source == f"{coordinator.rtsp_url}?session=8"


@pytest.mark.asyncio
@requires_homeassistant
async def test_camera_releases_live_video_after_viewer_closes() -> None:
    """The panel is released automatically instead of requiring Stop video."""
    coordinator = _camera_coordinator()
    coordinator.video_session_purpose = coordinator_module.VIDEO_PURPOSE_LIVE
    coordinator.async_release_live_video = AsyncMock()
    camera = ComelitIntercomCamera(coordinator)

    await camera._async_stop_live_after(0)

    coordinator.async_release_live_video.assert_awaited_once()


@pytest.mark.asyncio
@requires_homeassistant
async def test_live_release_waits_for_every_webrtc_viewer() -> None:
    """Closing one card must not stop another card's camera session."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator._live_viewers = {"viewer-a", "viewer-b"}
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._video_session_purpose = coordinator_module.VIDEO_PURPOSE_LIVE
    coordinator.request_video_stop = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator._ensure_vip_listener = AsyncMock()

    coordinator.remove_live_viewer("viewer-a")
    await coordinator.async_release_live_video()
    coordinator.async_stop_video.assert_not_awaited()

    coordinator.remove_live_viewer("viewer-b")
    await coordinator.async_release_live_video()
    coordinator.async_stop_video.assert_awaited_once_with(reason="live viewer closed")


def test_doorbell_events_do_not_answer_or_start_video() -> None:
    """The VIP listener may report a ring but must remain camera-only."""
    coordinator_source = (COMPONENT_DIR / "coordinator.py").read_text(encoding="utf-8")
    assert "on_inbound_ring=" not in coordinator_source
    assert "async_start_inbound_video" not in coordinator_source


@requires_homeassistant
def test_door_unique_id_preserves_pre_camera_identity() -> None:
    """Normal doors retain opendoor and omit an absent module suffix."""
    door = Door(
        id=0,
        index=0,
        name="Entrance",
        apt_address="SB100001",
        output_index=1,
    )

    assert door_unique_id("entry", door) == "entry_opendoor_SB100001_1"
    assert beta_door_unique_id("entry", door) == "entry_door_SB100001_1_0"


@pytest.mark.asyncio
@requires_homeassistant
async def test_live_request_promotes_snapshot_session() -> None:
    """A click during still capture reuses the negotiated panel session."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator._config = MagicMock()
    coordinator._client = MagicMock()
    coordinator._video_start_lock = asyncio.Lock()
    session = MagicMock(active=True)
    coordinator._video_session = session
    coordinator._video_session_purpose = coordinator_module.VIDEO_PURPOSE_SNAPSHOT

    result = await coordinator.async_start_video(
        auto_timeout=False,
        by_user=True,
        purpose=coordinator_module.VIDEO_PURPOSE_LIVE,
    )

    assert result is session
    assert coordinator.video_session_purpose == coordinator_module.VIDEO_PURPOSE_LIVE


@pytest.mark.asyncio
@requires_homeassistant
async def test_video_start_honors_stop_requested_during_negotiation() -> None:
    """A hidden dashboard must not publish a session that just became ready."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    coordinator._config = MagicMock()
    coordinator._client = MagicMock(connected=True)
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._video_session = None
    coordinator._video_session_purpose = None
    coordinator._video_stopped_by_user = False
    coordinator._vip_listener = None
    coordinator._rtsp_server = MagicMock()
    coordinator.async_stop_video = AsyncMock()
    coordinator._ensure_vip_listener = AsyncMock()

    session = MagicMock(cleanup_requires_reconnect=False)

    async def _finish_after_stop_request() -> None:
        coordinator.request_video_stop()

    session.start = AsyncMock(side_effect=_finish_after_stop_request)
    session.stop = AsyncMock()

    with (
        patch.object(coordinator_module, "VideoCallSession", return_value=session),
        pytest.raises(RuntimeError, match="cancelled"),
    ):
        await coordinator.async_start_video(by_user=True)

    session.stop.assert_awaited_once_with(reason="cancelled while starting")
    coordinator._rtsp_server.mark_not_ready.assert_called_once()
    coordinator._rtsp_server.disconnect_clients.assert_called_once()
    coordinator._ensure_vip_listener.assert_awaited_once()
    assert coordinator._video_session is None


@pytest.mark.asyncio
@requires_homeassistant
async def test_short_snapshot_session_is_released_after_one_frame() -> None:
    """A still capture does not leave the intercom media channel occupied."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    jpeg = b"\xff\xd8snapshot\xff\xd9"
    receiver = MagicMock(latest_frame=jpeg)
    session = MagicMock(rtp_receiver=receiver)
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._video_session = session
    coordinator._video_session_purpose = coordinator_module.VIDEO_PURPOSE_SNAPSHOT
    coordinator.async_start_video = AsyncMock(return_value=session)
    coordinator.async_stop_video = AsyncMock()
    coordinator._ensure_vip_listener = AsyncMock()

    assert await coordinator.async_capture_video_snapshot() == jpeg

    coordinator.async_stop_video.assert_awaited_once_with(reason="snapshot captured")
    coordinator._ensure_vip_listener.assert_awaited_once()


@pytest.mark.asyncio
@requires_homeassistant
async def test_short_snapshot_session_is_released_without_a_receiver() -> None:
    """An incomplete still session must not leave the intercom occupied."""
    coordinator = coordinator_module.ComelitDataUpdateCoordinator.__new__(
        coordinator_module.ComelitDataUpdateCoordinator
    )
    session = MagicMock(rtp_receiver=None)
    coordinator._video_start_lock = asyncio.Lock()
    coordinator._video_session = session
    coordinator._video_session_purpose = coordinator_module.VIDEO_PURPOSE_SNAPSHOT
    coordinator.async_start_video = AsyncMock(return_value=session)
    coordinator.async_stop_video = AsyncMock()
    coordinator._ensure_vip_listener = AsyncMock()

    assert await coordinator.async_capture_video_snapshot() is None

    coordinator.async_stop_video.assert_awaited_once_with(reason="snapshot captured")
    coordinator._ensure_vip_listener.assert_awaited_once()


def test_integration_exposes_camera_doors_and_preview_controls() -> None:
    """The stable camera keeps doors and adds privacy preview settings."""
    setup_source = (COMPONENT_DIR / "__init__.py").read_text(encoding="utf-8")
    for platform in ("BUTTON", "CAMERA", "NUMBER", "SWITCH"):
        assert f"Platform.{platform}" in setup_source
    assert "_remove_obsolete_intercom_entities" in setup_source
    button_source = (COMPONENT_DIR / "button.py").read_text(encoding="utf-8")
    assert "ComelitDoorButton" in button_source
    assert "ComelitStartVideoButton" not in button_source
    assert "ComelitStopVideoButton" not in button_source
    assert not (COMPONENT_DIR / "event.py").exists()
    assert (COMPONENT_DIR / "number.py").exists()
    assert (COMPONENT_DIR / "switch.py").exists()
    card_source = (COMPONENT_DIR / "www" / "comelit-intercom-card.js").read_text(
        encoding="utf-8"
    )
    assert "class ComelitIntercomCard" in card_source
    assert 'class="play"' not in card_source
    assert "Still ·" not in card_source


@pytest.mark.asyncio
async def test_channel_close_waits_for_device_release() -> None:
    """A video channel is not reusable until the panel completes its END flow."""
    client = IconaBridgeClient("192.0.2.1")
    writer = MagicMock()
    writer.drain = AsyncMock()
    client._writer = writer
    channel = Channel(
        name="RTPC2",
        channel_type=ChannelType.UAUT,
        request_id=123,
        server_channel_id=0x2103,
        sequence=3,
        is_open=True,
    )
    client._channels[channel.name] = channel

    close_task = asyncio.create_task(client.close_channel(channel.name))
    await asyncio.sleep(0)

    assert close_task.done() is False
    close_packet = writer.write.call_args_list[0].args[0]
    assert close_packet[4:6] == struct.pack("<H", channel.server_channel_id)
    assert close_packet[8:12] == struct.pack("<HH", 0x01EF, 3)

    device_end = struct.pack("<HHIH", 0x01EF, 3, 2, channel.server_channel_id)
    client._dispatch(0, device_end)

    assert await close_task is True
    assert channel.name not in client._channels
    close_ack = writer.write.call_args_list[1].args[0]
    assert close_ack[8:18] == struct.pack(
        "<HHIH", 0x01EF, 4, 4, channel.server_channel_id
    )


@pytest.mark.asyncio
async def test_video_cleanup_closes_remote_media_channels() -> None:
    """Cleanup sends ENDs and requests a reset when one is not acknowledged."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._active = True
    session._timeout_task = None
    session._tcp_task = None
    session._ctpp_task = None
    session._rtp_receiver = None
    session._rtsp_server = None
    session._external_rtsp = True
    session._owns_ctpp = False
    session._cleanup_requires_reconnect = False
    client = MagicMock()
    client.close_channel = AsyncMock(side_effect=lambda name: name != "RTPC2")
    session._client = client

    await session._cleanup()

    closed_names = {call.args[0] for call in client.close_channel.await_args_list}
    assert closed_names == {
        "UDPM",
        "RTPC",
        "RTPC2",
        "RTPC_DEVICE",
        "RTPC_DEVICE_REEST",
        "RTPC_AUDIO",
    }
    assert session.cleanup_requires_reconnect is True


@pytest.mark.asyncio
async def test_video_client_redacts_authentication_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Debug logging must not expose the credential used by authentication."""
    client = IconaBridgeClient("192.0.2.1")
    writer = MagicMock()
    writer.drain = AsyncMock()
    client._writer = writer
    channel = Channel(
        name="UAUT",
        channel_type=ChannelType.UAUT,
        request_id=123,
        server_channel_id=0x2104,
        is_open=True,
    )
    client._channels[channel.name] = channel
    token = "secret-token-that-must-not-be-logged"

    with caplog.at_level(logging.DEBUG):
        response_task = asyncio.create_task(
            client.send_json(
                channel,
                {
                    "message": "access",
                    "user-token": token,
                    "message-type": "request",
                },
            )
        )
        await asyncio.sleep(0)
        client._dispatch(channel.server_channel_id, b'{"response-code":200}')
        await response_task

    assert token not in caplog.text
    assert "<redacted>" in caplog.text
    assert b"<redacted>" not in writer.write.call_args.args[0]


def test_rtsp_is_video_only_until_two_way_audio_is_enabled() -> None:
    """Normal viewing must retain the first beta's proven video-only SDP."""
    server = LocalRtspServer()

    assert "m=video" in server._build_sdp()
    assert "m=audio" not in server._build_sdp()

    server.set_audio_enabled(True)

    sdp = server._build_sdp()
    assert sdp.count("m=audio") == 2
    exterior_audio, microphone = sdp.split("m=audio")[1:]
    assert "a=recvonly" in exterior_audio
    assert "a=sendonly" not in exterior_audio
    assert "a=sendonly" in microphone
    assert "a=recvonly" not in microphone


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name", ["RTPC", "RTPC_DEVICE"])
async def test_tcp_media_router_reads_both_exterior_audio_paths(
    source_name: str,
) -> None:
    """Panel audio is accepted on either firmware-dependent RTPC downlink."""
    app_audio_channel = Channel(
        name="RTPC",
        channel_type=ChannelType.UAUT,
        request_id=1,
        server_channel_id=2,
        is_open=True,
    )
    device_audio_channel = Channel(
        name="RTPC_DEVICE",
        channel_type=ChannelType.UAUT,
        request_id=0,
        server_channel_id=3,
        is_open=True,
    )
    video_channel = Channel(
        name="RTPC2",
        channel_type=ChannelType.UAUT,
        request_id=4,
        server_channel_id=5,
        is_open=True,
    )
    audio_rtp = struct.pack("!BBHII", 0x80, 8, 1, 160, 1234) + b"\xd5" * 160
    source = app_audio_channel if source_name == "RTPC" else device_audio_channel
    source.response_queue.put_nowait(audio_rtp)
    receiver = MagicMock()
    receiver.running = True

    def receive(data: bytes) -> None:
        receiver.running = False
        assert data == audio_rtp

    receiver.receive_tcp_rtp.side_effect = receive

    await VideoCallSession._tcp_media_router(
        MagicMock(),
        app_audio_channel,
        device_audio_channel,
        video_channel,
        receiver,
    )

    receiver.receive_tcp_rtp.assert_called_once_with(audio_rtp)


@pytest.mark.asyncio
async def test_rtsp_play_waits_for_real_video() -> None:
    """An idle relay must not create a successfully negotiated black producer."""
    server = LocalRtspServer()
    server._running = True
    reader = asyncio.StreamReader()
    reader.feed_data(b"PLAY rtsp://127.0.0.1/intercom RTSP/1.0\r\nCSeq: 4\r\n\r\n")
    writer = MagicMock()
    writer.get_extra_info.side_effect = lambda name: (
        ("127.0.0.1", 12345) if name == "peername" else None
    )
    writer.drain = AsyncMock()

    play_task = asyncio.create_task(server._handle_client(reader, writer))
    await asyncio.sleep(0)

    writer.write.assert_not_called()

    server.mark_ready()
    reader.feed_eof()
    await asyncio.wait_for(play_task, timeout=1)

    assert b"RTSP/1.0 200 OK" in writer.write.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_rtsp_parser_preserves_pipelined_requests() -> None:
    """A PLAY received in the same TCP read as SETUP must not be discarded."""
    server = LocalRtspServer()
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"SETUP rtsp://127.0.0.1/intercom/video RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        b"PLAY rtsp://127.0.0.1/intercom RTSP/1.0\r\nCSeq: 2\r\n\r\n"
    )
    buffer = bytearray()

    first = await server._read_rtsp_request(reader, buffer)
    second = await server._read_rtsp_request(reader, buffer)

    assert first is not None and first[0] == "SETUP"
    assert second is not None and second[0] == "PLAY"
    assert second[3] == "2"
    assert buffer == bytearray()


@pytest.mark.asyncio
async def test_outbound_audio_runs_answer_sequence_before_sender() -> None:
    """An outbound view becomes a call only after audio is explicitly enabled."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._audio_answered = False
    session._inbound = False
    session._device_rtpc_req_id = 123
    session._answer_context = (
        MagicMock(),
        MagicMock(),
        "SB0000061",
        "SB100001",
        "SB000006",
        1,
        2,
    )
    session._run_answer_sequence = AsyncMock()
    session._rtp_receiver = MagicMock()

    await session.enable_two_way_audio()

    session._run_answer_sequence.assert_awaited_once_with(*session._answer_context)
    session._rtp_receiver.start_audio_sender.assert_called_once_with(123)
    assert session.audio_answered is True


def test_self_view_audio_messages_use_apartment_targeting() -> None:
    """Self-view audio uses the apartment-specific captured wire format."""
    peer = encode_self_view_audio_peer("SB0000011", "SB000001", "SB0000011", 0x12345678)
    config = encode_self_view_audio_config_ack("SB0000011", "SB000001", 0x12355678)

    assert struct.unpack_from("<H", peer, 0)[0] == 0x1840
    assert struct.unpack_from(">H", peer, 8)[0] == 0x0070
    assert b"SB0000011\0\x01\0\0\0" in peer
    assert peer.endswith(b"SB000001\0\0")
    assert struct.unpack_from("<H", config, 0)[0] == 0x1840
    assert struct.unpack_from(">H", config, 8)[0] == 0x000C
    assert config.endswith(b"SB000001\0\0")


@pytest.mark.asyncio
async def test_self_view_audio_routes_dedicated_panel_rtpc() -> None:
    """The call activation sequence consumes the audio RTPC opened by the panel."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._config = MagicMock(apt_subaddress=1)
    session._ctpp_lock = asyncio.Lock()
    session._call_counter = 0x10000000
    session._audio_tcp_task = None
    receiver = MagicMock()
    receiver.running = False
    session._rtp_receiver = receiver
    client = MagicMock()
    audio_rtpc = Channel(
        name="RTPC_AUDIO",
        channel_type=ChannelType.UAUT,
        request_id=0,
    )
    client.register_placeholder_channel.return_value = audio_rtpc
    sent: list[bytes] = []

    async def send_binary(_channel: Channel, payload: bytes) -> None:
        sent.append(payload)
        if len(sent) == 3:
            audio_rtpc.server_channel_id = 0x3456
            audio_rtpc.is_open = True
            audio_rtpc.open_event.set()

    client.send_binary = AsyncMock(side_effect=send_binary)

    await session._send_answer_sequence(
        client,
        MagicMock(),
        "SB0000011",
        "SB100001",
        "SB000001",
        0,
        0x2002,
    )
    if session._audio_tcp_task:
        await session._audio_tcp_task

    assert struct.unpack_from(">H", sent[0], 6)[0] == 0x001A
    assert struct.unpack_from(">H", sent[1], 8)[0] == 0x0070
    assert struct.unpack_from(">H", sent[2], 8)[0] == 0x000C
    assert session._device_rtpc_req_id == 0x3456
    receiver.start_audio_sender.assert_called_once_with(0x3456)
    client.release_placeholder_channel.assert_not_called()


@pytest.mark.asyncio
async def test_inbound_audio_does_not_repeat_outbound_answer_sequence() -> None:
    """Inbound signaling is already answered before its audio path is enabled."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._audio_answered = False
    session._inbound = True
    session._device_rtpc_req_id = 456
    session._answer_context = None
    session._run_answer_sequence = AsyncMock()
    session._rtp_receiver = MagicMock()

    await session.enable_two_way_audio()

    session._run_answer_sequence.assert_not_awaited()
    session._rtp_receiver.start_audio_sender.assert_called_once_with(456)
    assert session.audio_answered is True


@pytest.mark.asyncio
async def test_enabling_audio_is_idempotent() -> None:
    """Repeated UI state updates must not renegotiate the device call."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._audio_answered = True
    session._inbound = False
    session._answer_context = None
    session._run_answer_sequence = AsyncMock()
    session._rtp_receiver = MagicMock()
    session._device_rtpc_req_id = 789

    await session.enable_two_way_audio()

    session._run_answer_sequence.assert_not_awaited()
    session._rtp_receiver.start_audio_sender.assert_not_called()


@pytest.mark.asyncio
async def test_microphone_audio_uses_captured_udp_rtpc_transport() -> None:
    """PCMA microphone RTP uses ICONA-wrapped UDP with the panel RTPC ID."""
    receiver = RtpReceiver("192.0.2.1")
    receiver._running = True
    receiver._backchannel_queue = asyncio.Queue()
    receiver._backchannel_queue.put_nowait(bytes([0xD5]) * 160)
    transport = MagicMock()
    receiver._transport = transport

    def stop_after_send(_packet: bytes) -> None:
        receiver._running = False

    transport.sendto.side_effect = stop_after_send
    receiver.start_audio_sender(0x1234)
    assert receiver._audio_sender_task is not None
    await receiver._audio_sender_task

    packet = transport.sendto.call_args.args[0]
    assert len(packet) == 180
    assert packet[:8] == struct.pack("<BBHH2s", 0, 6, 172, 0x1234, b"\0\0")
    assert packet[8] >> 6 == 2
    assert packet[9] & 0x7F == 8
    assert packet[20:] == bytes([0xD5]) * 160


@pytest.mark.parametrize("request_id", [0x2001, 0x2002, 0x2003])
def test_udp_audio_accepts_all_comelit_media_request_ids(request_id: int) -> None:
    """Exterior audio may be tagged as RTPC1, RTPC2, or panel-opened RTPC."""
    receiver = RtpReceiver("192.0.2.1", media_req_id=0x2002)
    receiver.set_audio_req_id(0x2001)
    receiver._audio_sender_req_id = 0x2003
    receiver._process_audio_rtp = MagicMock()
    rtp = struct.pack("!BBHII", 0x80, 8, 1, 160, 1234) + b"\xd5" * 160
    packet = struct.pack("<BBHH2s", 0, 6, len(rtp), request_id, b"\0\0") + rtp

    receiver._on_udp_packet(packet)

    receiver._process_audio_rtp.assert_called_once_with(rtp, 8)


def test_short_udp_control_response_is_recorded_before_rtp_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The panel's 14-byte keepalive reply must not be discarded as short RTP."""
    receiver = RtpReceiver("192.0.2.1", control_req_id=0x2000)
    packet = struct.pack("<BBHH2s6s", 0, 6, 6, 0x2000, b"\0\0", b"\x01\x02\0\0\0\x80")

    with caplog.at_level(logging.INFO):
        receiver._on_udp_packet(packet, ("192.0.2.1", 64100))

    assert receiver._udp_control_response_count == 1
    assert "Panel acknowledged UDP media socket" in caplog.text


@pytest.mark.asyncio
async def test_periodic_config_ack_does_not_restart_session() -> None:
    """A recurring 0x0003/0x000E message is ACKed without resetting media."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._active = True
    session._ctpp_lock = asyncio.Lock()
    session._call_counter = 0
    session._inline_reestablish = AsyncMock()
    response = struct.pack("<HI", 0x1840, 1) + struct.pack(">HH", 0x0003, 0x000E)
    client = MagicMock()
    client.read_response = AsyncMock(return_value=response)

    async def stop_after_ack(*_args: object) -> None:
        session._active = False

    client.send_binary = AsyncMock(side_effect=stop_after_ack)

    await session._ctpp_monitor_loop(client, MagicMock(), "A1", "B1", 1, 2, 3)

    client.send_binary.assert_awaited_once()
    session._inline_reestablish.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_sub_status_renews_expired_session() -> None:
    """A true 0x0003/0x0000 lease expiry still renews the call."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._active = True
    session._ctpp_lock = asyncio.Lock()
    session._call_counter = 0
    response = struct.pack("<HI", 0x1840, 1) + struct.pack(">HH", 0x0003, 0x0000)
    client = MagicMock()
    client.read_response = AsyncMock(return_value=response)

    async def renew(*_args: object) -> int:
        session._active = False
        return 42

    session._inline_reestablish = AsyncMock(side_effect=renew)

    await session._ctpp_monitor_loop(client, MagicMock(), "A1", "B1", 1, 2, 3)

    session._inline_reestablish.assert_awaited_once()
    assert session._call_counter == 42


@pytest.mark.asyncio
async def test_closed_device_rtpc_channel_renews_media_lease() -> None:
    """The panel's actual RTPC close renews even when CTPP status is ambiguous."""
    session = VideoCallSession.__new__(VideoCallSession)
    session._active = True
    session._ctpp_lock = asyncio.Lock()
    session._call_counter = 0
    session._device_rtpc_channel = Channel(
        name="RTPC_DEVICE",
        channel_type=ChannelType.UAUT,
        request_id=0,
        server_channel_id=0x1234,
        is_open=False,
    )
    client = MagicMock()

    async def renew(*_args: object) -> int:
        session._active = False
        return 43

    session._inline_reestablish = AsyncMock(side_effect=renew)

    await session._ctpp_monitor_loop(client, MagicMock(), "A1", "B1", 1, 2, 3)

    session._inline_reestablish.assert_awaited_once()
    assert session._call_counter == 43


@pytest.mark.asyncio
async def test_missing_media_is_a_start_failure() -> None:
    """A signaled call with no RTP is not exposed as a ready black stream."""
    receiver = MagicMock()
    receiver.wait_for_first_video = AsyncMock(side_effect=TimeoutError)
    receiver.udp_media_packet_count = 0
    receiver.tcp_media_packet_count = 0

    with pytest.raises(VideoCallError):
        await VideoCallSession._require_first_video(receiver)


@pytest.mark.asyncio
async def test_play_session_routes_interleaved_microphone_audio() -> None:
    """go2rtc's third SETUP track must feed the Comelit backchannel queue."""
    server = LocalRtspServer()
    writer = MagicMock()
    client = _TcpClient(writer=writer)
    transport = "RTP/AVP/TCP;unicast;interleaved=4-5"

    response = server._parse_setup(transport, "backchannel", client, "127.0.0.1")

    assert client.backchannel_ch == 4
    assert "interleaved=4-5" in response

    payload = b"\xd5" * 160
    rtp = struct.pack("!BBHII", 0x80, 8, 1, 160, 1234) + payload
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x24\x04" + struct.pack("!H", len(rtp)) + rtp)
    reader.feed_data(b"TEARDOWN rtsp://127.0.0.1/intercom RTSP/1.0\r\nCSeq: 4\r\n\r\n")
    reader.feed_eof()
    server._running = True

    await server._wait_for_teardown(reader, client, "127.0.0.1")

    assert server.backchannel_queue.get_nowait() == payload
    assert client.backchannel_started is True
