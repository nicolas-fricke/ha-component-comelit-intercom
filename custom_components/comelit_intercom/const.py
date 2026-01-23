"""Constants for the Comelit integration."""

DOMAIN = "comelit_intercom"

# Configuration keys
CONF_HOST = "host"
CONF_TOKEN = "token"
CONF_CALL_TARGET = "call_target"
CONF_DOOR_COUNT = "door_count"
CONF_RESET_INTERVAL = "reset_interval"
CONF_DOOR_DELAY = "door_delay"

# Default values
DEFAULT_NAME = "Comelit Intercom"
DEFAULT_PORT = 64100
DEFAULT_DOOR_COUNT = 1
# In peer-based multi-door setups, cam2 peer association is short-lived.
# We use a shorter reuse window (safety margin) for repeated Door 2 requests.
DEFAULT_RESET_INTERVAL = 15.0
DEFAULT_DOOR_DELAY = 0.0

# Update interval (in seconds)
UPDATE_INTERVAL = 300  # 5 minutes
