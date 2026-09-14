from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from deal_feed_runner import FeedProcessor, NotificationDeliveryError, _drain_queue_once
from deal_watcher.config import DealWatcherConfig
from deal_watcher.feed import ListingCandidate
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.normalizer import extract_specs
from deal_watcher.service import DealWatcherService


class FakeNotifier:
    def __init__(self, result=True):
        self.result = result
        self.messages: list[str] = []

    def notify(self, ad=None, message=None):
        self.messages.append(message or "")
        return self.result


class FeedProcessorAcceptanceTests(unittest.TestCase):
    def _processor(self, db_path: Path, *, notify_score: int = 80):
        processor = FeedProcessor.__new__(FeedProcessor)
        processor.deal_config = DealWatcherConfig(
            enabled=True,
            notify_score=notify_score,
            database_path=str(db_path),
        )
        processor.service = DealWatcherService(processor.deal_config)
        processor.notifier = FakeNotifier(True)
        return processor

    @staticmethod
    def _candidate(
        *,
        avito_id=1234567890,
        price=99_000,
        profile="fast-any-4070",
        mode="fast",
        title="Lenovo Legion 5 RTX 4070 32GB 1TB",
        description="",
    ):
        return ListingCandidate(
            avito_id=avito_id,
            title=title,
            description=description,
            price=price,
            url=f"https://www.avito.ru/moskva/noutbuki/legion_{avito_id}",
            profile=profile,
            mode=mode,
            source="acceptance",
        )

    def test_cold_start_strong_deal_reaches_notification_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db")
            candidate = self._candidate()
            delivered = processor.process(candidate)
            self.assertTrue(delivered)
            self.assertEqual(len(processor.notifier.messages), 1)
            self.assertIn("80/100", processor.notifier.messages[0])
            self.assertIn(candidate.url, processor.notifier.messages[0])

    def test_delivery_failure_does_not_mark_seen_and_retry_can_deliver(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db")
            candidate = self._candidate()
            processor.notifier = FakeNotifier(False)

            with self.assertRaises(NotificationDeliveryError):
                processor.process(candidate)
            self.assertFalse(
                processor.service.store.is_seen(
                    candidate.profile, candidate.avito_id, candidate.price
                )
            )

            processor.notifier = FakeNotifier(True)
            self.assertTrue(processor.process(candidate))
            self.assertTrue(
                processor.service.store.is_seen(
                    candidate.profile, candidate.avito_id, candidate.price
                )
            )

    def test_delivered_alert_suppresses_same_price_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db")
            candidate = self._candidate()
            self.assertTrue(processor.process(candidate))
            self.assertFalse(processor.process(candidate))
            self.assertEqual(len(processor.notifier.messages), 1)

    def test_low_score_revision_does_not_block_richer_same_price_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db")
            sparse = self._candidate(title="Ноутбук RTX 4070")
            rich = self._candidate(
                title="Lenovo Legion 5 RTX 4070 32GB 1TB",
                description="отличное состояние",
            )

            self.assertFalse(processor.process(sparse))
            self.assertFalse(
                processor.service.store.is_seen(
                    sparse.profile, sparse.avito_id, sparse.price
                )
            )
            self.assertTrue(processor.process(rich))
            self.assertEqual(len(processor.notifier.messages), 1)

    def test_price_change_is_new_alert_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db", notify_score=60)
            first = self._candidate(price=110_000)
            second = self._candidate(price=99_000)
            processor.process(first)
            processor.process(second)
            self.assertEqual(len(processor.notifier.messages), 2)
            self.assertTrue(
                processor.service.store.is_seen(
                    second.profile, second.avito_id, second.price
                )
            )

    def test_market_event_is_silent_and_enters_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db")
            candidate = self._candidate(
                profile="market-new-4070",
                mode="market",
            )
            self.assertFalse(processor.process(candidate))
            self.assertEqual(processor.notifier.messages, [])
            self.assertFalse(
                processor.service.store.is_seen(
                    candidate.profile, candidate.avito_id, candidate.price
                )
            )
            specs = extract_specs(candidate.title, candidate.description)
            stats = processor.service.store.market_stats(specs, min_samples=1)
            self.assertEqual(stats.sample_size, 1)
            self.assertEqual(stats.median_price, 99_000)

    def test_same_listing_fast_then_market_remains_independent_by_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            processor = self._processor(Path(tmp) / "test.db")
            fast = self._candidate()
            market = self._candidate(profile="market-new-4070", mode="market")
            self.assertTrue(processor.process(fast))
            self.assertFalse(processor.process(market))
            self.assertTrue(
                processor.service.store.is_seen(
                    fast.profile, fast.avito_id, fast.price
                )
            )
            self.assertFalse(
                processor.service.store.is_seen(
                    market.profile, market.avito_id, market.price
                )
            )
            self.assertEqual(len(processor.notifier.messages), 1)

    def test_queue_keeps_event_pending_when_notification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            processor = self._processor(db)
            processor.notifier = FakeNotifier(False)
            queue = FeedEventQueue(db)
            queue.enqueue(self._candidate())

            self.assertEqual(_drain_queue_once(processor=processor, queue=queue), 0)
            stats = queue.stats()
            self.assertEqual(stats["pending"], 1)
            self.assertEqual(stats["done"], 0)

    def test_stale_fast_backlog_is_analyzed_but_not_alerted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            processor = self._processor(db)
            queue = FeedEventQueue(db)
            old = datetime.now(timezone.utc) - timedelta(hours=3)
            candidate = self._candidate(avito_id=1234567999)
            queue.enqueue(candidate, observed_at=old)

            self.assertEqual(_drain_queue_once(processor=processor, queue=queue), 1)
            self.assertEqual(processor.notifier.messages, [])
            self.assertEqual(queue.stats()["done"], 1)
            self.assertFalse(
                processor.service.store.is_seen(
                    candidate.profile, candidate.avito_id, candidate.price
                )
            )
            specs = extract_specs(candidate.title, candidate.description)
            # It was still analyzed into deal_listings even though alert delivery
            # was suppressed as stale.
            self.assertIsNotNone(specs.gpu)


if __name__ == "__main__":
    unittest.main()
