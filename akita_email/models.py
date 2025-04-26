# akita_email/models.py
import time
import uuid
from dataclasses import dataclass, field
from typing import NewType

# Define a specific type for Meshtastic Node IDs for clarity
# Node IDs are unsigned 32-bit integers in Meshtastic
NodeId = NewType('NodeId', int)

# Define possible statuses for outgoing messages
OutboxStatus = NewType('OutboxStatus', str)
STATUS_PENDING = OutboxStatus('pending')   # Newly created, not yet sent
STATUS_SENT = OutboxStatus('sent')       # Attempted sending, awaiting ACK
STATUS_ACKED = OutboxStatus('acked')     # ACK received, delivery confirmed
STATUS_FAILED = OutboxStatus('failed')   # Max retries or expiry reached
STATUS_RECEIVED = OutboxStatus('received') # Used for inbox emails for consistency

@dataclass
class Email:
    """
    Represents an email message within the Akita system.
    Used for both incoming (Inbox) and outgoing (Outbox) messages.
    """
    # Core Email Fields (transmitted over LoRa)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())) # Unique ID
    to_node_id: NodeId          # Destination Meshtastic Node ID
    from_node_id: NodeId        # Source Meshtastic Node ID
    subject: str                # Email subject line
    body: str                   # Email content/body
    timestamp: float = field(default_factory=time.time) # Original creation time (epoch float)
    hops: int = 0               # Hop count (incremented on forward)

    # --- Fields primarily for Outbox Management (not transmitted directly) ---
    status: OutboxStatus = STATUS_PENDING # Current status of the message
    last_attempt_time: float = 0.0        # Timestamp of the last send attempt
    retry_count: int = 0                  # Number of send attempts made
    created_time: float = field(default_factory=time.time) # DB record creation time
    # Optional: Store the Node ID of the node that sent the ACK for this message
    acked_by_node_id: NodeId | None = None

    def __post_init__(self):
        """Basic validation after initialization."""
        if not isinstance(self.to_node_id, int):
             raise ValueError("to_node_id must be an integer")
        if not isinstance(self.from_node_id, int):
             raise ValueError("from_node_id must be an integer")
        # Ensure subject and body are strings, even if empty
        self.subject = str(self.subject) if self.subject is not None else ""
        self.body = str(self.body) if self.body is not None else ""


# You could potentially add other models here later, e.g., for Aliases if
# you implement a more complex alias system than just the node's short name.
# @dataclass
# class AliasMapping:
#    alias_name: str
#    node_id: NodeId
#    last_updated: float

