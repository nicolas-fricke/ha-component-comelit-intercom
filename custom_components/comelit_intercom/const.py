"""Constants for the Comelit integration."""

DOMAIN = "comelit_intercom"
INTEGRATION_VERSION = "1.4.2"

# Configuration keys
CONF_HOST = "host"
CONF_TOKEN = "token"
CONF_DEVICE_ID = "device_id"
CONF_ENABLE_NOTIFICATIONS = "enable_notifications"

# Default values
DEFAULT_NAME = "Comelit Intercom"
DEFAULT_PORT = 64100

# Dashboard preview settings (minutes)
DEFAULT_STILL_PREVIEW_INTERVAL = 30.0
MIN_STILL_PREVIEW_INTERVAL = 0.0
MAX_STILL_PREVIEW_INTERVAL = 180.0
STILL_PREVIEW_INTERVAL_STEP = 0.5

# Update interval (in seconds)
UPDATE_INTERVAL = 300  # 5 minutes
