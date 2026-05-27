# akita_email/protocol.py
import json
import logging
import time
import uuid
from typing import Dict, Any, Optional, Union, cast

from meshtastic.protobuf import mesh_pb2

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

def _get_meshtastic_payload_limit() -> int:
    return int(mesh_pb2.Constants.DATA_PAYLOAD_LEN)

def _serialize_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')

def _validate_payload_size(payload_bytes: bytes, label: str) -> bytes:
    payload_limit = _get_meshtastic_payload_limit()
    if len(payload_bytes) > payload_limit:
        raise ProtocolError(
            f"{label} payload is {len(payload_bytes)} bytes, exceeds Meshtastic limit of {payload_limit} bytes"
        )
    return payload_bytes

def _is_valid_node_id(value: Any) -> bool:
    return isinstance(value, int) and 0 <= value <= 0xFFFFFFFF

def _is_supported_meshtastic_port(portnum: Any) -> bool:
    return portnum in config.MESHTASTIC_ACCEPTED_PORTS

def _decode_payload_text(decoded_part: Dict[str, Any]) -> Optional[str]:
    payload_text = decoded_part.get('text')
    if isinstance(payload_text, str) and payload_text:
        return payload_text

    payload_bytes = decoded_part.get('payload')
    if isinstance(payload_bytes, str) and payload_bytes:
        return payload_bytes
    if isinstance(payload_bytes, (bytes, bytearray)) and payload_bytes:
        try:
            return bytes(payload_bytes).decode('utf-8')
        except UnicodeDecodeError:
            logger.warning("Received non-UTF8 Akita payload on supported Meshtastic port")
            return None

    return None

def _email_payload_dict(email: Email) -> Dict[str, Any]:
    payload = _encode_base(config.MSG_TYPE_EMAIL, email.message_id)
    payload.update({
        config.MSG_KEY_TO: email.to_node_id,
        config.MSG_KEY_FROM: email.from_node_id,
        config.MSG_KEY_SUBJECT: email.subject,
        config.MSG_KEY_BODY: email.body,
        config.MSG_KEY_TIMESTAMP: int(email.timestamp),
        config.MSG_KEY_HOPS: email.hops,
    })
    return payload

def encode_email_to_lora(email: Email) -> bytes:
    """
    Encodes an Email object into a compact JSON byte payload for Meshtastic.

    Args:
        email: The Email object to encode.

    Returns:
        A compact UTF-8 JSON byte payload.

    Raises:
        ProtocolError: If encoding fails.
    """
    payload = _email_payload_dict(email)
    try:
        return _validate_payload_size(_serialize_payload(payload), "Email")
    except (TypeError, UnicodeError) as e:
        logger.error(f"Failed to JSON encode email {email.message_id}: {e}", exc_info=True)
        raise ProtocolError(f"Failed to encode email: {e}") from e

def estimate_email_payload_size(email: Email) -> int:
    return len(_serialize_payload(_email_payload_dict(email)))

def validate_email_for_lora(email: Email) -> int:
    payload_bytes = _serialize_payload(_email_payload_dict(email))
    _validate_payload_size(payload_bytes, "Email")
    return len(payload_bytes)

def encode_ack_to_lora(ack_for_id: str, to_node_id: NodeId, from_node_id: NodeId) -> bytes:
    """
    Encodes an ACK message into a compact JSON byte payload for Meshtastic.

    Args:
        ack_for_id: The message_id of the email being acknowledged.
        to_node_id: The NodeId the ACK is being sent to (original sender).
        from_node_id: The NodeId sending the ACK (original recipient).

    Returns:
        A compact UTF-8 JSON byte payload.

    Raises:
        ProtocolError: If encoding fails.
    """
    # Create a unique ID for the ACK itself, derived from the original ID
    ack_msg_id = f"ack_{ack_for_id}_{uuid.uuid4().hex[:6]}"
    if len(ack_msg_id) > config.MESSAGE_ID_MAX_LENGTH:
        raise ProtocolError(
            f"Generated ACK ID exceeds maximum length of {config.MESSAGE_ID_MAX_LENGTH} characters"
        )
    payload = _encode_base(config.MSG_TYPE_ACK, ack_msg_id)
    payload.update({
        config.MSG_KEY_ACK_FOR: ack_for_id,
        config.MSG_KEY_TO: to_node_id,       # Destination of the ACK
        config.MSG_KEY_FROM: from_node_id,   # Source of the ACK
        config.MSG_KEY_TIMESTAMP: int(time.time()), # Timestamp of ACK generation
        config.MSG_KEY_HOPS: 0,              # ACK starts with 0 hops
    })
    try:
        return _validate_payload_size(_serialize_payload(payload), "ACK")
    except (TypeError, UnicodeError) as e:
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

    portnum = decoded_part.get("portnum")
    if not _is_supported_meshtastic_port(portnum):
        return None

    payload_str = _decode_payload_text(decoded_part)
    if not payload_str:
        return None

    try:
        # Attempt to parse the JSON payload
        data = json.loads(payload_str)
        if not isinstance(data, dict):
            logger.warning(f"Received non-dict JSON payload: {payload_str}")
            return None

        # --- Basic Akita Message Validation ---
        msg_type = data.get(config.MSG_KEY_TYPE)
        msg_id = data.get(config.MSG_KEY_ID)

        if not isinstance(msg_type, str) or not isinstance(msg_id, str):
            logger.warning(f"Received Akita message missing type or ID: {payload_str}")
            return None
        if len(msg_id) > config.MESSAGE_ID_MAX_LENGTH:
            logger.warning(f"Received Akita message with oversized ID: {msg_id}")
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
        if not _is_valid_node_id(packet_info['from_node_id']):
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
            if not _is_valid_node_id(data.get(config.MSG_KEY_TO)) or not _is_valid_node_id(data.get(config.MSG_KEY_FROM)):
                logger.warning(f"Invalid node ID in EMAIL (ID: {msg_id}): {data}")
                return None
            if data[config.MSG_KEY_TIMESTAMP] < 0 or data[config.MSG_KEY_HOPS] < 0:
                logger.warning(f"Invalid timestamp/hops in EMAIL (ID: {msg_id}): {data}")
                return None
            if not isinstance(data.get(config.MSG_KEY_BODY), str):
                logger.warning(f"Invalid body type in EMAIL (ID: {msg_id}): {data}")
                return None
            # Ensure subject is present (even if empty string), default if missing
            data.setdefault(config.MSG_KEY_SUBJECT, "")
            if not isinstance(data.get(config.MSG_KEY_SUBJECT), str):
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
            if not _is_valid_node_id(data.get(config.MSG_KEY_TO)) or not _is_valid_node_id(data.get(config.MSG_KEY_FROM)):
                logger.warning(f"Invalid node ID in ACK (ID: {msg_id}): {data}")
                return None
            if data[config.MSG_KEY_TIMESTAMP] < 0 or data[config.MSG_KEY_HOPS] < 0:
                logger.warning(f"Invalid timestamp/hops in ACK (ID: {msg_id}): {data}")
                return None
            # Validate ACK_FOR is a string
            if not isinstance(data.get(config.MSG_KEY_ACK_FOR), str):
                logger.warning(f"Invalid type for ack_for key in ACK (ID: {msg_id}): {data}")
                return None
            if len(data[config.MSG_KEY_ACK_FOR]) > config.MESSAGE_ID_MAX_LENGTH:
                logger.warning(f"ACK references oversized message ID (ID: {msg_id}): {data}")
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

