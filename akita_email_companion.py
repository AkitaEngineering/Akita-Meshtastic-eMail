# Akita eMail for Meshtastic - Companion Device (akita_email_companion.py)
import serial
import json
import sqlite3
import logging

MESHTASTIC_PORT = "/dev/ttyACM0"
MESHTASTIC_BAUD = 115200
DATABASE_FILE = "akita_email_companion.db"
LOG_FILE = "akita_email_companion.log"

# Logging setup
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

db = sqlite3.connect(DATABASE_FILE)
cursor = db.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS emails
                   (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER,
                    content TEXT)''')
db.commit()

try:
    meshtastic_serial = serial.Serial(MESHTASTIC_PORT, MESHTASTIC_BAUD, timeout=1)
    logging.info("Akita eMail Companion initialized.")
    while True:
        command = input("Enter command (send_email, read_emails, set_alias): ")
        if command == "send_email":
            try:
                recipient = input("Recipient ID: ")
                content = input("Message: ")
                email = {"recipient_id": int(recipient), "content": content}
                payload = {"send_email": email}
                meshtastic_serial.write(json.dumps(payload).encode())
                logging.info(f"Sending email to recipient {recipient}")
            except Exception as e:
                logging.error(f"Error sending email: {e}")

        elif command == "read_emails":
            try:
                cursor.execute("SELECT sender_id, content FROM emails")
                rows = cursor.fetchall()
                for row in rows:
                    print(f"From {row[0]}: {row[1]}")
                logging.info("Emails read.")
            except sqlite3.Error as e:
                logging.error(f"Database error reading emails: {e}")

        elif command == "set_alias":
            try:
                alias = input("Enter your alias: ")
                payload = {"alias_update": alias}
                meshtastic_serial.write(json.dumps(payload).encode())
                logging.info(f"Alias set to {alias}")
            except Exception as e:
                logging.error(f"Error setting alias: {e}")

        try:
            line = meshtastic_serial.readline().decode().strip()
            if line:
                try:
                    response = json.loads(line)
                    if "received_email" in response:
                        email = response["received_email"]
                        cursor.execute("INSERT INTO emails (sender_id, content) VALUES (?, ?)", (email["sender_id"], email["content"]))
                        db.commit()
                        print(f"Received email: {email['content']}")
                        logging.info(f"Received email from {email['sender_id']}")
                except json.JSONDecodeError:
                    logging.warning("Received non-JSON message from Meshtastic.")
        except serial.SerialException as e:
            logging.error(f"Meshtastic serial error: {e}")
            break
        except Exception as e:
            logging.error(f"Error processing received message: {e}")

except serial.SerialException as e:
    logging.error(f"Akita eMail Companion: Meshtastic device not found: {e}")
finally:
    db.close()
    logging.info("Akita eMail Companion terminated.")
