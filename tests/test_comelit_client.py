"""Protocol tests for the Comelit ICONA bridge client."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

MODULE_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "comelit_intercom"
)
PACKAGE_ROOT = MODULE_DIR.parent
TOP_LEVEL_PACKAGE = "custom_components"
PACKAGE_NAME = "custom_components.comelit_intercom"


def _load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


if TOP_LEVEL_PACKAGE not in sys.modules:
    package = types.ModuleType(TOP_LEVEL_PACKAGE)
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules[TOP_LEVEL_PACKAGE] = package

if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(MODULE_DIR)]
    sys.modules[PACKAGE_NAME] = package

_load_module(f"{PACKAGE_NAME}.control_discovery", MODULE_DIR / "control_discovery.py")
COMELIT_CLIENT = _load_module(
    f"{PACKAGE_NAME}.comelit_client", MODULE_DIR / "comelit_client.py"
)

Channel = COMELIT_CLIENT.Channel
ChannelData = COMELIT_CLIENT.ChannelData
IconaBridgeClient = COMELIT_CLIENT.IconaBridgeClient


class OpenActuatorTests(unittest.IsolatedAsyncioTestCase):
    """Verify the actuator-specific packet flow."""

    async def test_open_actuator_uses_distinct_packet_sequence(self) -> None:
        client = IconaBridgeClient("192.0.2.1")
        client.open_channels[Channel.CTPP] = ChannelData(
            channel=Channel.CTPP,
            id=1234,
            sequence=2,
        )

        written_packets: list[bytes] = []

        async def fake_write_packet(packet: bytes) -> None:
            written_packets.append(packet)

        async def fake_read_response() -> dict[str, str]:
            return {"type": "binary_data"}

        client._write_packet = fake_write_packet  # type: ignore[method-assign]
        client._read_response = fake_read_response  # type: ignore[method-assign]

        vip_config = {"apt-address": "SB000001", "apt-subaddress": ""}
        actuator = {
            "control-type": "actuator",
            "name": "Secondary relay",
            "apt-address": "SBIO0255",
            "module-index": 255,
            "output-index": 1,
        }

        await client.open_actuator(vip_config, actuator)

        self.assertEqual(len(written_packets), 3)
        self.assertEqual(written_packets[0][8:12], bytes([0xC0, 0x18, 0x45, 0xBE]))
        self.assertEqual(written_packets[1][8:12], bytes([0x00, 0x18, 0x45, 0xBE]))
        self.assertEqual(written_packets[2][8:12], bytes([0x20, 0x18, 0x45, 0xBE]))

        self.assertIn(b"SB0000011\x00", written_packets[0])
        self.assertIn(b"SBIO0255\x00", written_packets[0])


class FakeWriter:
    """Minimal stream writer for packet logging tests."""

    def __init__(self) -> None:
        self.written_packets: list[bytes] = []

    def write(self, packet: bytes) -> None:
        self.written_packets.append(packet)

    async def drain(self) -> None:
        return None


class PacketLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Verify sensitive packets are never logged raw."""

    async def test_write_packet_redacts_auth_payload(self) -> None:
        client = IconaBridgeClient("192.0.2.1")
        writer = FakeWriter()
        client.writer = writer  # type: ignore[assignment]

        token = "0123456789abcdef0123456789abcdef"
        packet = client._create_json_packet(
            1234,
            {
                "message": "access",
                "user-token": token,
                "message-type": "request",
                "message-id": 2,
            },
        )

        with self.assertLogs(COMELIT_CLIENT.__name__, level="DEBUG") as logs:
            await client._write_packet(packet)

        self.assertEqual(writer.written_packets, [packet])

        log_output = "\n".join(logs.output)
        self.assertIn("<redacted sensitive payload>", log_output)
        self.assertNotIn(token, log_output)
        self.assertNotIn(token.encode("utf-8").hex(" "), log_output)
        self.assertNotIn(packet.hex(" "), log_output)


if __name__ == "__main__":
    unittest.main()
