# akita_email/companion_cli.py
import serial
import json
import time
import threading
import logging
import sys
import readline # Enables history and editing in input()
from typing import Optional, Dict, Any, List

# Assumes running from repository root or package installed
try:
    from . import config, protocol
    from .models import NodeId # Import NodeId for type hints
except ImportError:
    # Allow running as a script from within the directory for development
    import config
    import protocol
    from models import NodeId


# Setup logger for the companion CLI
# Note: config.setup_logger logs to file and optionally console.
# We might want only console logging here, or separate config.
log_file = config.COMPANION_LOG_FILE
logger = config.setup_logger(config.APP_NAME + ".Companion", config.COMPANION_LOG_LEVEL, log_file, console=True)


# Global variable to signal the listener thread to stop
listener_running = False
# Global variable for the serial connection to the plugin
plugin_serial: Optional[serial.Serial] = None

def connect_to_plugin() -> Optional[serial.Serial]:
    """
    Establishes and returns a serial connection to the Akita Plugin process.
    Retries a few times before giving up.
    """
    port = config.COMPANION_PLUGIN_PORT
    baud = config.COMPANION_PLUGIN_BAUD
    retries = 3
    retry_delay = 2 # seconds

    if not port:
        logger.error("Companion plugin serial port (COMPANION_PLUGIN_PORT) is not configured in config.py.")
        print("Error: Plugin serial port not configured. Please set COMPANION_PLUGIN_PORT in akita_email/config.py.", file=sys.stderr)
        return None

    logger.info(f"Attempting to connect to Akita Plugin on {port} at {baud} baud...")
    for attempt in range(retries):
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=1 # Read timeout
            )
            logger.info(f"Connected successfully to Akita Plugin on attempt {attempt + 1}.")
            print(f"--- Connected to Akita Plugin on {port} ---")
            return ser
        except serial.SerialException as e:
            logger.warning(f"Connection attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                print(f"Warning: Could not connect to plugin, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to Akita Plugin after {retries} attempts.")
                print(f"\nError: Could not connect to the Akita Plugin on {port}.", file=sys.stderr)
                print("Ensure the plugin is running and the correct port (COMPANION_PLUGIN_PORT) is specified in config.py.", file=sys.stderr)
                return None
        except Exception as e:
            logger.error(f"Unexpected error during connection attempt {attempt + 1}: {e}", exc_info=True)
            print(f"Error: An unexpected error occurred while connecting: {e}", file=sys.stderr)
            return None # Unexpected error, stop trying
    return None # Should not be reached, but satisfies type checker


def send_command_to_plugin(ser: serial.Serial, command_type: str, **kwargs):
    """Encodes and sends a command to the plugin via the serial connection."""
    if not ser or not ser.is_open:
        logger.error(f"Cannot send command '{command_type}': Serial port not open.")
        print("Error: Not connected to the plugin.", file=sys.stderr)
        return False
    try:
        cmd_str = protocol.encode_companion_command(command_type, **kwargs)
        ser.write(cmd_str.encode('utf-8'))
        logger.debug(f"Sent command: {cmd_str.strip()}")
        return True
    except (serial.SerialException, protocol.ProtocolError) as e:
        logger.error(f"Error sending command '{command_type}': {e}", exc_info=True)
        print(f"Error sending command: {e}", file=sys.stderr)
        # Assume connection is lost on serial error
        if isinstance(e, serial.SerialException):
             close_plugin_connection() # Attempt to close gracefully
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending command '{command_type}': {e}", exc_info=True)
        print(f"Unexpected error sending command: {e}", file=sys.stderr)
        return False

def plugin_response_listener_thread(ser: serial.Serial):
    """
    Background thread that continuously listens for responses and notifications
    from the plugin process over the serial connection.
    """
    global listener_running
    listener_running = True
    logger.info("Plugin response listener thread started.")

    while listener_running:
        if not ser or not ser.is_open:
            logger.warning("Listener thread: Serial port closed or unavailable. Stopping.")
            listener_running = False # Signal main loop/self to stop
            break

        try:
            if ser.in_waiting > 0:
                # Read one line from the plugin
                line = ser.readline().decode('utf-8', errors='ignore')
                line = line.strip()

                if line:
                    logger.debug(f"Received from plugin: {line}")
                    # Decode the JSON message
                    response_data = protocol.decode_companion_message(line)

                    # Process if valid response structure
                    if response_data and 'resp' in response_data and 'data' in response_data:
                        display_plugin_response(response_data['resp'], response_data['data'])
                    elif response_data:
                         logger.warning(f"Malformed response structure from plugin: {response_data}")
                    # else: decode already logged warnings for non-JSON etc.

            # Small sleep to prevent 100% CPU usage when idle
            time.sleep(0.1)

        except serial.SerialException as e:
            logger.error(f"Plugin serial communication error in listener: {e}. Stopping listener.")
            print("\nError: Lost connection to Akita Plugin.", file=sys.stderr)
            close_plugin_connection() # Close connection
            listener_running = False # Signal main loop to exit
            break # Exit thread loop
        except UnicodeDecodeError as e:
             logger.warning(f"Received non-UTF8 data from plugin, ignoring line: {e}")
        except Exception as e:
            # Catch unexpected errors in the listener loop
            logger.error(f"Unexpected error in plugin listener thread: {e}", exc_info=True)
            # Avoid tight loop on unexpected errors, maybe signal main thread?
            time.sleep(1)

    logger.info("Plugin response listener thread stopped.")


def display_plugin_response(resp_type: str, data: Dict[str, Any]):
    """
    Formats and prints responses/notifications received from the plugin
    to the console in a user-friendly way.
    """
    # Use readline's tools to preserve current input line
    current_input = readline.get_line_buffer()
    sys.stdout.write('\r' + ' ' * len(current_input) + '\r') # Clear current line

    # Print the formatted response
    print("--- Plugin Response ---")
    if resp_type == config.RESP_NEW_EMAIL_NOTIFY:
        print(f"[*] New Email Received!")
        print(f"    ID:   {data.get('message_id', 'N/A')}")
        print(f"    From: {data.get('from_node_id', '?'):#0x}") # Display Node ID in hex
        print(f"    Subj: {data.get('subject', '(No Subject)')}")
        print(f"    (Use 'read' command to see full message)")

    elif resp_type == config.RESP_INBOX_LIST:
        emails = data.get('emails', [])
        if not emails:
            print("    Inbox is empty.")
        else:
            print(f"    Inbox ({len(emails)} messages):")
            for i, email_dict in enumerate(emails):
                 # Convert timestamp back to readable format
                 ts_float = email_dict.get('timestamp', 0)
                 ts_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts_float)) if ts_float else "N/A"
                 from_id = email_dict.get('from_node_id', '?')
                 subj = email_dict.get('subject', '(No Subject)')
                 body_preview = email_dict.get('body', '')[:60] # Preview first 60 chars
                 if len(email_dict.get('body', '')) > 60: body_preview += "..."

                 print(f"    [{i+1}] From: {from_id:#0x}  Rcvd: {ts_str}")
                 print(f"        Subj: {subj}")
                 print(f"        Body: {body_preview}")
                 print(f"        (ID: {email_dict.get('message_id', 'N/A')})")
            print("    --- End of List ---")

    elif resp_type == config.RESP_STATUS_UPDATE:
         status = data.get('status', 'unknown')
         msg_id = data.get('message_id')
         info = data.get('info')
         recipient = data.get('recipient_node_id')
         acked_by = data.get('acked_by')
         retries = data.get('retry_count')
         alias = data.get('alias')

         print(f"[*] Status Update:")
         if msg_id: print(f"    Message ID: {msg_id}")
         print(f"    Status: {status.upper()}")
         if recipient: print(f"    Recipient: {recipient:#0x}")
         if acked_by: print(f"    Confirmed By: {acked_by:#0x}")
         if retries is not None: print(f"    Retry Count: {retries}")
         if alias: print(f"    Alias: '{alias}'")
         if info: print(f"    Info: {info}")

    elif resp_type == config.RESP_PONG:
         ts = data.get('timestamp', time.time())
         latency = time.time() - ts
         print(f"[*] Pong received from plugin! (Latency: {latency:.3f}s)")

    elif resp_type == config.RESP_ERROR:
         print(f"[!] Error from Plugin:")
         if data.get('command'): print(f"    Command: {data['command']}")
         if data.get('message_id'): print(f"    Message ID: {data['message_id']}")
         print(f"    Message: {data.get('message', 'Unknown error')}")

    else:
        # Fallback for unknown response types
        print(f"[*] Unknown Response Type '{resp_type}':")
        print(f"    Data: {json.dumps(data, indent=2)}")

    print("-----------------------")

    # Restore the user's input line and cursor position
    sys.stdout.write(current_input)
    sys.stdout.flush()


def print_help():
    """Prints the help message for CLI commands."""
    print("\n--- Akita eMail Companion Commands ---")
    print("  send                 - Send a new email message.")
    print("  read [limit]         - Read received emails from inbox (default limit 50).")
    print("  status <message_id>  - Check the status of an outgoing message.")
    print("  alias <name>         - Set your Meshtastic node alias (short name, max ~12 chars).")
    print("                       - Use 'alias \"\"' to clear the alias.")
    print("  ping                 - Check connectivity with the Akita plugin.")
    print("  help                 - Show this help message.")
    print("  quit / exit          - Exit the companion CLI.")
    print("------------------------------------")

def parse_node_id(id_str: str) -> Optional[NodeId]:
    """Parses a string (hex or dec) into a valid NodeId integer."""
    try:
        node_id_int = int(id_str, 0) # Base 0 auto-detects hex (0x) or decimal
        # Basic validation for typical NodeId range (unsigned 32-bit)
        if 0 <= node_id_int <= 0xFFFFFFFF:
             return cast(NodeId, node_id_int)
        else:
             print(f"Error: Node ID '{id_str}' is out of the valid range (0 to 0xFFFFFFFF).", file=sys.stderr)
             return None
    except (ValueError, TypeError):
        print(f"Error: Invalid Node ID format '{id_str}'. Use decimal (e.g., 12345) or hex (e.g., 0xabcd1234).", file=sys.stderr)
        return None

def close_plugin_connection():
    """Closes the serial connection to the plugin if it's open."""
    global plugin_serial
    if plugin_serial and plugin_serial.is_open:
        try:
            plugin_serial.close()
            logger.info("Plugin serial connection closed.")
        except Exception as e:
            logger.error(f"Error closing plugin serial connection: {e}", exc_info=True)
    plugin_serial = None


def main_cli_loop():
    """The main interactive loop for the command-line interface."""
    global listener_running, plugin_serial

    while listener_running: # Loop relies on listener thread status
        try:
            # Use input() with readline support
            command_line = input("Akita> ").strip()
            if not command_line:
                continue # Skip empty input

            parts = command_line.split(maxsplit=1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            # --- Command Handling ---
            if cmd in ["quit", "exit"]:
                listener_running = False # Signal listener thread to stop
                break # Exit main loop

            elif cmd == "help":
                print_help()

            elif cmd == "ping":
                 print("Pinging plugin...")
                 send_command_to_plugin(plugin_serial, config.CMD_PING_PLUGIN)

            elif cmd == "send":
                try:
                    recipient_str = input("To Node ID (e.g., 0xabcd1234 or decimal): ").strip()
                    recipient_id = parse_node_id(recipient_str)
                    if recipient_id is None: continue # Error already printed by parse_node_id

                    subject = input("Subject: ").strip()
                    print("Body (end with 'EOF' or Ctrl+D on a new line):")
                    body_lines = []
                    while True:
                        try:
                            line = input()
                            # Allow EOF marker, case-insensitive
                            if line.strip().upper() == "EOF": break
                            body_lines.append(line)
                        except EOFError:
                            print() # Print newline after Ctrl+D
                            break # Ctrl+D also ends input

                    body = "\n".join(body_lines)

                    if not body:
                         print("Email body cannot be empty. Sending cancelled.")
                         continue

                    print("Sending email via plugin...")
                    send_command_to_plugin(plugin_serial, config.CMD_SEND_EMAIL,
                                           to_node_id=recipient_id,
                                           subject=subject,
                                           body=body)
                    # Confirmation/status will arrive asynchronously via listener

                except (ValueError, IndexError):
                    print("Invalid input. Please follow the prompts.")
                except EOFError:
                     print("\nInput cancelled.")
                except Exception as e: # Catch unexpected errors during send input
                     print(f"An error occurred during the 'send' command: {e}")
                     logger.error(f"Error during 'send' command input: {e}", exc_info=True)

            elif cmd == "read":
                 limit = 50
                 if args.isdigit() and int(args) > 0:
                      limit = int(args)
                 elif args:
                      print("Usage: read [positive_number_limit]")
                      continue
                 print(f"Requesting last {limit} emails from plugin inbox...")
                 send_command_to_plugin(plugin_serial, config.CMD_READ_EMAILS, limit=limit)

            elif cmd == "status":
                 if not args:
                     print("Usage: status <message_id>")
                     continue
                 message_id = args.strip()
                 print(f"Requesting status for message ID: {message_id}...")
                 send_command_to_plugin(plugin_serial, config.CMD_GET_STATUS, message_id=message_id)

            elif cmd == "alias":
                 if not args and args != '""': # Allow 'alias ""' to clear
                     print("Usage: alias <your_new_alias>")
                     print("       alias \"\"  (to clear alias)")
                     print("Alias should be max ~12 characters, printable ASCII.")
                     continue
                 # Handle quoted empty string for clearing alias
                 alias_name = args[1:-1] if args.startswith('"') and args.endswith('"') else args

                 print(f"Requesting plugin to set alias to '{alias_name}'...")
                 send_command_to_plugin(plugin_serial, config.CMD_SET_ALIAS, alias=alias_name)

            else:
                print(f"Unknown command: '{cmd}'. Type 'help' for available commands.")

        except KeyboardInterrupt:
            print("\nCtrl+C detected. Type 'quit' or 'exit' to leave.")
            # Don't exit immediately, allow user to type quit
        except EOFError:
             print("\nEOF detected. Exiting.")
             listener_running = False # Signal listener thread to stop
             break # Exit main loop
        except Exception as e:
            # Catch unexpected errors in the main loop
            print(f"\nAn unexpected error occurred in the CLI: {e}", file=sys.stderr)
            logger.critical(f"Unexpected critical error in main CLI loop: {e}", exc_info=True)
            # Maybe sleep or attempt recovery? For now, just log and continue.
            time.sleep(1)


def run_companion():
    """Sets up and runs the companion CLI application."""
    global plugin_serial, listener_running
    logger.info(f"--- Starting Akita eMail Companion CLI v{config.VERSION} ---")
    print(f"--- Akita eMail Companion CLI v{config.VERSION} ---")
    print(f"Logging to: {log_file}")

    plugin_serial = connect_to_plugin()
    if not plugin_serial:
        sys.exit(1) # Exit if connection failed

    # Start the background listener thread
    listener_thread = threading.Thread(
        target=plugin_response_listener_thread,
        args=(plugin_serial,),
        name="PluginResponseListener",
        daemon=True # Allow main thread to exit even if this crashes
    )
    listener_thread.start()

    # Give the listener a moment to start up
    time.sleep(0.5)
    if not listener_running:
         logger.error("Listener thread failed to start.")
         print("Error: Could not start background listener thread.", file=sys.stderr)
         close_plugin_connection()
         sys.exit(1)

    # Enter the main command loop
    main_cli_loop()

    # --- Cleanup ---
    print("\nExiting Akita eMail Companion...")
    # Listener thread should stop based on listener_running flag
    if listener_thread.is_alive():
         logger.debug("Waiting for listener thread to stop...")
         listener_thread.join(timeout=2.0) # Wait briefly for listener
         if listener_thread.is_alive():
              logger.warning("Listener thread did not stop gracefully.")

    close_plugin_connection() # Ensure connection is closed
    logger.info("--- Akita eMail Companion CLI stopped ---")
    print("Goodbye!")

# Make runnable as a script
if __name__ == "__main__":
    run_companion()
