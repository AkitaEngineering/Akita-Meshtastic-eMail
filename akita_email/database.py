# akita_email/database.py
import sqlite3
import time
import logging
from typing import List, Optional

from . import config
from .models import Email, NodeId, STATUS_PENDING, STATUS_SENT, STATUS_ACKED, STATUS_FAILED, STATUS_RECEIVED
from .exceptions import DatabaseError

# Get a logger specific to this module
logger = config.setup_logger(__name__, config.PLUGIN_LOG_LEVEL, config.PLUGIN_LOG_FILE, console=False)

class AkitaDatabase:
    """
    Handles all SQLite database operations for the Akita eMail Plugin.
    Manages inbox and outbox tables.
    Ensures thread safety for database operations using a connection per instance
    and relying on SQLite's default serialization for writes within transactions.
    """

    def __init__(self, db_path: str = config.PLUGIN_DATABASE_FILE):
        """
        Initializes the database connection and creates tables if they don't exist.

        Args:
            db_path: The path to the SQLite database file.

        Raises:
            DatabaseError: If the connection or table creation fails.
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        try:
            # Connect to the database. check_same_thread=False allows the connection
            # to be used by multiple threads (plugin, queue processor, companion listener),
            # but requires careful transaction management (using 'with self.conn:')
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.conn.row_factory = sqlite3.Row # Access columns by name (e.g., row['subject'])
            self._create_tables()
            logger.info(f"Database initialized successfully at {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Database connection or table creation failed: {e}", exc_info=True)
            raise DatabaseError(f"Database connection/setup failed: {e}") from e

    def _create_tables(self):
        """Creates the inbox and outbox tables if they don't exist."""
        if not self.conn:
            raise DatabaseError("Database connection is not available.")

        try:
            with self.conn: # Use context manager for automatic commit/rollback
                # Inbox: Stores emails received by this node
                self.conn.execute('''
                    CREATE TABLE IF NOT EXISTS inbox (
                        message_id TEXT PRIMARY KEY,
                        to_node_id INTEGER NOT NULL,
                        from_node_id INTEGER NOT NULL,
                        subject TEXT,
                        body TEXT NOT NULL,
                        timestamp REAL NOT NULL, -- Original send time
                        hops INTEGER NOT NULL,
                        received_time REAL DEFAULT (strftime('%s', 'now')) -- When we received it
                    )
                ''')
                # Outbox: Stores emails originated from or being forwarded by this node
                self.conn.execute('''
                    CREATE TABLE IF NOT EXISTS outbox (
                        message_id TEXT PRIMARY KEY,
                        to_node_id INTEGER NOT NULL,
                        from_node_id INTEGER NOT NULL, -- Original sender
                        subject TEXT,
                        body TEXT NOT NULL,
                        timestamp REAL NOT NULL,    -- Original creation time
                        hops INTEGER DEFAULT 0,     -- Hops when *we* send/forward it
                        status TEXT NOT NULL DEFAULT ?, -- pending, sent, acked, failed
                        last_attempt_time REAL DEFAULT 0,
                        retry_count INTEGER DEFAULT 0,
                        created_time REAL DEFAULT (strftime('%s', 'now')), -- When added to our outbox
                        acked_by_node_id INTEGER -- Node ID that sent the ACK
                    )
                ''', (STATUS_PENDING,)) # Set default status using constant
                # Add indexes for performance on common lookups
                self.conn.execute('CREATE INDEX IF NOT EXISTS idx_inbox_received_time ON inbox(received_time)')
                self.conn.execute('CREATE INDEX IF NOT EXISTS idx_outbox_status_attempt ON outbox(status, last_attempt_time)')
                self.conn.execute('CREATE INDEX IF NOT EXISTS idx_outbox_created_time ON outbox(created_time)')

            logger.debug("Database tables verified/created.")
        except sqlite3.Error as e:
            logger.error(f"Failed to create/verify database tables: {e}", exc_info=True)
            raise DatabaseError(f"Failed to create/verify tables: {e}") from e

    def add_incoming_email(self, email: Email) -> bool:
        """
        Stores a received email in the inbox table. Ignores duplicates based on message_id.

        Args:
            email: The Email object to store.

        Returns:
            True if the email was added, False if it was a duplicate.

        Raises:
            DatabaseError: If the database operation fails.
        """
        if not self.conn: raise DatabaseError("Database not connected")
        try:
            with self.conn:
                cursor = self.conn.execute(
                    '''INSERT OR IGNORE INTO inbox
                       (message_id, to_node_id, from_node_id, subject, body, timestamp, hops)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (email.message_id, email.to_node_id, email.from_node_id,
                     email.subject, email.body, email.timestamp, email.hops)
                )
            if cursor.rowcount > 0:
                logger.info(f"Stored incoming email {email.message_id} from {email.from_node_id:#0x}")
                return True
            else:
                logger.debug(f"Ignored duplicate incoming email {email.message_id}")
                return False
        except sqlite3.Error as e:
            logger.error(f"Failed to add incoming email {email.message_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to add incoming email {email.message_id}: {e}") from e

    def add_outgoing_email(self, email: Email) -> bool:
        """
        Adds an email to the outbox queue for sending/forwarding.
        Ignores duplicates based on message_id (e.g., if forwarding a message already seen).

        Args:
            email: The Email object to queue.

        Returns:
            True if the email was added, False if it was a duplicate.

        Raises:
            DatabaseError: If the database operation fails.
        """
        if not self.conn: raise DatabaseError("Database not connected")
        try:
            with self.conn:
                cursor = self.conn.execute(
                    '''INSERT OR IGNORE INTO outbox
                       (message_id, to_node_id, from_node_id, subject, body, timestamp, hops, status, created_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (email.message_id, email.to_node_id, email.from_node_id,
                     email.subject, email.body, email.timestamp, email.hops,
                     STATUS_PENDING, email.created_time) # Start as pending
                )
            if cursor.rowcount > 0:
                logger.info(f"Queued outgoing/forwarding email {email.message_id} to {email.to_node_id:#0x}")
                return True
            else:
                logger.debug(f"Ignored duplicate outgoing/forwarding email {email.message_id}")
                return False
        except sqlite3.Error as e:
            logger.error(f"Failed to queue outgoing email {email.message_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to queue outgoing email {email.message_id}: {e}") from e

    def get_emails_to_send(self) -> List[Email]:
        """
        Retrieves emails from the outbox that need sending or retrying.
        Checks retry intervals and expiry times. Marks expired messages as failed.

        Returns:
            A list of Email objects ready to be sent/retried.
        """
        if not self.conn: raise DatabaseError("Database not connected")
        emails_to_send = []
        now = time.time()
        retry_cutoff = now - config.MESSAGE_RETRY_INTERVAL
        expiry_cutoff = now - config.MESSAGE_EXPIRY_TIME

        try:
            # Use a separate cursor for potentially updating expired messages within the loop
            update_cursor = self.conn.cursor()

            with self.conn: # Transaction for reading and potentially updating expired
                # Select messages that are pending or sent but past the retry interval
                read_cursor = self.conn.execute(
                    f'''SELECT * FROM outbox
                       WHERE status = ? OR (status = ? AND last_attempt_time < ?)
                       ORDER BY created_time ASC''', # Process oldest first
                    (STATUS_PENDING, STATUS_SENT, retry_cutoff)
                )

                for row in read_cursor.fetchall():
                    # Check for expiry first
                    if row['created_time'] < expiry_cutoff:
                        if row['status'] != STATUS_FAILED: # Avoid redundant updates
                            logger.warning(f"Email {row['message_id']} to {row['to_node_id']:#0x} expired after {config.MESSAGE_EXPIRY_TIME}s. Marking as failed.")
                            # Update status directly using the separate cursor
                            update_cursor.execute(
                                "UPDATE outbox SET status = ?, last_attempt_time = ? WHERE message_id = ?",
                                (STATUS_FAILED, now, row['message_id'])
                            )
                        continue # Skip this expired message

                    # If not expired, create Email object and add to list
                    email = Email(
                        message_id=row['message_id'],
                        to_node_id=row['to_node_id'],
                        from_node_id=row['from_node_id'],
                        subject=row['subject'],
                        body=row['body'],
                        timestamp=row['timestamp'],
                        hops=row['hops'],
                        status=row['status'],
                        last_attempt_time=row['last_attempt_time'],
                        retry_count=row['retry_count'],
                        created_time=row['created_time'],
                        acked_by_node_id=row['acked_by_node_id']
                    )
                    emails_to_send.append(email)

            return emails_to_send
        except sqlite3.Error as e:
            logger.error(f"Failed to retrieve pending emails: {e}", exc_info=True)
            # Don't raise here to avoid crashing the queue processor, return empty list
            return []

    def update_outbox_after_send_attempt(self, message_id: str):
        """Updates the outbox after a send attempt: increments retry, sets status to 'sent', updates time."""
        if not self.conn: raise DatabaseError("Database not connected")
        now = time.time()
        try:
            with self.conn:
                self.conn.execute(
                    '''UPDATE outbox
                       SET status = ?, last_attempt_time = ?, retry_count = retry_count + 1
                       WHERE message_id = ?''',
                    (STATUS_SENT, now, message_id)
                )
            logger.debug(f"Updated outbox status to '{STATUS_SENT}' for {message_id} after send attempt.")
        except sqlite3.Error as e:
            logger.error(f"Failed to update outbox status after send attempt for {message_id}: {e}", exc_info=True)
            # Log error but don't raise to keep queue processor running

    def mark_outbox_acked(self, message_id: str, acked_by: NodeId):
        """Marks an outbox message as acknowledged."""
        if not self.conn: raise DatabaseError("Database not connected")
        now = time.time()
        try:
            with self.conn:
                self.conn.execute(
                    '''UPDATE outbox
                       SET status = ?, last_attempt_time = ?, acked_by_node_id = ?
                       WHERE message_id = ?''',
                    (STATUS_ACKED, now, acked_by, message_id)
                )
            logger.info(f"Marked outbox email {message_id} as '{STATUS_ACKED}' by {acked_by:#0x}.")
        except sqlite3.Error as e:
            logger.error(f"Failed to mark outbox email {message_id} as '{STATUS_ACKED}': {e}", exc_info=True)
            # Log error but don't raise

    def mark_outbox_failed(self, message_id: str):
        """Explicitly marks an outbox message as failed (e.g., due to encoding error)."""
        if not self.conn: raise DatabaseError("Database not connected")
        now = time.time()
        try:
            with self.conn:
                self.conn.execute(
                    '''UPDATE outbox SET status = ?, last_attempt_time = ?
                       WHERE message_id = ?''',
                    (STATUS_FAILED, now, message_id)
                )
            logger.warning(f"Marked outbox email {message_id} as '{STATUS_FAILED}'.")
        except sqlite3.Error as e:
            logger.error(f"Failed to mark outbox email {message_id} as '{STATUS_FAILED}': {e}", exc_info=True)
            # Log error but don't raise


    def get_inbox_emails(self, limit: int = 50) -> List[Email]:
        """Retrieves emails from the inbox, newest received first."""
        if not self.conn: raise DatabaseError("Database not connected")
        emails = []
        try:
            with self.conn: # Read operation, transaction not strictly needed but good practice
                cursor = self.conn.execute(
                    '''SELECT * FROM inbox
                       ORDER BY received_time DESC LIMIT ?''', (limit,)
                )
                for row in cursor.fetchall():
                    email = Email(
                        message_id=row['message_id'],
                        to_node_id=row['to_node_id'],
                        from_node_id=row['from_node_id'],
                        subject=row['subject'],
                        body=row['body'],
                        timestamp=row['timestamp'],
                        hops=row['hops'],
                        status=STATUS_RECEIVED, # Mark as received for consistency
                        created_time=row['received_time'] # Use received time here
                    )
                    emails.append(email)
            return emails
        except sqlite3.Error as e:
            logger.error(f"Failed to retrieve inbox emails: {e}", exc_info=True)
            return [] # Return empty list on error

    def get_outbox_status(self, message_id: str) -> Optional[Email]:
        """Retrieves the current status and details of a specific outbox message."""
        if not self.conn: raise DatabaseError("Database not connected")
        try:
            with self.conn:
                cursor = self.conn.execute(
                    "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
                )
                row = cursor.fetchone()
                if row:
                    return Email(
                        message_id=row['message_id'],
                        to_node_id=row['to_node_id'],
                        from_node_id=row['from_node_id'],
                        subject=row['subject'],
                        body=row['body'], # Might not need body for status check?
                        timestamp=row['timestamp'],
                        hops=row['hops'],
                        status=row['status'],
                        last_attempt_time=row['last_attempt_time'],
                        retry_count=row['retry_count'],
                        created_time=row['created_time'],
                        acked_by_node_id=row['acked_by_node_id']
                    )
                else:
                    return None # Message not found
        except sqlite3.Error as e:
            logger.error(f"Failed to retrieve outbox status for {message_id}: {e}", exc_info=True)
            return None # Return None on error

    def close(self):
        """Closes the database connection."""
        if self.conn:
            try:
                self.conn.close()
                self.conn = None
                logger.info("Database connection closed.")
            except sqlite3.Error as e:
                logger.error(f"Error closing database connection: {e}", exc_info=True)

    def __del__(self):
        """Ensure connection is closed when the object is garbage collected."""
        self.close()

