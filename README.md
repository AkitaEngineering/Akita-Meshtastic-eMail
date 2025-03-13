# Akita eMail for Meshtastic

Akita eMail for Meshtastic is an off-grid email system designed to operate over Meshtastic networks. It enables users to send and receive text-based emails without relying on traditional internet or cellular infrastructure. This system is particularly useful in remote areas, during emergencies, or in situations where connectivity is limited.

## Features

* **Off-Grid Email:** Send and receive text messages over Meshtastic.
* **Store-and-Forward:** Messages are stored and forwarded through the mesh network until they reach their destination.
* **Persistent Storage:** Messages are stored persistently using SQLite databases on both the Meshtastic device and the companion device.
* **Basic Routing:** Implements a simple distance-vector routing algorithm for message forwarding.
* **Alias Management:** Users can set aliases to simplify addressing.
* **Companion Device:** Utilizes an ESP32-C3 (or similar) as a companion device for enhanced functionality and user interface.
* **Logging:** Robust logging for debugging and monitoring.
* **Error Handling:** Comprehensive error handling for reliability.

## Requirements

* Meshtastic compatible device.
* ESP32-C3 (or similar) companion device (optional but recommended).
* Python 3.x (for Meshtastic plugin).
* ESP32-C3 development environment (for companion device).
* `meshtastic` Python library.
* `pyserial` Python library.
* SQLite.

## Installation

### Meshtastic Plugin

1.  Install the necessary Python libraries:

    ```bash
    pip install meshtastic pyserial
    ```

2.  Save `akita_email_plugin.py` to a directory on your Meshtastic device's host.

3.  Run the plugin:

    ```bash
    python akita_email_plugin.py
    ```

### Companion Device (ESP32-C3)

1.  Ensure you have the ESP32-C3 development environment set up.

2.  Save `akita_email_companion.py` to your ESP32-C3 project directory.

3.  Upload the code to your ESP32-C3.

## Configuration

1.  **Serial Ports:**
    * Adjust `COMPANION_DEVICE_PORT` in `akita_email_plugin.py` and `MESHTASTIC_PORT` in `akita_email_companion.py` to match your serial port configurations.

## Usage

### Companion Device Interface

The companion device provides a command-line interface for interacting with Akita eMail.

* **`send_email`:** Send an email.
    * Prompts for recipient ID and message content.
* **`read_emails`:** Read received emails.
    * Displays emails stored in the companion device's database.
* **`set_alias`:** Set your alias.
    * Sets the alias for your Meshtastic node.

## Logging

* The Meshtastic plugin logs to `akita_email_plugin.log`.
* The companion device logs to `akita_email_companion.log`.

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues.
