"""
Token extractor for Comelit devices.

Extracts user authentication tokens by creating and downloading
device configuration backups.
"""

import asyncio
import gzip
import logging
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import aiohttp

_LOGGER = logging.getLogger(__name__)

MAX_BACKUP_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1_000
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_USERS_CONFIG_BYTES = 4 * 1024 * 1024


def _validate_tar_member_path(member_name: str) -> None:
    """Validate that a tar member path cannot escape the extraction directory."""
    posix_path = PurePosixPath(member_name)
    windows_path = PureWindowsPath(member_name)

    if (
        not member_name
        or member_name.startswith(("/", "\\"))
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise tarfile.TarError(f"Refusing unsafe tar member path: {member_name!r}")


def _validated_tar_target(destination: Path, member: tarfile.TarInfo) -> Path:
    """Return the safe target path for a tar member or raise TarError."""
    _validate_tar_member_path(member.name)

    if member.issym() or member.islnk():
        raise tarfile.TarError(f"Refusing tar link member: {member.name!r}")

    if not member.isfile() and not member.isdir():
        raise tarfile.TarError(f"Refusing unsupported tar member: {member.name!r}")

    target = (destination / member.name).resolve(strict=False)
    if target != destination and destination not in target.parents:
        raise tarfile.TarError(
            f"Refusing tar member outside destination: {member.name!r}"
        )

    return target


def _safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    """Safely extract regular files and directories from a tar archive."""
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    members = tar.getmembers()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise tarfile.TarError("Backup contains too many archive members")
    total_size = 0
    for member in members:
        if member.size < 0 or member.size > MAX_MEMBER_BYTES:
            raise tarfile.TarError(f"Backup member is too large: {member.name!r}")
        if member.isfile():
            total_size += member.size
            if total_size > MAX_EXTRACTED_BYTES:
                raise tarfile.TarError("Backup expands beyond the safe size limit")
    validated_members = [
        (member, _validated_tar_target(resolved_destination, member))
        for member in members
    ]

    for member, target in validated_members:
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        source = tar.extractfile(member)
        if source is None:
            raise tarfile.TarError(f"Unable to read tar member: {member.name!r}")

        target.parent.mkdir(parents=True, exist_ok=True)
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


async def _read_backup_response(resp: aiohttp.ClientResponse) -> bytes | None:
    """Read a backup response without allowing an unbounded download."""
    if (
        resp.content_length is not None
        and resp.content_length > MAX_BACKUP_DOWNLOAD_BYTES
    ):
        return None
    backup = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        if len(backup) + len(chunk) > MAX_BACKUP_DOWNLOAD_BYTES:
            return None
        backup.extend(chunk)
    return bytes(backup)


def _read_users_config(file_path: Path) -> str:
    """Decode users.cfg while limiting plain and gzip-expanded data."""
    with file_path.open("rb") as file:
        magic = file.read(2)
    if magic == b"\x1f\x8b":
        with gzip.open(file_path, "rb") as file:
            content = file.read(MAX_USERS_CONFIG_BYTES + 1)
    else:
        with file_path.open("rb") as file:
            content = file.read(MAX_USERS_CONFIG_BYTES + 1)
    if len(content) > MAX_USERS_CONFIG_BYTES:
        raise OSError("users.cfg expands beyond the safe size limit")
    return content.decode("utf-8", errors="ignore")


async def extract_token(
    host: str, password: str = "comelit", port: int = 8080
) -> str | None:
    """
    Extract authentication token from Comelit device.

    Creates a new backup on the device, downloads it, and extracts
    the user authentication token from the configuration files.
    """
    base_url = f"http://{host}:{port}"

    # The Comelit web interface uses IP-based sessions instead of cookies.
    # Once we authenticate from an IP address, all subsequent requests from
    # that IP are authorized for a period of time. This is why we don't need
    # to handle cookies or session tokens.
    timeout = aiohttp.ClientTimeout(total=60, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            _LOGGER.info("Starting token extraction")

            # Step 1: Login to establish IP-based session
            # The 'l-pwd' field is the login password field name in the web interface
            login_data = {"l-pwd": password}
            login_headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{base_url}/",  # Required by the device for security
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }

            async with session.post(
                f"{base_url}/do-login.html", data=login_data, headers=login_headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Login failed with status: {resp.status}")
                    return None

                login_content = await resp.text()

                if "Access granted" not in login_content:
                    _LOGGER.error("Login failed - check password")
                    return None

                _LOGGER.info("Login successful")

            # Step 2: Create a new backup
            # We need to create a fresh backup to ensure we get the current token
            # (in case it was changed since the last backup)
            _LOGGER.info("Creating new backup...")
            headers = {
                "X-Requested-With": "XMLHttpRequest",  # Required to trigger AJAX response
                "Referer": f"{base_url}/config-backup.html",
            }

            async with session.post(
                f"{base_url}/create-backup.html", headers=headers
            ) as resp:
                create_response = await resp.text()
                if "Backup successfully created" not in create_response:
                    _LOGGER.error(f"Backup creation failed: {create_response}")
                    return None

                _LOGGER.info("Backup created successfully")

            # Step 3: Wait for backup creation to complete
            # The device needs a moment to actually create the backup file
            await asyncio.sleep(2)

            async with session.get(f"{base_url}/config-backup.html") as resp:
                if resp.status != 200:
                    _LOGGER.error("Failed to get backup list")
                    return None

                backup_page = await resp.text()

            # Step 4: Find the latest backup
            # Backup filenames are timestamps like "1234567890.tar.gz"
            # We want the highest number (most recent timestamp)
            backup_files = re.findall(r"([0-9]+\.tar\.gz)", backup_page)
            if not backup_files:
                _LOGGER.error("No backup files found")
                return None

            # Sort numerically to get the most recent backup
            backup_files.sort()
            latest_backup = backup_files[-1]
            _LOGGER.info(f"Using latest backup: {latest_backup}")

            # Step 5: Download the backup
            async with session.get(f"{base_url}/{latest_backup}") as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Failed to download backup: {resp.status}")
                    return None

                backup_data = await _read_backup_response(resp)
                if backup_data is None:
                    _LOGGER.error("Backup exceeds the safe download size limit")
                    return None
                _LOGGER.info(f"Downloaded {len(backup_data)} bytes")

            # Step 6: Extract token from backup
            return await extract_token_from_backup(backup_data)

        except Exception as e:
            _LOGGER.error(f"Error in token extraction: {e}")
            import traceback

            _LOGGER.error(traceback.format_exc())
            return None


async def extract_token_from_backup(backup_data: bytes) -> str | None:
    """Extract token from backup archive."""
    if len(backup_data) > MAX_BACKUP_DOWNLOAD_BYTES:
        _LOGGER.error("Backup exceeds the safe archive size limit")
        return None
    try:
        loop = asyncio.get_event_loop()

        def _extract() -> str | None:
            with tempfile.TemporaryDirectory() as tmpdir:
                backup_path = Path(tmpdir) / "backup.tar.gz"
                extract_dir = Path(tmpdir) / "backup"
                backup_path.write_bytes(backup_data)

                # Extract tar.gz
                try:
                    with tarfile.open(backup_path, "r:gz") as tar:
                        _safe_extract_tar(tar, extract_dir)
                except (OSError, tarfile.TarError) as e:
                    _LOGGER.error(f"Failed to extract backup: {e}")
                    return None

                # Find and process users.cfg which contains user configuration
                # This file is typically located in etc/comelit/ within the backup
                for root, _dirs, files in os.walk(extract_dir):
                    for file in files:
                        if file == "users.cfg":
                            file_path = Path(root) / file
                            _LOGGER.debug(f"Found {file} at: {file_path}")

                            # Some firmware gzip users.cfg without adding .gz.
                            try:
                                content = _read_users_config(file_path)
                            except OSError as err:
                                _LOGGER.error("Unable to read users.cfg: %s", err)
                                return None

                            _LOGGER.debug(f"users.cfg size: {len(content)} bytes")

                            # The users.cfg file uses a serialized format where:
                            # - Fields are numbered (1:, 2:, etc.)
                            # - Field 9 contains the authentication token
                            # - Format is: 9:4:"TOKEN_HERE" where 4 is the field type (string)
                            # - Tokens are always 32 character hexadecimal strings
                            matches: list[str] = re.findall(
                                r'9:4:"([a-f0-9]{32})"', content, re.IGNORECASE
                            )
                            if matches:
                                _LOGGER.info(f"Found {len(matches)} tokens in {file}")
                                # Return the first valid token
                                # Skip null tokens (all zeros) which indicate no user configured
                                for token in matches:
                                    if token != "00000000000000000000000000000000":
                                        _LOGGER.info(f"Extracted token: {token[:8]}...")
                                        return token

                            # Fallback: Some older firmware versions might store tokens differently
                            # Try to find any 32-character hex string as a last resort
                            hex_matches: list[str] = re.findall(
                                r"\b([a-f0-9]{32})\b", content, re.IGNORECASE
                            )
                            if hex_matches:
                                _LOGGER.info(
                                    f"Found {len(hex_matches)} potential tokens via hex pattern"
                                )
                                for match in hex_matches:
                                    if match != "00000000000000000000000000000000":
                                        return match

                _LOGGER.error("No token found in backup")
                return None

        # Run the file extraction in a thread pool to avoid blocking the event loop
        # File I/O operations can be slow and would block async operations
        return await loop.run_in_executor(None, _extract)

    except Exception as e:
        _LOGGER.error(f"Error extracting token: {e}")
        return None
