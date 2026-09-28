"""Custom exceptions for the Comelit local library."""

from homeassistant.exceptions import HomeAssistantError


class ComelitError(HomeAssistantError):
    """Base exception for all Comelit errors."""


class ConnectionComelitError(ComelitError):
    """Failed to connect to the device."""


class AuthenticationError(ComelitError):
    """Authentication failed (invalid token or credentials)."""


class ProtocolError(ComelitError):
    """Unexpected data in the wire protocol."""


class TokenExtractionError(ComelitError):
    """Failed to extract token from device backup."""


class DoorOpenError(ComelitError):
    """Failed to open a door."""


class VideoCallError(ComelitError):
    """Failed to establish video call."""
