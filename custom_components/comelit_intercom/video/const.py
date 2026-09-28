"""Constants used only by the isolated Comelit video transport."""

DOMAIN = "comelit_intercom"

_verbose_logging: bool = False


def is_verbose_logging() -> bool:
    """Return True when verbose logging is enabled via the integration options."""
    return _verbose_logging


def set_verbose_logging(enabled: bool) -> None:
    """Enable detailed video protocol logging."""
    global _verbose_logging
    _verbose_logging = enabled


# Video config sent to the device via encode_video_config().
VIDEO_WIDTH = 800
VIDEO_HEIGHT = 480
VIDEO_FPS = 16
