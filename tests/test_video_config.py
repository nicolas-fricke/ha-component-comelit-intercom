"""Tests for adapting cached VIP data to the video transport."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "comelit_intercom"
)
VIDEO_DIR = COMPONENT_DIR / "video"
TOP_LEVEL_PACKAGE = "custom_components"
COMPONENT_PACKAGE = "custom_components.comelit_intercom"
VIDEO_PACKAGE = f"{COMPONENT_PACKAGE}.video"


def _package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package


def _load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_package(TOP_LEVEL_PACKAGE, COMPONENT_DIR.parent)
_package(COMPONENT_PACKAGE, COMPONENT_DIR)
_package(VIDEO_PACKAGE, VIDEO_DIR)
_load_module(f"{VIDEO_PACKAGE}.models", VIDEO_DIR / "models.py")
VIDEO_CONFIG = _load_module(f"{VIDEO_PACKAGE}.config", VIDEO_DIR / "config.py")

device_config_from_vip = VIDEO_CONFIG.device_config_from_vip


class DeviceConfigFromVipTests(unittest.TestCase):
    """Verify the video session receives normalized address information."""

    def test_parses_apartment_and_entrance_addresses(self) -> None:
        config = device_config_from_vip(
            {
                "apt-address": "SB000006",
                "apt-subaddress": "1",
                "user-parameters": {
                    "entrance-address-book": [
                        {"apt-address": "SB100001", "name": "Entrance"}
                    ]
                },
            }
        )

        self.assertEqual(config.apt_address, "SB000006")
        self.assertEqual(config.apt_subaddress, 1)
        self.assertEqual(config.caller_address, "SB100001")

    def test_accepts_singleton_entrance_address_book(self) -> None:
        config = device_config_from_vip(
            {
                "apt-address": "SB000006",
                "apt-subaddress": "",
                "user-parameters": {
                    "entrance-address-book": {"apt-address": "SB100001"}
                },
            }
        )

        self.assertEqual(config.apt_subaddress, 0)
        self.assertEqual(config.caller_address, "SB100001")

    def test_handles_missing_or_invalid_optional_data(self) -> None:
        config = device_config_from_vip(
            {
                "apt-address": "SB000006",
                "apt-subaddress": "not-a-number",
                "user-parameters": None,
            }
        )

        self.assertEqual(config.apt_subaddress, 0)
        self.assertEqual(config.caller_address, "")


if __name__ == "__main__":
    unittest.main()
