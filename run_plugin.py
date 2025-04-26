#!/usr/bin/env python3
# run_plugin.py
# Main script to start the Akita eMail Meshtastic Plugin.

import time
import logging
import argparse
import sys
import os

# --- Dynamic Path Setup ---
# Add the parent directory (project root) to the Python path
# This allows running the script from the project root (`python run_plugin.py`)
# and ensures the 'akita_email' package can be imported.
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- Import Core Components ---
try:
    import meshtastic
    import meshtastic.serial_interface
    import meshtastic.tcp_interface
    from akita_email import config
    from akita_email.plugin import AkitaEmailPlugin
    from akita_email.exceptions import AkitaEmailError, ConfigurationError, DatabaseError
except ImportError as e:
    print(f"Error importing required modules: {e}", file=sys.stderr)
    print("Please ensure you have installed the necessary libraries (meshtastic, pyserial)", file=sys.stderr)
    print("And that the script is run correctly relative to the 'akita_email' package.", file=sys.stderr)
    sys.exit(1)

# --- Setup Logging ---
# Use the central logger setup from config
# Note: Console logging is enabled by default in config.setup_logger
logger = config.setup_logger(config.APP_NAME + ".Runner", config.PLUGIN_LOG_LEVEL, config.PLUGIN_LOG_FILE, console=True)

def main():
    """Parses arguments, connects to Meshtastic, starts the plugin, and handles shutdown."""
    parser = argparse.ArgumentParser(
        description=f"Run the Akita eMail Plugin v{config.VERSION} for Meshtastic.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter # Show defaults in help
    )
    # Connection arguments (mutually exclusive group ideally, but handle precedence)
    conn_group = parser.add_argument_group('Meshtastic Connection')
    conn_group.add_argument(
        '--port',
        help='Serial port device for Meshtastic node (e.g., /dev/ttyUSB0, COM3)',
        default=None
    )
    conn_group.add_argument(
        '--host',
        help='TCP hostname or IP address for Meshtastic node (if using TCP interface)',
        default=None
    )
    conn_group.add_argument(
        '--device',
        help='Specify Meshtastic device path directly (alternative to --port)',
        default=None
    )

    # Logging arguments
    log_group = parser.add_argument_group('Logging')
    log_group.add_argument(
        '--no-log-file',
        action='store_true',
        help='Disable logging to the log file (log only to console).'
    )
    log_group.add_argument(
        '--debug',
        action='store_true',
        help='Enable DEBUG level logging (overrides config file level).'
    )

    args = parser.parse_args()

    # --- Configure Logging Level ---
    if args.debug:
        logger.info("DEBUG logging enabled via command line.")
        # Update level for all configured handlers
        logging.getLogger(config.APP_NAME).setLevel(logging.DEBUG)
        for handler in logging.getLogger(config.APP_NAME).handlers:
             handler.setLevel(logging.DEBUG)
        # Also set root logger level if needed, though specific logger is better
        # logging.getLogger().setLevel(logging.DEBUG)


    # --- Disable File Logging if requested ---
    if args.no_log_file:
         logger.info("File logging disabled via command line.")
         # Find and remove the FileHandler
         app_logger = logging.getLogger(config.APP_NAME)
         file_handler = None
         for handler in app_logger.handlers:
             if isinstance(handler, logging.FileHandler):
                 file_handler = handler
                 break
         if file_handler:
             app_logger.removeHandler(file_handler)
             file_handler.close() # Ensure file is closed


    # --- Establish Meshtastic Connection ---
    meshtastic_interface = None
    try:
        # Determine connection method (Port/Device takes precedence over Host)
        if args.port or args.device:
            device_path = args.port or args.device
            logger.info(f"Connecting to Meshtastic node via Serial: {device_path}")
            meshtastic_interface = meshtastic.serial_interface.SerialInterface(device=device_path)
        elif args.host:
            logger.info(f"Connecting to Meshtastic node via TCP: {args.host}")
            meshtastic_interface = meshtastic.tcp_interface.TCPInterface(hostname=args.host)
        else:
            # Attempt default SerialInterface connection if no specific method given
            logger.info("No specific port or host provided, attempting default SerialInterface connection...")
            meshtastic_interface = meshtastic.serial_interface.SerialInterface()

        # Basic check after connection attempt (e.g., wait for myInfo)
        logger.info("Waiting for Meshtastic connection to establish...")
        time.sleep(2) # Give it a moment
        if not meshtastic_interface or not meshtastic_interface.myInfo:
             raise meshtastic.MeshtasticError("Failed to get node info after connection. Is device responsive?")
        logger.info("Meshtastic connection established successfully.")

    except meshtastic.MeshtasticError as e:
        logger.critical(f"Failed to connect to Meshtastic device: {e}", exc_info=True)
        print(f"\nError: Could not connect to Meshtastic device: {e}", file=sys.stderr)
        if isinstance(e, meshtastic.NoProtoError):
             print("Hint: Is the Meshtastic device plugged in, powered on, and the firmware running?", file=sys.stderr)
        elif "permission denied" in str(e).lower():
             print("Hint: You might need permissions for the serial port.", file=sys.stderr)
             print("      On Linux: Add user to 'dialout' group (e.g., sudo usermod -a -G dialout $USER) and log out/in.", file=sys.stderr)
        elif "could not open port" in str(e).lower():
             print(f"Hint: Check if the port '{args.port or args.device or 'default'}' is correct and not in use by another program.", file=sys.stderr)
        sys.exit(1) # Exit if connection fails
    except Exception as e:
         # Catch any other unexpected errors during connection
         logger.critical(f"Unexpected error during Meshtastic connection: {e}", exc_info=True)
         print(f"\nError: An unexpected error occurred connecting to Meshtastic: {e}", file=sys.stderr)
         sys.exit(1)


    # --- Initialize and Start the Plugin ---
    plugin: Optional[AkitaEmailPlugin] = None
    try:
        logger.info(f"Initializing Akita eMail Plugin v{config.VERSION}...")
        plugin = AkitaEmailPlugin(meshtastic_interface)
        plugin.start() # This starts background threads

        logger.info("Plugin running. Press Ctrl+C to stop.")

        # Keep the main thread alive while the plugin runs in background threads
        while True:
            # We could add checks here, e.g., if background threads are still alive
            if not plugin.running: # Check if plugin signaled stop internally
                 logger.warning("Plugin indicated it is no longer running. Shutting down.")
                 break
            # Check Meshtastic interface health? (Less reliable way)
            # if not meshtastic_interface._rxThread or not meshtastic_interface._rxThread.is_alive():
            #    logger.error("Meshtastic RX thread seems to have stopped. Shutting down.")
            #    break
            time.sleep(5) # Check periodically

    except ConfigurationError as e:
         logger.critical(f"Plugin configuration error during initialization: {e}", exc_info=True)
         print(f"\nError: Plugin configuration failed: {e}", file=sys.stderr)
    except DatabaseError as e:
         logger.critical(f"Plugin database error during initialization: {e}", exc_info=True)
         print(f"\nError: Plugin database initialization failed: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        logger.info("Ctrl+C received. Initiating shutdown...")
        # The 'finally' block will handle stopping the plugin
    except Exception as e:
        # Catch unexpected errors during plugin start or the main loop
        logger.critical(f"A critical error occurred while running the plugin: {e}", exc_info=True)
        print(f"\nCritical Error: {e}", file=sys.stderr)
    finally:
        # --- Graceful Shutdown ---
        logger.info("Starting shutdown sequence...")
        if plugin and plugin.running:
            logger.info("Stopping AkitaEmailPlugin...")
            try:
                plugin.stop() # This stops threads, closes DB, companion serial
            except Exception as e:
                logger.error(f"Error during plugin stop: {e}", exc_info=True)

        if meshtastic_interface:
            logger.info("Closing Meshtastic interface...")
            try:
                meshtastic_interface.close()
            except Exception as e:
                logger.error(f"Error closing Meshtastic interface: {e}", exc_info=True)

        logger.info("Shutdown complete.")
        print("\nAkita eMail Plugin stopped.")

if __name__ == "__main__":
    main()
