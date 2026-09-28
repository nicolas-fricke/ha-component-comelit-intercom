"""Tests for Comelit backup token extraction."""

from __future__ import annotations

import gzip
import importlib.util
import io
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "aiohttp" not in sys.modules:
    try:
        __import__("aiohttp")
    except ModuleNotFoundError:
        aiohttp_stub = types.ModuleType("aiohttp")

        class ClientTimeout:
            """Test stub for aiohttp.ClientTimeout."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        class ClientSession:
            """Test stub for aiohttp.ClientSession."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        aiohttp_stub.ClientTimeout = ClientTimeout
        aiohttp_stub.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp_stub


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "comelit_intercom"
    / "token_extractor.py"
)
SPEC = importlib.util.spec_from_file_location("token_extractor", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
TOKEN_EXTRACTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOKEN_EXTRACTOR)

extract_token_from_backup = TOKEN_EXTRACTOR.extract_token_from_backup
safe_extract_tar = TOKEN_EXTRACTOR._safe_extract_tar
validate_tar_member_path = TOKEN_EXTRACTOR._validate_tar_member_path


def _make_tar_gz(members: list[tuple[str, bytes]]) -> bytes:
    """Create an in-memory tar.gz archive."""
    backup = io.BytesIO()
    with tarfile.open(fileobj=backup, mode="w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return backup.getvalue()


class SafeExtractTarTests(unittest.TestCase):
    """Verify backup archives cannot write outside the extraction directory."""

    def test_rejects_unsafe_member_paths(self) -> None:
        unsafe_paths = [
            "../escape.txt",
            "etc/../escape.txt",
            "/tmp/escape.txt",
            r"..\escape.txt",
            r"C:\escape.txt",
            r"\server\share\escape.txt",
        ]

        for member_path in unsafe_paths:
            with self.subTest(member_path=member_path):
                with self.assertRaises(tarfile.TarError):
                    validate_tar_member_path(member_path)

    def test_rejects_traversal_before_extracting_any_members(self) -> None:
        backup = _make_tar_gz(
            [
                ("etc/comelit/users.cfg", b'9:4:"0123456789abcdef0123456789abcdef"'),
                ("../escape.txt", b"outside"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "extract"
            with tarfile.open(fileobj=io.BytesIO(backup), mode="r:gz") as tar:
                with self.assertRaises(tarfile.TarError):
                    safe_extract_tar(tar, destination)

            self.assertFalse((destination / "etc/comelit/users.cfg").exists())
            self.assertFalse((Path(tmpdir) / "escape.txt").exists())

    def test_rejects_oversized_archive_member(self) -> None:
        backup = _make_tar_gz([("etc/comelit/users.cfg", b"123456789")])

        with (
            patch.object(TOKEN_EXTRACTOR, "MAX_MEMBER_BYTES", 8),
            tempfile.TemporaryDirectory() as tmpdir,
            tarfile.open(fileobj=io.BytesIO(backup), mode="r:gz") as tar,
            self.assertRaises(tarfile.TarError),
        ):
            safe_extract_tar(tar, Path(tmpdir) / "extract")


class ExtractTokenFromBackupTests(unittest.IsolatedAsyncioTestCase):
    """Verify token extraction from safe backup contents."""

    async def test_extracts_token_from_backup(self) -> None:
        token = "0123456789abcdef0123456789abcdef"
        backup = _make_tar_gz([("etc/comelit/users.cfg", f'9:4:"{token}"'.encode())])

        self.assertEqual(await extract_token_from_backup(backup), token)

    async def test_rejects_gzip_expansion_beyond_users_config_limit(self) -> None:
        backup = _make_tar_gz([("etc/comelit/users.cfg", gzip.compress(b"x" * 65))])

        with patch.object(TOKEN_EXTRACTOR, "MAX_USERS_CONFIG_BYTES", 64):
            self.assertIsNone(await extract_token_from_backup(backup))


if __name__ == "__main__":
    unittest.main()
