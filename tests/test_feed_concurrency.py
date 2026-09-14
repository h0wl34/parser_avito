from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from deal_watcher.feed import ListingCandidate
from deal_watcher.feed_queue import FeedEventQueue
from deal_watcher.models import Condition, LaptopSpecs
from deal_watcher.storage import DealWatcherStore


def candidate(
    avito_id: int = 1234567890,
    *,
    title: str = "Lenovo Legion 5 RTX 4070 32GB 1TB",
    price: int = 99_000,
    profile: str = "fast-any-4070",
) -> ListingCandidate:
    return ListingCandidate(
        avito_id=avito_id,
        title=title,
        price=price,
        url=f"https://www.avito.ru/moskva/noutbuki/x_{avito_id}",
        profile=profile,
        mode="market" if profile.startswith("market-") else "fast",
        source="concurrency-test",
    )


class FeedQueueConcurrencyTests(unittest.TestCase):
    def test_many_concurrent_provider_retries_insert_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            event = candidate()
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(lambda _: queue.enqueue(event), range(40)))

            self.assertEqual(sum(bool(value) for value in results), 1)
            self.assertEqual(queue.stats()["pending"], 1)

    def test_many_unique_events_survive_concurrent_ingress(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            events = [candidate(2_000_000_000 + idx) for idx in range(50)]
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(queue.enqueue, events))

            self.assertTrue(all(results))
            self.assertEqual(queue.stats()["pending"], len(events))

    def test_two_workers_cannot_claim_same_event_simultaneously(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            FeedEventQueue(db).enqueue(candidate())

            # Separate queue objects mimic separate worker processes/connections.
            queue_a = FeedEventQueue(db)
            queue_b = FeedEventQueue(db)
            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(lambda q: q.claim_due(), (queue_a, queue_b)))

            claimed = [event for event in claims if event is not None]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0].candidate.avito_id, 1234567890)

    def test_content_revision_at_same_price_is_a_new_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = FeedEventQueue(Path(tmp) / "test.db")
            sparse = candidate(title="Ноутбук RTX 4070")
            rich = candidate(title="Lenovo Legion 5 RTX 4070 32GB 1TB")

            self.assertTrue(queue.enqueue(sparse))
            self.assertTrue(queue.enqueue(rich))
            self.assertEqual(queue.stats()["pending"], 2)

    def test_deal_store_and_ingress_queue_can_write_same_db_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            queue = FeedEventQueue(db)
            store = DealWatcherStore(db)
            specs = LaptopSpecs(
                brand="Lenovo",
                family="Legion 5",
                gpu="RTX 4070",
                ram_gb=32,
                storage_gb=1024,
                condition=Condition.NEW_LIKELY,
            )

            def enqueue(idx: int):
                return queue.enqueue(candidate(3_000_000_000 + idx))

            def record(idx: int):
                store.record_listing(
                    avito_id=4_000_000_000 + idx,
                    title=f"Legion {idx}",
                    seller_id=None,
                    url=None,
                    price=100_000 + idx,
                    specs=specs,
                    baseline_eligible=True,
                )
                return True

            with ThreadPoolExecutor(max_workers=12) as pool:
                futures = []
                for idx in range(30):
                    futures.append(pool.submit(enqueue, idx))
                    futures.append(pool.submit(record, idx))
                results = [future.result(timeout=15) for future in futures]

            self.assertTrue(all(results))
            self.assertEqual(queue.stats()["pending"], 30)
            stats = store.market_stats(specs, min_samples=1)
            self.assertEqual(stats.sample_size, 30)


if __name__ == "__main__":
    unittest.main()
