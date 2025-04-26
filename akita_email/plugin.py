# akita_email/plugin.py
import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
import serial
import threading
import time
import logging
from typing import Optional, Any, Dict, List, cast
from functools import partial

from . import config, protocol, database, models
from .exceptions import AkitaEmailError, CommunicationError, ProtocolError, RoutingError, DatabaseError, ConfigurationError
from .models import NodeId # Explicitly import NodeId

# Get a logger specific to this module
logger = config.setup_logger(__name__, config.PLUGIN_LOG_LEVEL, config.PLUGIN_LOG_FILE)

class AkitaEmailPlugin:
    """
    The core Akita eMail plugin logic that interacts with Meshtastic,
    manages the database, processes messages, and communicates with the companion CLI.
    """

    def __init__(self, mesh_interface: meshtastic.MeshInterface):
        """
        Initializes the Akita eMail Plugin.

        Args:
            mesh_interface: An initialized meshtastic.MeshInterface instance.

        Raises:
            ConfigurationError: If the local node ID cannot be determined.
            DatabaseError: If the database cannot be initialized.
        """
        self.interface = mesh_interface
        self.db: Optional[database.AkitaDatabase] = None
        self.companion_serial: Optional[serial.Serial] = None
        self.running = False
        self._lock = threading.Lock() # General purpose lock if needed for shared state
        self._threads: List[threading.Thread] = []
        self._local_node_id: Optional[NodeId] = None
        self._local_node_info: Optional[Dict[str, Any]] = None # Store more info if needed

        # --- Initialization Steps ---
        # 1. Get Local Node Info
        self._get_local_node_info() # Raises ConfigurationError on failure

        # 2. Initialize Database
        try:
            self.db = database.AkitaDatabase()
        except DatabaseError as e:
             logger.critical(f"Failed to initialize database: {e}", exc_info=True)
             raise # Re-raise DatabaseError as it's critical

        # 3. Companion connection is attempted in start()

        logger.info(f"AkitaEmailPlugin initialized for Node ID: {self._local_node_id:#0x}")

    def _get_local_node_info(self):
        """Fetches and stores the local node's information from the interface."""
        try:
            # Wait briefly for interface to potentially connect and get info
            for _ in range(5): # Try for ~5 seconds
                if self.interface.myInfo and self.interface.myInfo.my_node_num is not None:
                    break
                time.sleep(1)

            if not self.interface.myInfo or self.interface.myInfo.my_node_num is None:
                raise AkitaEmailError("Meshtastic interface did not provide local node info.")

            self._local_node_id = cast(NodeId, self.interface.myInfo.my_node_num) # Cast to NodeId type
            self._local_node_info = self.interface.myInfo.protobuf # Store raw protobuf if needed
            logger.info(f"Successfully retrieved local Node ID: {self._local_node_id:#0x}")

        except (AttributeError, AkitaEmailError, Exception) as e:
             logger.critical(f"Failed to get local node ID from Meshtastic interface: {e}", exc_info=True)
             raise ConfigurationError("Cannot determine local node ID. Ensure Meshtastic device is connected and responsive.") from e


    def _init_companion_connection(self):
        """Attempts to connect to the companion device via serial port."""
        if not config.COMPANION_SERIAL_PORT:
            logger.info("Companion serial port not configured. Skipping companion connection.")
            return

        try:
            self.companion_serial = serial.Serial(
                port=config.COMPANION_SERIAL_PORT,
                baudrate=config.COMPANION_SERIAL_BAUD,
                timeout=1 # Read timeout
            )
            logger.info(f"Successfully connected to companion interface on {config.COMPANION_SERIAL_PORT}")

            # Start the listener thread only if connection is successful
            companion_thread = threading.Thread(
                target=self._companion_listener_thread,
                name="CompanionListener",
                daemon=True
            )
            self._threads.append(companion_thread)
            companion_thread.start()

        except serial.SerialException as e:
            logger.warning(f"Companion interface connection failed on {config.COMPANION_SERIAL_PORT}: {e}. Plugin will run without companion features.")
            self.companion_serial = None # Ensure it's None if connection failed
        except Exception as e:
            logger.error(f"Unexpected error initializing companion connection: {e}", exc_info=True)
            self.companion_serial = None

    def start(self):
        """Starts the plugin's background threads and registers handlers."""
        if self.running:
            logger.warning("Plugin start() called but already running.")
            return

        logger.info(f"Starting Akita eMail Plugin v{config.VERSION}...")
        self.running = True

        # Re-confirm local node ID on start, just in case
        self._get_local_node_info()

        # Attempt companion connection
        self._init_companion_connection()

        # Register Meshtastic receive handler
        # Use functools.partial to pass 'self' instance to the handler method
        receive_handler_partial = partial(AkitaEmailPlugin._meshtastic_receive_handler, self)
        self.interface.addReceiveHandler(receive_handler_partial)
        logger.debug("Registered Meshtastic receive handler.")

        # Start outgoing message processing thread
        queue_thread = threading.Thread(
            target=self._outgoing_queue_processor_thread,
            name="OutgoingQueueProcessor",
            daemon=True # Allows main thread to exit even if this is running
        )
        self._threads.append(queue_thread)
        queue_thread.start()

        logger.info("Akita eMail Plugin started successfully.")

    def stop(self):
        """Stops the plugin, background threads, and cleans up resources."""
        if not self.running:
            logger.warning("Plugin stop() called but not running.")
            return

        logger.info("Stopping Akita eMail Plugin...")
        self.running = False # Signal threads to stop

        # Note: meshtastic-python doesn't have a removeReceiveHandler method.
        # The handler check self.running internally.

        # Wait for threads to finish (with a timeout)
        shutdown_timeout = 5.0 # seconds
        logger.debug(f"Waiting up to {shutdown_timeout}s for threads to complete...")
        start_time = time.time()
        for thread in self._threads:
            if thread.is_alive():
                 join_timeout = max(0.1, shutdown_timeout - (time.time() - start_time))
                 try:
                     thread.join(timeout=join_timeout)
                     if thread.is_alive():
                          logger.warning(f"Thread {thread.name} did not finish within timeout.")
                 except Exception as e:
                      logger.error(f"Error joining thread {thread.name}: {e}", exc_info=True)
        self._threads.clear() # Clear the list after attempting to join

        # Close companion serial connection
        if self.companion_serial and self.companion_serial.is_open:
            try:
                self.companion_serial.close()
                logger.info("Companion serial port closed.")
            except Exception as e:
                logger.error(f"Error closing companion serial port: {e}", exc_info=True)
            self.companion_serial = None

        # Close database connection
        if self.db:
            try:
                self.db.close()
                logger.info("Database connection closed.")
            except Exception as e:
                logger.error(f"Error closing database: {e}", exc_info=True)
            self.db = None

        # Note: We don't close the mesh_interface here, as it was passed in externally.
        # The caller script (run_plugin.py) is responsible for closing it.

        logger.info("Akita eMail Plugin stopped.")

    def _send_to_companion(self, message_payload: str):
        """
        Sends a pre-encoded JSON string line to the connected companion device.
        Handles potential serial communication errors.
        """
        if self.companion_serial and self.companion_serial.is_open:
            try:
                self.companion_serial.write(message_payload.encode('utf-8'))
                logger.debug(f"Sent to companion: {message_payload.strip()}")
                return True
            except serial.SerialException as e:
                logger.error(f"Serial error sending to companion: {e}. Closing port.")
                # Attempt to close the problematic port
                try:
                    self.companion_serial.close()
                except Exception:
                    pass # Ignore errors during close
                self.companion_serial = None # Mark as disconnected
                return False
            except Exception as e:
                logger.error(f"Unexpected error sending to companion: {e}", exc_info=True)
                return False
        else:
            # logger.debug("No companion connected or port closed, message not sent.")
            return False

    # --- Meshtastic Packet Handling ---

    def _meshtastic_receive_handler(self, packet: Dict[str, Any], interface: meshtastic.MeshInterface):
        """
        Callback method registered with meshtastic-python to handle incoming packets.
        Decodes, validates, and processes Akita messages (Email, ACK).
        """
        if not self.running or not self._local_node_id or not self.db:
            # logger.debug("Plugin stopped or not fully initialized, ignoring packet.")
            return # Ignore packets if plugin is stopped or not ready

        # Decode the packet using the protocol module
        decoded_data = protocol.decode_lora_packet(packet)
        if not decoded_data:
            # Not a valid Akita message or failed decoding (already logged by decode)
            return

        # Extract key information
        msg_type = decoded_data[config.MSG_KEY_TYPE]
        message_id = decoded_data[config.MSG_KEY_ID]
        packet_info = decoded_data['_packet_info_']
        # Node ID of the node that *directly* sent us this packet
        immediate_sender_id = cast(NodeId, packet_info['from_node_id'])

        logger.debug(f"Processing '{msg_type}' message (ID: {message_id}) received from {immediate_sender_id:#0x}")

        try:
            if msg_type == config.MSG_TYPE_EMAIL:
                self._handle_received_email_packet(decoded_data)
            elif msg_type == config.MSG_TYPE_ACK:
                self._handle_received_ack_packet(decoded_data)
            # Handle other types like ALIAS if implemented
            else:
                # Should not happen if decode_lora_packet is correct
                logger.warning(f"Received unhandled but decoded Akita message type '{msg_type}' (ID: {message_id})")

        except (DatabaseError, ProtocolError, CommunicationError, AkitaEmailError) as e:
            # Log specific Akita errors
            logger.error(f"Error processing message {message_id} from {immediate_sender_id:#0x}: {e}", exc_info=True)
        except Exception as e:
            # Log unexpected errors
            logger.error(f"Unexpected critical error processing message {message_id} from {immediate_sender_id:#0x}: {e}", exc_info=True)


    def _handle_received_email_packet(self, data: Dict[str, Any]):
        """Processes a decoded EMAIL message packet."""
        if not self.db or not self._local_node_id: return # Should not happen if called correctly

        message_id = data[config.MSG_KEY_ID]
        to_node_id = cast(NodeId, data[config.MSG_KEY_TO]) # Final destination
        original_sender_id = cast(NodeId, data[config.MSG_KEY_FROM])
        hops = data[config.MSG_KEY_HOPS]
        packet_info = data['_packet_info_']
        immediate_sender_id = cast(NodeId, packet_info['from_node_id'])

        # --- Check if the message is for this node ---
        if to_node_id == self._local_node_id:
            # --- Message is for us ---
            logger.info(f"Received direct email (ID: {message_id}) from {original_sender_id:#0x} via {immediate_sender_id:#0x}")

            # Create Email object from received data
            email = models.Email(
                message_id=message_id,
                to_node_id=to_node_id,
                from_node_id=original_sender_id,
                subject=data[config.MSG_KEY_SUBJECT],
                body=data[config.MSG_KEY_BODY],
                timestamp=data[config.MSG_KEY_TIMESTAMP],
                hops=hops,
                status=models.STATUS_RECEIVED # Mark as received
            )

            # Store in inbox (add_incoming_email handles duplicates)
            was_new = self.db.add_incoming_email(email)

            # Send ACK back to the original sender
            self._send_ack(
                ack_for_id=message_id,
                ack_to_node_id=original_sender_id, # Send ACK to original sender
                ack_from_node_id=self._local_node_id # ACK originates from us
            )

            # Notify companion device only if it was a new email
            if was_new:
                notification_payload = protocol.encode_companion_response(
                    config.RESP_NEW_EMAIL_NOTIFY,
                    message_id=message_id,
                    from_node_id=email.from_node_id,
                    subject=email.subject
                )
                self._send_to_companion(notification_payload)

        else:
            # --- Message is NOT for us - Attempt to forward ---
            logger.debug(f"Email (ID: {message_id}) from {original_sender_id:#0x} to {to_node_id:#0x} received for forwarding.")

            # Increment hop count
            new_hops = hops + 1

            # Check hop limit BEFORE storing/forwarding
            if new_hops > config.MESSAGE_HOP_LIMIT:
                logger.warning(f"Dropping email {message_id} - Hop limit exceeded ({new_hops}/{config.MESSAGE_HOP_LIMIT})")
                return # Do not forward

            # Create Email object for forwarding (status will be pending)
            email_to_forward = models.Email(
                message_id=message_id, # Keep original ID
                to_node_id=to_node_id,
                from_node_id=original_sender_id, # Keep original sender
                subject=data[config.MSG_KEY_SUBJECT],
                body=data[config.MSG_KEY_BODY],
                timestamp=data[config.MSG_KEY_TIMESTAMP], # Keep original timestamp
                hops=new_hops, # Use incremented hop count
                # Status defaults to PENDING
            )

            # Add to *our* outbox to be processed by the queue processor
            # add_outgoing_email handles duplicates (won't re-queue if already processing)
            self.db.add_outgoing_email(email_to_forward)


    def _handle_received_ack_packet(self, data: Dict[str, Any]):
        """Processes a decoded ACK message packet."""
        if not self.db or not self._local_node_id: return

        ack_message_id = data[config.MSG_KEY_ID]
        ack_for_id = data[config.MSG_KEY_ACK_FOR] # ID of the email being ACKed
        ack_to_node_id = cast(NodeId, data[config.MSG_KEY_TO]) # Who the ACK is intended for (us)
        ack_from_node_id = cast(NodeId, data[config.MSG_KEY_FROM]) # Who sent the ACK (original recipient)
        packet_info = data['_packet_info_']
        immediate_sender_id = cast(NodeId, packet_info['from_node_id']) # Node that forwarded the ACK to us

        # Ensure the ACK is actually intended for this node
        if ack_to_node_id != self._local_node_id:
            logger.debug(f"Ignoring ACK (ID: {ack_message_id}) intended for {ack_to_node_id:#0x}, not us ({self._local_node_id:#0x}).")
            # Note: We could potentially forward ACKs too, but it adds complexity.
            # If the original sender doesn't get the ACK, they will retry the email.
            return

        logger.info(f"Received ACK (ID: {ack_message_id}) for email (ID: {ack_for_id}) from {ack_from_node_id:#0x} via {immediate_sender_id:#0x}.")

        # Update the status of the original message in *our* outbox
        self.db.mark_outbox_acked(ack_for_id, ack_from_node_id)

        # Notify companion device of the successful delivery confirmation
        status_update_payload = protocol.encode_companion_response(
            config.RESP_STATUS_UPDATE,
            message_id=ack_for_id,
            status=models.STATUS_ACKED,
            recipient_node_id=ack_from_node_id # The node that confirmed receipt
        )
        self._send_to_companion(status_update_payload)


    def _send_ack(self, ack_for_id: str, ack_to_node_id: NodeId, ack_from_node_id: NodeId):
        """Encodes and sends an ACK message via Meshtastic."""
        try:
            ack_payload_str = protocol.encode_ack_to_lora(ack_for_id, ack_to_node_id, ack_from_node_id)
            logger.info(f"Sending ACK for email {ack_for_id} to {ack_to_node_id:#0x}")

            # ACKs are sent directly. We don't store ACKs in the outbox or wait for ACKs-of-ACKs.
            # Send directly to the node the ACK is intended for.
            self.interface.sendText(
                text=ack_payload_str,
                destinationId=ack_to_node_id,
                channelIndex=config.PRIMARY_CHANNEL_INDEX,
                # Use default hop limit for ACKs, maybe lower? Default seems reasonable.
                # hopLimit=config.MESSAGE_HOP_LIMIT
            )
            logger.debug(f"ACK for {ack_for_id} sent successfully to mesh.")

        except meshtastic.MeshInterfaceError as e:
             # Error during Meshtastic send operation
             logger.error(f"Meshtastic error sending ACK for {ack_for_id} to {ack_to_node_id:#0x}: {e}")
             # Cannot do much if ACK send fails. The original sender will eventually retry the email.
        except ProtocolError as e:
             # Error during ACK encoding
             logger.error(f"Protocol error encoding ACK for {ack_for_id}: {e}", exc_info=True)
        except Exception as e:
             # Catch any other unexpected errors during ACK sending
             logger.error(f"Unexpected error sending ACK for {ack_for_id}: {e}", exc_info=True)


    # --- Outgoing Queue Processing ---

    def _outgoing_queue_processor_thread(self):
        """Background thread loop that processes the outbox queue."""
        logger.info("Outgoing queue processor thread started.")
        while self.running:
            try:
                if not self.db: # Check if DB is available
                     logger.error("Database not available in queue processor. Sleeping.")
                     time.sleep(15)
                     continue

                # Get messages needing processing (pending or retry)
                emails_to_process = self.db.get_emails_to_send()

                if not emails_to_process:
                    # Queue is empty, sleep longer
                    time.sleep(5)
                    continue

                logger.debug(f"Processing {len(emails_to_process)} email(s) from outgoing queue.")
                for email in emails_to_process:
                    if not self.running: break # Exit loop if plugin is stopping

                    # Double-check hop limit just before sending
                    if email.hops >= config.MESSAGE_HOP_LIMIT:
                        logger.warning(f"Dropping email {email.message_id} from queue - Hop limit {email.hops}/{config.MESSAGE_HOP_LIMIT} reached before sending.")
                        self.db.mark_outbox_failed(email.message_id)
                        continue

                    # Attempt to send the email via Meshtastic
                    send_success = self._attempt_send_email(email)

                    # Update DB status based on send attempt *outcome*
                    if send_success:
                        self.db.update_outbox_after_send_attempt(email.message_id)
                    else:
                        # If _attempt_send_email failed (e.g., encoding error), it might have already marked it failed.
                        # If it was a Meshtastic send error, we just leave it for the next retry cycle.
                        logger.warning(f"Send attempt failed for email {email.message_id}. Will retry later if applicable.")
                        # Optionally: Notify companion immediately about send *attempt* failure? Less critical.

                    # Small delay between processing messages to avoid flooding
                    time.sleep(1.0)

            except DatabaseError as e:
                logger.error(f"Database error in outgoing queue processor: {e}", exc_info=True)
                time.sleep(10) # Wait longer after DB errors
            except Exception as e:
                logger.critical(f"Unexpected critical error in outgoing queue processor: {e}", exc_info=True)
                # Avoid tight loop on critical errors
                time.sleep(10)

            # Regular sleep even if queue wasn't empty, before checking again
            time.sleep(1)

        logger.info("Outgoing queue processor thread stopped.")


    def _attempt_send_email(self, email: models.Email) -> bool:
        """
        Encodes and attempts to send a single email via Meshtastic.
        Determines the destination for sendText().

        Args:
            email: The Email object to send.

        Returns:
            True if the send attempt was initiated successfully (doesn't guarantee delivery),
            False if an error occurred preventing the send attempt (e.g., encoding, immediate Meshtastic error).
        """
        if not self._local_node_id: return False # Should not happen

        try:
            # Encode the email payload for LoRa
            email_payload_str = protocol.encode_email_to_lora(email)

            # Determine the destination ID for the sendText call.
            # We send directly to the *final* recipient's Node ID.
            # Meshtastic's underlying router is responsible for finding the path.
            destination_id = email.to_node_id

            logger.info(f"Attempting to send/forward email {email.message_id} (Hop {email.hops}) "
                        f"from {email.from_node_id:#0x} to {destination_id:#0x} "
                        f"(Originated: {email.from_node_id == self._local_node_id})")

            # Calculate remaining hop limit for this transmission
            # Ensure it's at least 1 if hops already match limit (should have been caught earlier)
            remaining_hops = max(1, config.MESSAGE_HOP_LIMIT - email.hops)

            # Send the text message via Meshtastic
            self.interface.sendText(
                text=email_payload_str,
                destinationId=destination_id,
                channelIndex=config.PRIMARY_CHANNEL_INDEX,
                hopLimit=remaining_hops # Tell Meshtastic the max hops *from this point*
            )
            logger.debug(f"Email {email.message_id} handed off to Meshtastic interface for sending.")
            return True # Send attempt initiated

        except meshtastic.MeshInterfaceError as e:
            # Error during the Meshtastic send operation itself
            logger.error(f"Meshtastic error during send attempt for email {email.message_id} to {email.to_node_id:#0x}: {e}")
            # Do not mark as failed yet, allow retry mechanism to handle it
            return False # Send attempt failed
        except ProtocolError as e:
            # Error during encoding - this is fatal for this message
            logger.error(f"Protocol error encoding email {email.message_id}: {e}. Marking as failed.", exc_info=True)
            if self.db:
                self.db.mark_outbox_failed(email.message_id)
            return False # Send attempt failed (permanently)
        except Exception as e:
            # Catch any other unexpected errors during send attempt
            logger.error(f"Unexpected error during send attempt for email {email.message_id}: {e}", exc_info=True)
            return False # Send attempt failed


    # --- Companion Interaction ---

    def _companion_listener_thread(self):
        """Background thread loop to listen for commands from the companion CLI."""
        logger.info("Companion listener thread started.")
        while self.running:
            if not self.companion_serial or not self.companion_serial.is_open:
                logger.info("Companion serial port closed or unavailable. Listener thread stopping.")
                break # Exit thread if serial port is closed

            try:
                if self.companion_serial.in_waiting > 0:
                    # Read a line from the serial port
                    line = self.companion_serial.readline().decode('utf-8', errors='ignore')
                    line = line.strip()

                    if line:
                        logger.debug(f"Received from companion: {line}")
                        # Decode the JSON message
                        command_data = protocol.decode_companion_message(line)

                        if command_data and 'cmd' in command_data and 'params' in command_data:
                            # Process the valid command
                            self._handle_companion_command(command_data['cmd'], command_data['params'])
                        elif command_data:
                             logger.warning(f"Malformed command structure from companion: {command_data}")
                        # else: decode_companion_message already logged warnings for non-JSON etc.

            except serial.SerialException as e:
                logger.error(f"Companion serial communication error: {e}. Listener stopping.")
                # Ensure port is closed and reference removed
                if self.companion_serial:
                    try: self.companion_serial.close()
                    except Exception: pass
                self.companion_serial = None
                break # Exit thread
            except UnicodeDecodeError as e:
                 logger.warning(f"Companion sent non-UTF8 data, ignoring line: {e}")
            except Exception as e:
                # Catch unexpected errors in the listener loop
                logger.error(f"Unexpected error in companion listener: {e}", exc_info=True)
                time.sleep(1) # Avoid tight loop on unexpected errors

            # Small sleep to prevent high CPU usage if no data is waiting
            time.sleep(0.1)

        logger.info("Companion listener thread stopped.")


    def _handle_companion_command(self, command: str, params: Dict[str, Any]):
        """Processes validated commands received from the companion CLI."""
        if not self.db or not self._local_node_id:
             logger.error("Plugin not ready to handle companion command (no DB or Node ID).")
             self._send_error_to_companion("Plugin not fully initialized.")
             return

        logger.debug(f"Handling companion command: {command} with params: {params}")
        response_payload = None # Prepare for potential response

        try:
            if command == config.CMD_SEND_EMAIL:
                # Validate required parameters
                if not all(k in params for k in ['to_node_id', 'body']):
                    raise ProtocolError("Missing parameters for send_email command (to_node_id, body required)")

                # Validate and convert recipient ID (allow hex/decimal)
                try:
                    to_node_id_int = int(str(params['to_node_id']), 0)
                    to_node_id = cast(NodeId, to_node_id_int) # Cast to NodeId
                except (ValueError, TypeError):
                    raise ProtocolError(f"Invalid to_node_id format: {params['to_node_id']}. Use decimal or 0xHEX.")

                subject = params.get('subject', '') # Optional subject, default to empty
                body = params['body']

                if not isinstance(body, str) or not body:
                     raise ProtocolError("Email body cannot be empty.")

                # Create and queue the email
                email = models.Email(
                    to_node_id=to_node_id,
                    from_node_id=self._local_node_id, # Use plugin's node ID as sender
                    subject=str(subject), # Ensure string
                    body=body
                    # timestamp, message_id, etc., handled by model default factory
                )
                was_added = self.db.add_outgoing_email(email)
                if was_added:
                     logger.info(f"Email to {to_node_id:#0x} (ID: {email.message_id}) queued via companion command.")
                     # Optionally send immediate confirmation back? Status update is better.
                     response_payload = protocol.encode_companion_response(
                         config.RESP_STATUS_UPDATE,
                         message_id=email.message_id,
                         status=models.STATUS_PENDING,
                         info="Email queued for sending."
                     )
                else:
                     # This might happen if the companion sends the same command twice quickly
                     logger.warning(f"Attempted to queue duplicate email via companion (ID: {email.message_id})")
                     response_payload = protocol.encode_companion_response(
                         config.RESP_ERROR,
                         command=command,
                         message="Duplicate email ignored."
                     )


            elif command == config.CMD_READ_EMAILS:
                 limit = params.get('limit', 50)
                 try:
                     limit = int(limit)
                     if limit <= 0: limit = 50
                 except ValueError:
                     limit = 50

                 logger.debug(f"Companion requested inbox emails (limit: {limit}).")
                 inbox_emails = self.db.get_inbox_emails(limit=limit)
                 # Convert list of Email objects to list of dictionaries for JSON
                 email_list_dicts = [protocol.email_to_dict(mail) for mail in inbox_emails]
                 response_payload = protocol.encode_companion_response(
                     config.RESP_INBOX_LIST,
                     emails=email_list_dicts
                 )

            elif command == config.CMD_GET_STATUS:
                 message_id = params.get('message_id')
                 if not message_id or not isinstance(message_id, str):
                      raise ProtocolError("Missing or invalid 'message_id' parameter for get_status.")

                 logger.debug(f"Companion requested status for message ID: {message_id}")
                 email_status = self.db.get_outbox_status(message_id)
                 if email_status:
                      response_payload = protocol.encode_companion_response(
                          config.RESP_STATUS_UPDATE,
                          message_id=message_id,
                          status=email_status.status,
                          recipient_node_id=email_status.to_node_id,
                          acked_by=email_status.acked_by_node_id,
                          retry_count=email_status.retry_count,
                          last_attempt=email_status.last_attempt_time
                      )
                 else:
                      response_payload = protocol.encode_companion_response(
                          config.RESP_ERROR,
                          command=command,
                          message_id=message_id,
                          message="Message ID not found in outbox."
                      )


            elif command == config.CMD_SET_ALIAS:
                 alias = params.get('alias')
                 if alias is None: # Allow empty string to clear alias? Check Meshtastic rules.
                     raise ProtocolError("Missing 'alias' parameter for set_alias command.")
                 alias = str(alias).strip() # Ensure string and strip whitespace

                 # Validate alias according to Meshtastic rules (e.g., length, characters)
                 # Using a simplified check here, refer to meshtastic-python for exact rules.
                 MAX_ALIAS_LEN = 12 # Example limit
                 if len(alias) > MAX_ALIAS_LEN or not alias.isascii() or not alias.isprintable():
                      # Note: Meshtastic might have more specific rules (e.g., no leading/trailing spaces)
                      logger.warning(f"Invalid alias requested: '{alias}'. Length/characters invalid.")
                      raise ProtocolError(f"Invalid alias. Max {MAX_ALIAS_LEN} printable ASCII chars.")

                 logger.info(f"Setting Meshtastic node short name to '{alias}' via companion command.")
                 try:
                      # Use the Meshtastic interface to set the short name for the local node
                      # Note: This might require node restart on some firmware versions to take full effect.
                      self.interface.getNode(self._local_node_id).setShortName(alias)
                      # Meshtastic doesn't provide immediate confirmation, assume success if no exception
                      response_payload = protocol.encode_companion_response(
                          config.RESP_STATUS_UPDATE,
                          status='alias_set',
                          alias=alias,
                          info="Alias set request sent to node. May require node restart."
                      )
                 except Exception as e:
                      logger.error(f"Failed to send setShortName request to node: {e}", exc_info=True)
                      raise CommunicationError(f"Failed to set node alias via Meshtastic: {e}") from e

            elif command == config.CMD_PING_PLUGIN:
                 logger.debug("Received ping from companion.")
                 response_payload = protocol.encode_companion_response(config.RESP_PONG, timestamp=time.time())

            else:
                logger.warning(f"Received unknown command from companion: {command}")
                response_payload = protocol.encode_companion_response(
                    config.RESP_ERROR,
                    command=command,
                    message="Unknown command received by plugin."
                )

            # Send the prepared response (if any) back to the companion
            if response_payload:
                self._send_to_companion(response_payload)

        except (ProtocolError, DatabaseError, CommunicationError, ValueError, TypeError) as e:
             # Handle known errors during command processing
             logger.error(f"Error handling companion command '{command}': {e}", exc_info=True)
             # Send specific error back to companion
             self._send_error_to_companion(f"Error processing command '{command}': {e}", command)
        except Exception as e:
             # Handle unexpected errors
             logger.critical(f"Unexpected critical error handling companion command '{command}': {e}", exc_info=True)
             # Send generic error back to companion
             self._send_error_to_companion("Internal plugin error occurred.", command)

    def _send_error_to_companion(self, error_message: str, command: Optional[str] = None):
        """Helper method to send a formatted error response to the companion."""
        try:
            error_payload = protocol.encode_companion_response(
                config.RESP_ERROR,
                command=command,
                message=str(error_message)
            )
            self._send_to_companion(error_payload)
        except Exception as e:
            logger.error(f"Failed to send error message to companion: {e}", exc_info=True)

