from __future__ import annotations

import hashlib
import hmac
from http.server import HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from deal_webhook_ingress import build_handler
from deal_watcher.feed_config import (
    FeedSourcesConfig,
    JsonlFeedConfig,
    WebhookIngressConfig,
)
from deal_watcher.feed_queue import FeedEventQueue


class WebhookHttpAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secret = "acceptance-secret"
        self.old_secret = os.environ.get("AVIGRAM_CALLBACK_SECRET")
        os.environ["AVIGRAM_CALLBACK_SECRET"] = self.secret
        self.addCleanup(self._restore_secret)

        self.queue = FeedEventQueue(Path(self.tmp.name) / "test.db")
        self.config = FeedSourcesConfig(
            jsonl=JsonlFeedConfig(enabled=False),
            webhook=WebhookIngressConfig(
                enabled=True,
                bind_host="127.0.0.1",
                port=1,
                path="/avigram-callback",
                require_signature=True,
                secret_env="AVIGRAM_CALLBACK_SECRET",
                max_body_bytes=1024,
            ),
        )
        handler = build_handler(self.config, self.queue)
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def _restore_secret(self):
        if self.old_secret is None:
            os.environ.pop("AVIGRAM_CALLBACK_SECRET", None)
        else:
            os.environ["AVIGRAM_CALLBACK_SECRET"] = self.old_secret

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _payload(self):
        now_ms = int(time.time() * 1000)
        return {
            "id": 1234567890,
            "title": "Lenovo Legion 5 RTX 4070 32GB 1TB",
            "price": "99 000 ₽",
            "description": "новый",
            "timestamp": now_ms,
            "link": "https://www.avito.ru/moskva/noutbuki/legion_1234567890",
            "seller": {"id": "seller-1"},
            "info": {"searchName": "fast-any-4070", "searchId": 42},
        }

    def _signed_post(self, payload, *, valid=True):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time() * 1000))
        digest = hmac.new(
            self.secret.encode(),
            timestamp.encode() + b"." + raw,
            hashlib.sha256,
        ).hexdigest()
        signature = "sha256=" + digest
        if not valid:
            signature = "sha256=" + ("0" * 64)
        request = Request(
            self.base + "/avigram-callback",
            data=raw,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Avigram-Timestamp": timestamp,
                "X-Avigram-Signature": signature,
            },
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode())

    def test_valid_callback_is_committed_before_ack_and_duplicate_is_idempotent(self):
        status, body = self._signed_post(self._payload())
        self.assertEqual(status, 202)
        self.assertTrue(body["queued"])
        self.assertEqual(self.queue.stats()["pending"], 1)

        status, body = self._signed_post(self._payload())
        self.assertEqual(status, 202)
        self.assertFalse(body["queued"])
        self.assertEqual(self.queue.stats()["pending"], 1)

        event = self.queue.claim_due()
        self.assertIsNotNone(event)
        self.assertEqual(event.candidate.profile, "fast-any-4070")
        self.assertEqual(event.candidate.price, 99_000)

    def test_invalid_signature_is_rejected_without_queue_write(self):
        with self.assertRaises(HTTPError) as ctx:
            self._signed_post(self._payload(), valid=False)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()
        self.assertEqual(self.queue.stats()["pending"], 0)

    def test_healthz_exposes_queue_health_without_secrets(self):
        with urlopen(self.base + "/healthz", timeout=2) as response:
            body = json.loads(response.read().decode())
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["ingress"], "avigram")
        self.assertIn("pending", body["queue"])
        self.assertNotIn(self.secret, json.dumps(body))


if __name__ == "__main__":
    unittest.main()
