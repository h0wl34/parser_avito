from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from deal_phone_ingress import build_handler
from deal_watcher.phone import (
    PhoneNotificationStore,
    compute_phone_signature,
    verify_phone_signature,
)


UTC = timezone.utc


def sample_payload(event_id: str = "a" * 64) -> dict:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "device_id": "mi13-ultra-test",
        "source": "android_notification_listener",
        "package": "com.avito.android",
        "notification_key": "0|com.avito.android|42|null|10001",
        "notification_id": 42,
        "post_time_ms": 1_789_370_000_000,
        "captured_at_ms": 1_789_370_000_123,
        "title": "Новое объявление",
        "text": "Lenovo Legion RTX 4070 — 99 000 ₽",
        "big_text": "Lenovo Legion RTX 4070 — 99 000 ₽, Москва",
        "actions": [],
        "extras": {"android.title": "Новое объявление"},
    }


class PhoneSignatureTests(unittest.TestCase):
    def test_signature_round_trip_and_sha_prefix(self):
        now = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)
        timestamp = str(int(now.timestamp()))
        body = b'{"hello":"world"}'
        signature = compute_phone_signature("secret", timestamp, body)
        self.assertTrue(
            verify_phone_signature(
                body,
                timestamp=timestamp,
                signature=signature,
                secret="secret",
                now=now,
            )
        )
        self.assertTrue(
            verify_phone_signature(
                body,
                timestamp=timestamp,
                signature="sha256=" + signature,
                secret="secret",
                now=now,
            )
        )
        self.assertFalse(
            verify_phone_signature(
                body + b"x",
                timestamp=timestamp,
                signature=signature,
                secret="secret",
                now=now,
            )
        )

    def test_stale_signature_is_rejected(self):
        now = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)
        sent = now - timedelta(seconds=301)
        timestamp = str(int(sent.timestamp()))
        body = b"{}"
        signature = compute_phone_signature("secret", timestamp, body)
        self.assertFalse(
            verify_phone_signature(
                body,
                timestamp=timestamp,
                signature=signature,
                secret="secret",
                max_skew_seconds=300,
                now=now,
            )
        )


class PhoneStoreTests(unittest.TestCase):
    def test_store_is_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            payload = sample_payload()
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            store = PhoneNotificationStore(db)
            self.assertTrue(store.store(payload, raw_body=raw))
            self.assertFalse(store.store(payload, raw_body=raw))
            self.assertEqual(store.count(), 1)
            rows = PhoneNotificationStore(db).recent()
            self.assertEqual(rows[0].event_id, "a" * 64)
            self.assertEqual(rows[0].package_name, "com.avito.android")
            self.assertIn("Lenovo Legion", rows[0].text or "")
            self.assertEqual(json.loads(rows[0].payload_json)["schema_version"], 1)


class PhoneIngressHttpTests(unittest.TestCase):
    def test_signed_post_is_accepted_and_retry_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PhoneNotificationStore(Path(tmp) / "test.db")
            secret = "correct horse battery staple"
            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(store, secret=secret))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = sample_payload()
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                timestamp = str(int(datetime.now(UTC).timestamp()))
                signature = compute_phone_signature(secret, timestamp, body)

                for expected_stored in (True, False):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request(
                        "POST",
                        "/phone-notification",
                        body=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Phone-Timestamp": timestamp,
                            "X-Phone-Signature": signature,
                        },
                    )
                    response = conn.getresponse()
                    result = json.loads(response.read())
                    conn.close()
                    self.assertEqual(response.status, 202)
                    self.assertEqual(result["stored"], expected_stored)

                self.assertEqual(store.count(), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unsigned_post_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PhoneNotificationStore(Path(tmp) / "test.db")
            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(store, secret="secret"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps(sample_payload()).encode()
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("POST", "/phone-notification", body=body)
                response = conn.getresponse()
                response.read()
                conn.close()
                self.assertEqual(response.status, 401)
                self.assertEqual(store.count(), 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
