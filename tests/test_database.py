import time
import unittest

from akita_email import config
from akita_email.database import AkitaDatabase
from akita_email.models import Email, STATUS_FAILED


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = AkitaDatabase(":memory:")

    def tearDown(self):
        self.db.close()

    def test_in_memory_database_initializes(self):
        self.assertIsNotNone(self.db.conn)

    def test_expired_email_is_marked_failed_and_not_returned(self):
        email = Email(
            message_id="expired-message",
            to_node_id=2,
            from_node_id=1,
            subject="subject",
            body="body",
            timestamp=time.time(),
            created_time=time.time() - config.MESSAGE_EXPIRY_TIME - 5,
        )

        self.assertTrue(self.db.add_outgoing_email(email))

        ready_to_send = self.db.get_emails_to_send()
        status = self.db.get_outbox_status(email.message_id)

        self.assertEqual(ready_to_send, [])
        self.assertIsNotNone(status)
        self.assertEqual(status.status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()