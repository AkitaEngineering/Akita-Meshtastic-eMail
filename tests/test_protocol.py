import unittest

from meshtastic.protobuf import mesh_pb2

from akita_email import config, protocol
from akita_email.exceptions import ProtocolError
from akita_email.models import Email


class ProtocolTests(unittest.TestCase):
    def _email(self, body: str = "hello") -> Email:
        return Email(
            message_id="123e4567-e89b-12d3-a456-426614174000",
            to_node_id=2,
            from_node_id=1,
            subject="subject",
            body=body,
            timestamp=1_700_000_000,
        )

    def test_private_app_payload_round_trip(self):
        email = self._email()

        payload = protocol.encode_email_to_lora(email)
        decoded = protocol.decode_lora_packet(
            {
                "from": 1,
                "to": 2,
                "decoded": {"portnum": "PRIVATE_APP", "payload": payload},
            }
        )

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded[config.MSG_KEY_ID], email.message_id)
        self.assertEqual(decoded[config.MSG_KEY_BODY], email.body)

    def test_legacy_text_payload_round_trip(self):
        email = self._email()

        payload = protocol.encode_email_to_lora(email)
        decoded = protocol.decode_lora_packet(
            {
                "from": 1,
                "to": 2,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": payload.decode("utf-8")},
            }
        )

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded[config.MSG_KEY_ID], email.message_id)

    def test_rejects_oversized_email_payload(self):
        payload_limit = int(mesh_pb2.Constants.DATA_PAYLOAD_LEN)
        body = "x" * payload_limit
        email = self._email(body=body)
        while protocol.estimate_email_payload_size(email) <= payload_limit:
            body += "x"
            email = self._email(body=body)

        with self.assertRaises(ProtocolError):
            protocol.encode_email_to_lora(email)

    def test_rejects_ack_id_that_would_overflow_limit(self):
        with self.assertRaises(ProtocolError):
            protocol.encode_ack_to_lora("x" * config.MESSAGE_ID_MAX_LENGTH, 2, 1)


if __name__ == "__main__":
    unittest.main()