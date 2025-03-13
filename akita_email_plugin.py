# Akita eMail for Meshtastic - Plugin (akita_email_plugin.py)
import meshtastic
import meshtastic.serial_interface
import json
import threading
import time
import serial
import sqlite3
import logging

# Configuration
COMPANION_DEVICE_PORT = "/dev/ttyUSB0"
COMPANION_DEVICE_BAUD = 115200
DATABASE_FILE = "akita_email_plugin.db"
LOG_FILE = "akita_email_plugin.log"

# Logging setup
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AkitaEmailPlugin:
    def __init__(self, interface):
        self.interface = interface
        self.companion_serial = None
        self.routing_table = {}
        self.running = True

        self.db = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
        self.cursor = self.db.cursor()
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS messages
                               (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                recipient_id INTEGER,
                                sender_id INTEGER,
                                content TEXT,
                                hops INTEGER,
                                timestamp REAL)''')
        self.db.commit()

        try:
            self.companion_serial = serial.Serial(COMPANION_DEVICE_PORT, COMPANION_DEVICE_BAUD, timeout=1)
            self.companion_thread = threading.Thread(target=self.companion_listener)
            self.companion_thread.daemon = True
            self.companion_thread.start()
            logging.info("Companion device connected.")
        except serial.SerialException as e:
            logging.error(f"Companion device not found: {e}")

        interface.add_receive_handler(self.receive_handler)
        self.queue_thread = threading.Thread(target=self.process_queue)
        self.queue_thread.daemon = True
        self.queue_thread.start()
        self.routing_thread = threading.Thread(target=self.update_routing_table)
        self.routing_thread.daemon = True
        self.routing_thread.start()

        logging.info("Akita eMail Plugin initialized.")

    def stop(self):
        self.running = False
        if self.companion_serial:
            self.companion_serial.close()
        self.db.close()
        logging.info("Akita eMail Plugin stopped.")

    def receive_handler(self, packet, interface):
        if "decoded" in packet and "text" in packet["decoded"]:
            try:
                message = json.loads(packet["decoded"]["text"])
                if "offgrid_email" in message:
                    self.store_message(message["offgrid_email"])
                    logging.info("Off-grid email received and queued.")
            except json.JSONDecodeError:
                logging.warning("Received non-JSON message.")
            except Exception as e:
                logging.error(f"Error processing received packet: {e}")

    def store_message(self, message):
        try:
            self.cursor.execute('''INSERT INTO messages (recipient_id, sender_id, content, hops, timestamp)
                               VALUES (?, ?, ?, ?, ?)''',
                               (message["recipient_id"], message["sender_id"], message["content"], message["hops"], time.time()))
            self.db.commit()
        except sqlite3.Error as e:
            logging.error(f"Database error storing message: {e}")

    def load_messages(self):
        try:
            self.cursor.execute("SELECT recipient_id, sender_id, content, hops, timestamp FROM messages")
            rows = self.cursor.fetchall()
            messages = [{"recipient_id": row[0], "sender_id": row[1], "content": row[2], "hops": row[3], "timestamp": row[4]} for row in rows]
            return messages
        except sqlite3.Error as e:
            logging.error(f"Database error loading messages: {e}")
            return []

    def clear_message(self, message):
        try:
            self.cursor.execute("DELETE FROM messages WHERE recipient_id = ? AND sender_id = ? AND content = ? AND hops = ? AND timestamp = ?", (message["recipient_id"], message["sender_id"], message["content"], message["hops"], message["timestamp"]))
            self.db.commit()
        except sqlite3.Error as e:
            logging.error(f"Database error clearing message: {e}")

    def process_queue(self):
        while self.running:
            messages = self.load_messages()
            if messages:
                message = messages[0]
                self.forward_message(message)
                self.clear_message(message)
            time.sleep(1)

    def forward_message(self, message):
        if message["recipient_id"] == self.interface.meshNode.localNode.getNodeNum():
            logging.info(f"Received message: {message['content']}")
            if self.companion_serial:
                try:
                    self.companion_serial.write(json.dumps({"received_email": message}).encode())
                except serial.SerialException as e:
                    logging.error(f"Error sending received email to companion: {e}")
            return

        message["hops"] += 1
        best_next_hop = self.find_next_hop(message["recipient_id"])

        if best_next_hop:
            payload = {"offgrid_email": message}
            try:
                self.interface.sendText(json.dumps(payload))
                logging.info(f"Forwarding message to {best_next_hop}")
            except Exception as e:
                logging.error(f"Error sending message via Meshtastic: {e}")
        else:
            logging.warning(f"No route to {message['recipient_id']}")
            self.store_message(message)

    def find_next_hop(self, recipient_id):
        if recipient_id in self.routing_table:
            return self.routing_table[recipient_id]["next_hop"]
        return None

    def update_routing_table(self):
        while self.running:
            neighbors = self.interface.meshNode.getNodes()
            for node in neighbors.values():
                node_num = node.getNodeNum()
                if node_num != self.interface.meshNode.localNode.getNodeNum():
                    self.routing_table[node_num] = {"next_hop": node_num, "distance": 1}

            for dest_id in list(self.routing_table.keys()):
                if dest_id not in neighbors and dest_id != self.interface.meshNode.localNode.getNodeNum():
                    del self.routing_table[dest_id]
            time.sleep(5)

    def companion_listener(self):
        while self.running and self.companion_serial:
            try:
                line = self.companion_serial.readline().decode().strip()
                if line:
                    try:
                        command = json.loads(line)
                        if "send_email" in command:
                            email = command["send_email"]
                            email["sender_id"] = self.interface.meshNode.localNode.getNodeNum()
                            email["hops"] = 0
                            self.store_message(email)
                        elif "alias_update" in command:
                            self.interface.meshNode.setShortName(command["alias_update"])

                    except json.JSONDecodeError:
                        logging.warning("Companion sent non-JSON message.")
            except serial.SerialException:
                logging.error("Companion device disconnected.")
                break
            except Exception as e:
                logging.error(f"Error in companion listener: {e}")

if __name__ == "__main__":
    interface = meshtastic.serial_interface.SerialInterface()
    plugin = AkitaEmailPlugin(interface)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        plugin.stop()
        interface.close()
        logging.info("Akita eMail Plugin terminated.")
