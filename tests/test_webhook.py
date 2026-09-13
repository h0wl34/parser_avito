from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from deal_watcher.feed import ListingCandidate
from deal_watcher.webhook import (
    append_candidate_durable,
    candidate_from_avigram_payload,
    verify_avigram_signature,
)


class AvigramWebhookTests(unittest.TestCase):
    def _payload(self, search_name: str = "fast-any-4070"):
        return {
            "id": 1234567890,
            "title": "Lenovo Legion 5 RTX 4070",
            "price": "99 000 ₽",
            "description": "Почти новый, использовался пару дней",
            "timestamp": 1789300800000,
            "link": "https://www.avito.ru/moskva/noutbuki/x_1234567890",
            "parameters": [
                {"title": "Оперативная память", "description": "32 ГБ"},
                {"title": "SSD", "description": "1 ТБ"},
            ],
            "seller": {"id": "seller-1", "name": "Seller"},
            "info": {
                "searchId": 42,
                "searchName": search_name,
                "searchUrl": "https://www.avito.ru/moskva/noutbuki",
                "userId": 7,
                "sentAt": 1789300800000,
                "subExpiresAt": None,
            },
        }

    def test_payload_maps_to_fast_candidate(self):
        candidate = candidate_from_avigram_payload(self._payload())
        self.assertEqual(candidate.avito_id, 1234567890)
        self.assertEqual(candidate.price, 99000)
        self.assertEqual(candidate.profile, "fast-any-4070")
        self.assertEqual(candidate.mode, "fast")
        self.assertEqual(candidate.seller_id, "seller-1")
        self.assertIn("32 ГБ", candidate.description)
        self.assertFalse(candidate.baseline_eligible)

    def test_market_search_name_maps_to_baseline_event(self):
        candidate = candidate_from_avigram_payload(
            self._payload("market-new-4070")
        )
        self.assertEqual(candidate.mode, "market")
        self.assertTrue(candidate.baseline_eligible)

    def test_signature_matches_documented_timestamp_dot_raw_body_format(self):
        raw = json.dumps(self._payload(), separators=(",", ":")).encode("utf-8")
        timestamp = "1789300800000"
        secret = "test-secret"
        digest = hmac.new(
            secret.encode(),
            timestamp.encode() + b"." + raw,
            hashlib.sha256,
        ).hexdigest()
        signature = "sha256=" + digest
        self.assertTrue(
            verify_avigram_signature(
                raw,
                timestamp=timestamp,
                signature=signature,
                secret=secret,
                now_ms=1789300800000,
            )
        )
        self.assertFalse(
            verify_avigram_signature(
                raw + b" ",
                timestamp=timestamp,
                signature=signature,
                secret=secret,
                now_ms=1789300800000,
            )
        )

    def test_signature_rejects_stale_timestamp(self):
        self.assertFalse(
            verify_avigram_signature(
                b"{}",
                timestamp="1000",
                signature="sha256=deadbeef",
                secret="x",
                max_skew_seconds=300,
                now_ms=1_000_000,
            )
        )

    def test_durable_spool_line_round_trips(self):
        candidate = ListingCandidate(
            avito_id=1234567890,
            title="ThinkBook RTX 4060",
            price=85000,
            url="https://www.avito.ru/x_1234567890",
            profile="fast-any-4060",
            mode="fast",
            source="avigram",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.jsonl"
            append_candidate_durable(candidate, path)
            line = path.read_text(encoding="utf-8").strip()
            restored = ListingCandidate.from_json(line)
            self.assertEqual(restored.avito_id, candidate.avito_id)
            self.assertEqual(restored.price, candidate.price)
            self.assertEqual(restored.profile, candidate.profile)


if __name__ == "__main__":
    unittest.main()
