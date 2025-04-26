# akita_email/protocol.py
import json
import logging
import time
import uuid
from typing import Dict, Any, Optional, Union, cast

from . import config
from .models import Email, NodeId, STATUS_RECEIVED # Import STATUS_RECEIVED
from .exceptions import ProtocolError

# Get a logger specific to this module
logger = config.setup_logger(__name__, config.PLUGIN_LOG_LEVEL, config.PLUGIN_LOG_FILE, console=False)

# --- LoRa Message Encoding/Decoding (JSON for now) ---

def _encode_base(msg_type: str, msg_id: str) -> Dict[str, Any]:
    """Creates the base dictionary structure for an Akita LoRa message."""
    if not msg_id:
        msg_id = str(uuid.uuid4()) # Ensure message always has an ID
        logger.warning(f"Generated missing message ID for type {msg_type}: {msg_id}")
    return {
        config.MSG_KEY_TYPE: msg_type,
        config.MSG_KEY_ID: msg_id,
    }

def encode_email_to_lora(email: Email) -> str:
    """
    Encodes an Email object into a compact JSON string for LoRa transmission.

    Args:
        email: The Email object to encode.

    Returns:
        A JSON string representation of the email.

    Raises:
        ProtocolError: If encoding fails.
    """
    payload = _encode_base(config.MSG_TYPE_EMAIL, email.message_id)
    payload.update({
        config.MSG_KEY_TO: email.to_node_id,
        config.MSG_KEY_FROM: email.from_node_id,
        config.MSG_KEY_SUBJECT: email.subject, # Include subject
        config.MSG_KEY_BODY: email.body,
        config.MSG_KEY_TIMESTAMP: int(email.timestamp), # Use integer timestamp for compactness
        config.MSG_KEY_HOPS: email.hops,
    })
    try:
        # Use separators=(',', ':') for the most compact JSON representation
        return json.dumps(payload, separators=(',', ':'))
    except TypeError as e:
        logger.error(f"Failed to JSON encode email {email.message_id}: {e}", exc_info=True)
        raise ProtocolError(f"Failed to encode email: {e}") from e

def encode_ack_to_lora(ack_for_id: str, to_node_id: NodeId, from_node_id: NodeId) -> str:
    """
    Encodes an ACK message into a compact JSON string for LoRa transmission.

    Args:
        ack_for_id: The message_id of the email being acknowledged.
        to_node_id: The NodeId the ACK is being sent to (original sender).
        from_node_id: The NodeId sending the ACK (original recipient).

    Returns:
        A JSON string representation of the ACK.

    Raises:
        ProtocolError: If encoding fails.
    """
    # Create a unique ID for the ACK itself, derived from the original ID
    ack_msg_id = f"ack_{ack_for_id}_{uuid.uuid4().hex[:6]}"
    payload = _encode_base(config.MSG_TYPE_ACK, ack_msg_id)
    payload.update({
        config.MSG_KEY_ACK_FOR: ack_for_id,
        config.MSG_KEY_TO: to_node_id,       # Destination of the ACK
        config.MSG_KEY_FROM: from_node_id,   # Source of the ACK
        config.MSG_KEY_TIMESTAMP: int(time.time()), # Timestamp of ACK generation
        config.MSG_KEY_HOPS: 0,              # ACK starts with 0 hops
    })
    try:
        return json.dumps(payload, separators=(',', ':'))
    except TypeError as e:
        logger.error(f"Failed to JSON encode ACK for {ack_for_id}: {e}", exc_info=True)
        raise ProtocolError(f"Failed to encode ACK: {e}") from e

def decode_lora_packet(packet: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Decodes a received Meshtastic packet potentially containing an Akita message.
    Validates the structure and required fields based on message type.

    Args:
        packet: The raw packet dictionary received from meshtastic-python.

    Returns:
        A dictionary containing the decoded Akita message data and metadata,
        or None if the packet is not a valid Akita message.
        The returned dict includes a '_packet_info_' key with Meshtastic metadata.
    """
    if not packet:
        return None

    # Check if it's a decoded text message packet
    decoded_part = packet.get("decoded")
    if not isinstance(decoded_part, dict):
        return None # Not a decoded packet

    # Check portnum - TEXT_MESSAGE_APP is the default for sendText
    # Other relevant portnums might exist depending on Meshtastic usage.
    portnum = decoded_part.get("portnum")
    if portnum != 'TEXT_MESSAGE_APP':
        # logger.debug(f"Ignoring packet on portnum {portnum}")
        return None

    payload_str = decoded_part.get("text")
    if not payload_str:
        # logger.debug("Ignoring packet with empty text payload")
        return None # Empty text payload

    try:
        # Attempt to parse the JSON payload
        data = json.loads(payload_str)
        if not isinstance(data, dict):
            logger.warning(f"Received non-dict JSON payload: {payload_str}")
            return None

        # --- Basic Akita Message Validation ---
        msg_type = data.get(config.MSG_KEY_TYPE)
        msg_id = data.get(config.MSG_KEY_ID)

        if not msg_type or not msg_id:
            logger.warning(f"Received Akita message missing type or ID: {payload_str}")
            return None

        # Store raw packet metadata alongside decoded data for context
        packet_info = {
            'from_node_id': packet.get('from'), # NodeId of the immediate sender
            'to_node_id': packet.get('to'),     # NodeId of the immediate recipient (^all, ^local, specific)
            'channel': packet.get('channel'),   # Channel index
            'hop_limit': packet.get('hopLimit'),
            'rx_rssi': packet.get('rxRssi'),
            'rx_snr': packet.get('rxSnr'),
            'rx_time': packet.get('rxTime'),
        }
        data['_packet_info_'] = packet_info # Add metadata under a specific key

        # Ensure the 'from' node ID is valid
        if not isinstance(packet_info['from_node_id'], int):
             logger.warning(f"Received message with invalid 'from' node ID: {packet_info['from_node_id']}")
             return None # Cannot process without a valid sender node ID


        # --- Type-Specific Validation ---
        if msg_type == config.MSG_TYPE_EMAIL:
            required_keys = [config.MSG_KEY_TO, config.MSG_KEY_FROM, config.MSG_KEY_BODY,
                             config.MSG_KEY_TIMESTAMP, config.MSG_KEY_HOPS]
            if not all(key in data for key in required_keys):
                logger.warning(f"Received incomplete EMAIL message (ID: {msg_id}): Missing keys in {data}")
                return None
            # Validate numeric types
            for key in [config.MSG_KEY_TO, config.MSG_KEY_FROM, config.MSG_KEY_TIMESTAMP, config.MSG_KEY_HOPS]:
                 if not isinstance(data.get(key), int):
                      logger.warning(f"Invalid type for key '{key}' in EMAIL (ID: {msg_id}): {data}")
                      return None
            # Ensure subject is present (even if empty string), default if missing
            data.setdefault(config.MSG_KEY_SUBJECT, "")
            if not isinstance(data.get(config.MSG_KEY_SUBJECT), str):
                 logger.warning(f"Invalid type for subject key in EMAIL (ID: {msg_id}): {data}")
                 data[config.MSG_KEY_SUBJECT] = "" # Force to empty string


        elif msg_type == config.MSG_TYPE_ACK:
            required_keys = [config.MSG_KEY_ACK_FOR, config.MSG_KEY_TO, config.MSG_KEY_FROM,
                             config.MSG_KEY_TIMESTAMP, config.MSG_KEY_HOPS]
            if not all(key in data for key in required_keys):
                logger.warning(f"Received incomplete ACK message (ID: {msg_id}): Missing keys in {data}")
                return None
            # Validate numeric types
            for key in [config.MSG_KEY_TO, config.MSG_KEY_FROM, config.MSG_KEY_TIMESTAMP, config.MSG_KEY_HOPS]:
                 if not isinstance(data.get(key), int):
                      logger.warning(f"Invalid type for key '{key}' in ACK (ID: {msg_id}): {data}")
                      return None
            # Validate ACK_FOR is a string
            if not isinstance(data.get(config.MSG_KEY_ACK_FOR), str):
                 logger.warning(f"Invalid type for ack_for key in ACK (ID: {msg_id}): {data}")
                 return None

        # Add validation for other message types (like ALIAS) here if implemented

        else:
            logger.warning(f"Received unknown Akita message type '{msg_type}' (ID: {msg_id}): {data}")
            return None # Unknown type

        # If all checks pass, return the validated data dictionary
        logger.debug(f"Successfully decoded packet type '{msg_type}' (ID: {msg_id}) from {packet_info['from_node_id']:#0x}")
        return data

    except json.JSONDecodeError:
        # Ignore messages that are clearly not JSON, might be other mesh traffic
        # logger.debug(f"Received non-JSON text message: {payload_str}")
        return None
    except KeyError as e:
        # This shouldn't happen with .get() usage, but catch just in case
        logger.warning(f"Missing expected key {e} during decoding: {payload_str}", exc_info=True)
        return None
    except Exception as e:
        # Catch any other unexpected errors during decoding
        logger.error(f"Unexpected error decoding packet: {payload_str} - Error: {e}", exc_info=True)
        return None


# --- Companion <-> Plugin Communication Protocol (Serial JSON Lines) ---

def encode_companion_command(command_type: str, **kwargs) -> str:
    """
    Encodes a command dictionary into a JSON string line for sending
    FROM the Companion CLI TO the Plugin over serial.

    Args:
        command_type: The command identifier (e.g., config.CMD_SEND_EMAIL).
        **kwargs: Parameters for the command.

    Returns:
        A newline-terminated JSON string.

    Raises:
        ProtocolError: If encoding fails.
    """
    payload = {"cmd": command_type, "params": kwargs}
    try:
        # Add newline for line-based serial reading
        return json.dumps(payload) + "\n"
    except TypeError as e:
        raise ProtocolError(f"Failed to encode companion command {command_type}: {e}") from e

def encode_companion_response(response_type: str, **kwargs) -> str:
    """
    Encodes a response or notification dictionary into a JSON string line
    for sending FROM the Plugin TO the Companion CLI over serial.

    Args:
        response_type: The response identifier (e.g., config.RESP_INBOX_LIST).
        **kwargs: Data associated with the response.

    Returns:
        A newline-terminated JSON string.

    Raises:
        ProtocolError: If encoding fails.
    """
    payload = {"resp": response_type, "data": kwargs}
    try:
        # Add newline for line-based serial reading
        return json.dumps(payload) + "\n"
    except TypeError as e:
        raise ProtocolError(f"Failed to encode companion response {response_type}: {e}") from e

def decode_companion_message(line: str) -> Optional[Dict[str, Any]]:
    """
    Decodes a single line of JSON received over serial (either a command or response).

    Args:
        line: The raw string line received from the serial port.

    Returns:
        A dictionary representing the decoded JSON message, or None if decoding fails.
    """
    line = line.strip()
    if not line:
        return None # Ignore empty lines
    try:
        data = json.loads(line)
        if not isinstance(data, dict):
             logger.warning(f"Received non-dict JSON over serial: {line}")
             return None
        # Basic validation: check for 'cmd'/'params' or 'resp'/'data' structure
        is_command = 'cmd' in data and 'params' in data
        is_response = 'resp' in data and 'data' in data
        if not is_command and not is_response:
             logger.warning(f"Received malformed JSON over serial (missing keys): {line}")
             return None
        return data
    except json.JSONDecodeError:
        logger.warning(f"Received non-JSON message over serial: {line}")
        return None
    except Exception as e:
         logger.error(f"Unexpected error decoding serial message: {line} - Error: {e}", exc_info=True)
         return None

# --- Helper to convert Email model to dict for companion responses ---
def email_to_dict(email: Email) -> Dict[str, Any]:
    """Converts an Email object to a dictionary suitable for companion responses."""
    return {
        'message_id': email.message_id,
        'to_node_id': email.to_node_id,
        'from_node_id': email.from_node_id,
        'subject': email.subject,
        'body': email.body,
        'timestamp': email.timestamp,
        'hops': email.hops,
        'status': email.status,
        'last_attempt_time': email.last_attempt_time,
        'retry_count': email.retry_count,
        'created_time': email.created_time,
        'acked_by_node_id': email.acked_by_node_id,
    }

