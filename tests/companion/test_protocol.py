from __future__ import annotations

import unittest

from fle.companion.protocol import (
    MAX_DATAGRAM_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_packet,
    encode_packet,
)


class ProtocolTests(unittest.TestCase):
    def test_round_trip_preserves_unicode_and_payload(self) -> None:
        encoded = encode_packet(
            "chat",
            {"text": "挖 32 铁矿", "nested": {"ok": True}},
            packet_id="test-1",
            token="local-token",
            tick=42,
        )
        packet = decode_packet(encoded)
        self.assertEqual(packet.version, PROTOCOL_VERSION)
        self.assertEqual(packet.id, "test-1")
        self.assertEqual(packet.type, "chat")
        self.assertEqual(packet.payload["text"], "挖 32 铁矿")
        self.assertEqual(packet.token, "local-token")
        self.assertEqual(packet.tick, 42)

    def test_rejects_wrong_protocol_version(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "unsupported protocol"):
            decode_packet(b'{"version":99,"id":"x","type":"ping","payload":{}}')

    def test_rejects_oversized_packet(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "maximum"):
            encode_packet("chat", {"text": "x" * MAX_DATAGRAM_BYTES})

    def test_rejects_non_mapping_payload(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "mapping"):
            encode_packet("chat", ["not", "a", "mapping"])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
