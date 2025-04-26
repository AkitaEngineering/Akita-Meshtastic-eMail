# Akita eMail for Meshtastic

**Version: 0.2.0**
**Author:** Akita Engineering ([www.akitaengineering.com](http://www.akitaengineering.com))
**License:** GPLv3

Akita eMail is an experimental, off-grid, store-and-forward email-like system designed to operate over Meshtastic LoRa mesh networks. It enables users to send and receive text-based messages with subjects, mimicking basic email functionality without relying on internet infrastructure.

**Disclaimer:** This is proof-of-concept software. LoRa has significant bandwidth and duty-cycle limitations. This system is not designed for high-volume traffic and relies on nodes being online to forward messages. Reliability depends heavily on mesh density, configuration, and environmental factors. Use at your own risk.

## Features

* **Off-Grid Messaging:** Send/receive text messages (`to`, `from`, `subject`, `body`) over Meshtastic.
* **Store-and-Forward:** Messages are stored locally (in SQLite DB) and forwarded through the mesh.
* **Reliability (Best Effort):** Uses application-level Acknowledgements (ACKs) to confirm message delivery to the final recipient node. Messages are automatically retried if not ACKed within a configurable interval.
* **Persistent Storage:** Uses SQLite for inbox and outbox storage on the plugin host computer.
* **Basic Routing:** Leverages the Meshtastic node database and routing capabilities (`sendText` handles path finding).
* **Hop Limit:** Prevents messages from looping indefinitely (configurable).
* **Message Expiry:** Messages that cannot be delivered after a configurable duration are marked as failed.
* **Alias Management:** Set your Meshtastic node's short name via the companion CLI.
* **Modular Design:** Codebase structured into logical Python modules and package.
* **Companion CLI:** Provides a command-line interface (runs separately) for interacting with the system (sending/reading mail, checking status).
* **Channel Usage:** Operates exclusively on the Meshtastic **Primary Channel (Channel 0)**.
* **Logging:** Configurable logging for both plugin and companion CLI.

## Requirements

* **Meshtastic Network:** At least two Meshtastic-compatible devices flashed with recent firmware (v2.2+ recommended).
* **Host Computer:** A computer (like Raspberry Pi, Linux PC, Mac, Windows) connected to one of the Meshtastic devices via USB (or potentially TCP). This host runs the `AkitaEmailPlugin`.
* **Python:** Python 3.8+ installed on the host computer.
* **Libraries:** `meshtastic` and `pyserial` Python libraries (see `requirements.txt`).

## Installation

1.  **Clone the Repository:**
    ```bash
    git clone [https://github.com/akitaengineering/akita_email.git](https://github.com/akitaengineering/akita_email.git) 
    cd akita_email
    ```

2.  **Install Dependencies:**
    ```bash
    # Recommended: Create and activate a virtual environment first
    # python -m venv venv
    # source venv/bin/activate  # Linux/macOS
    # venv\Scripts\activate    # Windows

    pip install -r requirements.txt
    ```

3.  **Configure Ports (IMPORTANT):**
    * Edit `akita_email/config.py` to set the correct serial ports for communication **between the plugin and companion CLI processes**.
    * **`COMPANION_SERIAL_PORT`**: Port the **Plugin** uses to talk TO the **Companion CLI**.
    * **`COMPANION_PLUGIN_PORT`**: Port the **Companion CLI** uses to talk TO the **Plugin**.
    * **Crucially:** If running both processes on the **same computer**, you MUST use *virtual serial ports* (e.g., created with `socat` on Linux/macOS or `com0com` on Windows). Assign one end of the virtual pair to each setting. See `CONFIGURATION.md` (if available) or online guides for setting up virtual ports.
    * If running on **different computers**, use correctly connected physical serial ports/adapters.
    * *Failure to configure these ports correctly will prevent the CLI from communicating with the plugin.*

## Running Akita eMail

You need to run **two** separate processes, typically in different terminal windows:

1.  **Run the Plugin Process:**
    * Connect your Meshtastic device (the one acting as the 'modem') to the host computer via USB.
    * Identify the serial port assigned to the Meshtastic device (e.g., `/dev/ttyACM0`, `/dev/ttyUSB0`, `COM3`).
    * Start the plugin script from the project root directory (`akita_email/`), providing the Meshtastic device port:
        ```bash
        python run_plugin.py --port /dev/ttyACM0 # Replace with your Meshtastic device port
        ```
        *Use `--host <ip_address>` instead of `--port` if connecting to the Meshtastic device via TCP.*
    * The plugin will start, connect to the Meshtastic device, initialize its database, and attempt to listen on the `COMPANION_SERIAL_PORT` for connections from the companion CLI.

2.  **Run the Companion CLI Process:**
    * In a **separate terminal window**, from the project root directory (`akita_email/`):
        ```bash
        python run_companion.py
        ```
    * The companion CLI will start and attempt to connect to the plugin process via the `COMPANION_PLUGIN_PORT`.
    * If the connection is successful, you will see a `--- Connected to Akita Plugin ---` message and the `Akita>` prompt.

## Usage (Companion CLI Commands)

Type commands at the `Akita>` prompt:

* `send`: Start composing a new email message. Prompts for To (Node ID in decimal or hex), Subject, and Body (end with Ctrl+D or 'EOF' on a new line).
* `read [limit]`: Display received emails from the inbox (default: 50 most recent).
* `status <message_id>`: Check the status (pending, sent, acked, failed) of an outgoing message using its unique ID.
* `alias <name>`: Set your Meshtastic node's short name (max ~12 ASCII chars). Use `alias ""` to clear. (May require node restart).
* `ping`: Send a ping to the plugin process to check the serial link.
* `help`: Show available commands.
* `quit` or `exit`: Stop the companion CLI.

*(Note: Message IDs are UUIDs and are shown when reading messages or in status updates.)*

## How it Works (Simplified)

1.  **Send:** User types `send` in Companion CLI -> CLI prompts for details -> CLI sends `send_email` command (JSON) to Plugin via serial (`COMPANION_PLUGIN_PORT`).
2.  **Queue:** Plugin receives command -> Creates `Email` object -> Stores in `outbox` DB (status: `pending`) -> Assigns unique `message_id`.
3.  **Process:** Plugin's background queue thread fetches pending email from DB.
4.  **Encode & Send:** Plugin encodes email to compact JSON -> Calls `meshtastic.interface.sendText()` targeting the final recipient Node ID on Channel 0 with appropriate hop limit.
5.  **Mesh Relay:** Meshtastic nodes relay the packet over LoRa using built-in routing.
6.  **Receive & Store:** If the recipient node runs the Akita Plugin -> Plugin receives packet -> Decodes email -> Stores in `inbox` DB (if new).
7.  **ACK:** Recipient Plugin encodes an ACK message (JSON) containing original `message_id` -> Sends ACK back towards original sender via `sendText()`.
8.  **Confirm:** Original sender's Plugin receives ACK -> Matches `ack_for_id` -> Updates original email status in `outbox` DB to `acked` -> Notifies its Companion CLI.
9.  **Retry/Fail:** If sender doesn't receive ACK within `MESSAGE_RETRY_INTERVAL` -> Queue processor resends original email (up to `MESSAGE_EXPIRY_TIME`). If expiry reached -> Status marked `failed`.

## License

This project is licensed under the GNU General Public License v3.0. A copy of the license should be included in the file `LICENSE`. You can also find the full text online at:
[https://www.gnu.org/licenses/gpl-3.0.en.html](https://www.gnu.org/licenses/gpl-3.0.en.html)

## Contributing

Contributions, bug reports, and feature requests are welcome! Please open an issue or submit a pull request on the GitHub repository.

## Limitations & Future Ideas

* **Efficiency:** JSON is human-readable but verbose for LoRa. CBOR or custom binary formats could significantly reduce airtime usage.
* **Routing:** Relies entirely on Meshtastic's routing. Application-level routing or source routing could potentially improve reliability in challenging mesh conditions but adds complexity.
* **Alias Resolution:** Currently uses Node IDs for addressing. A distributed alias lookup system would be complex but user-friendly.
* **Error Handling:** Further robustness can be added for edge cases (e.g., database corruption, more specific serial errors).
* **UI:** Companion is CLI only. A simple web interface (e.g., using Flask/WebSockets) hosted by the plugin process could be an alternative to the serial CLI.
* **Group Messages:** Not implemented. Would require protocol changes (e.g., list of recipients, different ACK logic).
* **Attachments:** Not feasible over LoRa due to bandwidth limitations.
* **Security:** Relies solely on Meshtastic's channel encryption. No additional end-to-end application-level encryption is implemented.
* **Companion<->Plugin IPC:** Serial communication requires careful port setup, especially on the same host. Alternatives like local sockets (TCP or Unix domain) or ZeroMQ could be more robust for same-machine setups.

## Attribution

This software was developed by Akita Engineering ([www.akitaengineering.com](http://www.akitaengineering.com)).
