from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from deal_watcher.feed import ListingCandidate
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.jsonl_queue import file_identity, iter_complete_records
from deal_watcher.models import Condition, LaptopSpecs
from deal_watcher.storage import DealWatcherStore


UTC = timezone.utc


def candidate(
    *,
    avito_id: int = 1234567890,
    price: int = 99_000,
    profile: str = "fast-any-4070",
    source: str = "avigram",
) -> ListingCandidate:
    return ListingCandidate(
        avito_id=avito_id,
        title="Lenovo Legion 5 RTX 4070 32GB 1TB",
        price=price,
        url=f"https://www.avito.ru/moskva/noutbuki/x_{avito_id}",
        profile=profile,
        mode="market" if profile.startswith("market-") else "fast",
        source=source,
    )


class FeedQueueTests(unittest.TestCase):
    def test_provider_retries_are_deduplicated_but_price_and_profile_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            self.assertTrue(queue.enqueue(candidate(source="avigram")))
            self.assertFalse(queue.enqueue(candidate(source="jsonl")))
            self.assertTrue(queue.enqueue(candidate(price=95_000)))
            self.assertTrue(queue.enqueue(candidate(profile="market-new-4070")))
            self.assertEqual(queue.stats()["pending"], 3)

    def test_claim_has_lease_and_expired_processing_event_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            now = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
            queue.enqueue(candidate(), observed_at=now)

            first = queue.claim_due(now=now, lease_seconds=60)
            self.assertIsNotNone(first)
            self.assertEqual(first.attempts, 1)
            self.assertIsNone(
                queue.claim_due(now=now + timedelta(seconds=30), lease_seconds=60)
            )

            recovered = queue.claim_due(
                now=now + timedelta(seconds=61),
                lease_seconds=60,
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.event_key, first.event_key)
            self.assertEqual(recovered.attempts, 2)

    def test_retry_respects_next_attempt_and_ack_finishes_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            now = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
            queue.enqueue(candidate(), observed_at=now)
            event = queue.claim_due(now=now)
            self.assertIsNotNone(event)

            queue.retry(
                event.event_key,
                error="telegram unavailable",
                delay_seconds=30,
                now=now,
            )
            self.assertIsNone(queue.claim_due(now=now + timedelta(seconds=29)))
            retried = queue.claim_due(now=now + timedelta(seconds=31))
            self.assertIsNotNone(retried)
            self.assertEqual(retried.attempts, 2)

            queue.ack(retried.event_key, now=now + timedelta(seconds=32))
            self.assertIsNone(queue.claim_due(now=now + timedelta(hours=1)))
            self.assertEqual(queue.stats()["done"], 1)

    def test_prune_only_removes_old_completed_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            old = datetime(2026, 8, 1, tzinfo=UTC)
            queue.enqueue(candidate(avito_id=1), observed_at=old)
            event = queue.claim_due(now=old)
            queue.ack(event.event_key, now=old)

            queue.enqueue(candidate(avito_id=2))
            removed = queue.prune_done(
                keep_days=14,
                now=datetime(2026, 9, 14, tzinfo=UTC),
            )
            self.assertEqual(removed, 1)
            stats = queue.stats()
            self.assertEqual(stats["done"], 0)
            self.assertEqual(stats["pending"], 1)


class JsonlQueueTests(unittest.TestCase):
    def test_partial_final_line_is_not_yielded_or_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.jsonl"
            first = '{"id":1}\n'
            partial = '{"id":2'
            path.write_text(first + partial, encoding="utf-8")

            records = list(iter_complete_records(path, offset=0))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].line, first)
            resume = records[0].end_offset

            with path.open("a", encoding="utf-8") as fh:
                fh.write('}\n')

            records = list(iter_complete_records(path, offset=resume))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].line, '{"id":2}\n')

    def test_file_identity_changes_on_atomic_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feed.jsonl"
            path.write_text("old\n", encoding="utf-8")
            old_identity = file_identity(path)

            replacement = Path(tmp) / "replacement.jsonl"
            replacement.write_text("new\n", encoding="utf-8")
            os.replace(replacement, path)
            self.assertNotEqual(file_identity(path), old_identity)


class StorageDurabilityTests(unittest.TestCase):
    def test_feed_cursor_survives_store_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            first = DealWatcherStore(db)
            first.save_feed_cursor("jsonl:/x", "1:2", 123)

            second = DealWatcherStore(db)
            self.assertEqual(second.get_feed_cursor("jsonl:/x"), ("1:2", 123))

    def test_price_drop_survives_same_price_retry_and_history_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            store = DealWatcherStore(db)
            specs = LaptopSpecs(
                brand="Lenovo",
                family="Legion 5",
                gpu="RTX 4070",
                condition=Condition.NEW_LIKELY,
            )
            t0 = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
            store.record_listing(
                avito_id=10,
                title="before",
                seller_id=None,
                url=None,
                price=110_000,
                specs=specs,
                observed_at=t0,
            )
            first_drop = store.record_listing(
                avito_id=10,
                title="deal",
                seller_id=None,
                url=None,
                price=99_000,
                specs=specs,
                observed_at=t0 + timedelta(minutes=1),
            )
            retry_drop = store.record_listing(
                avito_id=10,
                title="deal retry",
                seller_id=None,
                url=None,
                price=99_000,
                specs=specs,
                observed_at=t0 + timedelta(minutes=2),
            )
            self.assertAlmostEqual(first_drop, 10.0, places=1)
            self.assertAlmostEqual(retry_drop, 10.0, places=1)

            with sqlite3.connect(db) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM deal_prices WHERE avito_id=10"
                ).fetchone()[0]
            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
