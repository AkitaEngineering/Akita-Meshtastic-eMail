# akita_email/config.py
import logging
import sys

# --- General Configuration ---
APP_NAME = "AkitaEmail"
VERSION = "0.2.0" # Indicate refactored version

# --- Meshtastic Plugin Configuration ---
PLUGIN_DATABASE_FILE = "akita_plugin_store.db"
PLUGIN_LOG_FILE = "akita_plugin.log"
PLUGIN_LOG_LEVEL = logging.INFO
# Log Format - Consistent across modules
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# --- Meshtastic Network Configuration ---
PRIMARY_CHANNEL_INDEX = 0 # Explicitly use Channel 0 (Primary Channel)
MESSAGE_HOP_LIMIT = 7     # Max hops for a message (Meshtastic default is often 3 or 7)
MESSAGE_RETRY_INTERVAL = 60 * 5 # Seconds between retries for un-ACKed messages (5 minutes)
MESSAGE_EXPIRY_TIME = 3600 * 6 # Seconds before giving up on a message (6 hours)

# --- Companion Device Communication (via Serial) ---
# These ports need to be configured correctly for your setup.
# If plugin and companion run on the same host, consider using virtual serial ports
# (e.g., using 'socat' on Linux/macOS) or modify code for other IPC (not implemented).

# Serial port the *plugin* uses to talk TO the companion CLI process
# Example Linux: "/dev/ttyUSB0", Example macOS: "/dev/tty.usbserial-XYZ"
# Example Windows: "COM3"
COMPANION_SERIAL_PORT = "/dev/ttyUSB0" # CHANGE THIS TO YOUR PLUGIN -> COMPANION PORT
COMPANION_SERIAL_BAUD = 115200

# --- Companion CLI Configuration ---
# Serial port the *companion CLI* uses to talk TO the plugin process
# Ensure this is DIFFERENT from COMPANION_SERIAL_PORT if using two serial adapters on one host.
COMPANION_PLUGIN_PORT = "/dev/ttyACM0" # CHANGE THIS TO YOUR COMPANION -> PLUGIN PORT
COMPANION_PLUGIN_BAUD = 115200
COMPANION_LOG_FILE = "akita_companion.log"
COMPANION_LOG_LEVEL = logging.INFO

# --- Protocol Definitions ---
# Using short keys for slightly more compact JSON payloads over LoRa
MSG_KEY_TYPE = 't'       # Message Type (e.g., 'eml', 'ack')
MSG_KEY_ID = 'i'         # Unique Message ID (UUID string)
MSG_KEY_TO = 'to'        # Destination Node ID (Meshtastic NodeNum)
MSG_KEY_FROM = 'fm'      # Source Node ID (Meshtastic NodeNum)
MSG_KEY_SUBJECT = 's'    # Email Subject (String)
MSG_KEY_BODY = 'b'       # Email Body (String)
MSG_KEY_TIMESTAMP = 'ts' # Original Send Timestamp (Unix epoch, integer)
MSG_KEY_HOPS = 'hp'      # Current Hop Count (Integer)
MSG_KEY_ACK_FOR = 'af'   # ID of the message being ACKed (String)
# MSG_KEY_ERROR = 'err'  # Optional: Error message content

# Message Types
MSG_TYPE_EMAIL = 'eml'
MSG_TYPE_ACK = 'ack'
# MSG_TYPE_ALIAS = 'als' # Example for future alias messages

# Command types for Companion <-> Plugin communication (Serial JSON)
# These run over serial, so verbosity is less critical than LoRa protocol
CMD_SEND_EMAIL = "send_email"
CMD_READ_EMAILS = "read_emails"
CMD_SET_ALIAS = "set_alias"
CMD_GET_STATUS = "get_status" # Request status of an outgoing message
CMD_PING_PLUGIN = "ping"      # Check if plugin is responsive

# Response/Notification types for Plugin -> Companion communication
RESP_INBOX_LIST = "inbox_list"
RESP_STATUS_UPDATE = "status_update" # For ACK/fail/progress notifications
RESP_NEW_EMAIL_NOTIFY = "new_email_notify"
RESP_PONG = "pong" # Response to ping
RESP_ERROR = "error_response" # General error reporting to companion

# --- Utility Functions ---
def setup_logger(name: str, level: int, log_file: str, console: bool = True):
    """Configures and returns a logger instance."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)

    # File Handler
    try:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as e:
        print(f"Warning: Could not set up file logging to {log_file}: {e}", file=sys.stderr)


    # Console Handler
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    # Prevent duplicate logging if called multiple times
    logger.propagate = False
    return logger

