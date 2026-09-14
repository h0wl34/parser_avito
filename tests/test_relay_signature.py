from __future__ import annotations

import hashlib
import hmac
import unittest

from deal_watcher.health import verify_relay_signature


class RelaySignatureTests(unittest.TestCase):
    def test_signature_accepts_exact_body_and_rejects_tampering(self):
        body = b'{"text":"watcher critical"}'
        timestamp = "1789372800000"
        key = "test-key"
        digest = hmac.new(
            key.encode(),
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        signature = "sha256=" + digest
        self.assertTrue(
            verify_relay_signature(
                body,
                timestamp=timestamp,
                signature=signature,
                secret=key,
                now_ms=1789372800000,
            )
        )
        self.assertFalse(
            verify_relay_signature(
                body + b" ",
                timestamp=timestamp,
                signature=signature,
                secret=key,
                now_ms=1789372800000,
            )
        )

    def test_signature_rejects_stale_timestamp(self):
        self.assertFalse(
            verify_relay_signature(
                b"{}",
                timestamp="1000",
                signature="sha256=00",
                secret="key",
                max_skew_seconds=300,
                now_ms=1_000_000,
            )
        )


if __name__ == "__main__":
    unittest.main()
